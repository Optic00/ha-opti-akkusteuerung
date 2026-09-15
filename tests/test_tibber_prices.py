"""Synthetic HA service snapshots and scheduling, never live homes or devices."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.coordinator import OptiCoordinator
from custom_components.opti_akku.engine import StrategyEngine
from custom_components.opti_akku.sources import build_inputs
from custom_components.opti_akku.tibber_prices import TibberPriceError, async_fetch_prices
from tests.test_coordinator import device
from tests.test_price_sources import dated_prices

BERLIN = ZoneInfo("Europe/Berlin")
HOME = "synthetic-home"
NATIVE = {"price_provider": "tibber", "price_unit": "EUR/kWh", "tibber_home": HOME,
          "tibber_eur_confirmed": True, "price_max_age": 7200, "tibber_entry_id": "synthetic-tibber-entry"}


@pytest.fixture(autouse=True)
def clock(hass, freezer):
    dt_util.set_default_time_zone(BERLIN)
    freezer.move_to(datetime(2026, 9, 13, 6, tzinfo=UTC))
    yield freezer
    dt_util.set_default_time_zone(UTC)


def native_day(day, *, minutes=60, price=.25):
    return [{"start_time": row["start"], "price": row["price"]}
            for row in dated_prices(day, timezone=BERLIN, minutes=minutes, price=price)]


@pytest.fixture
def tibber_entry(hass):
    entry = MockConfigEntry(domain="tibber", title="Synthetic Tibber", entry_id="synthetic-tibber-entry")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def service(hass, tibber_entry):
    payload = {"prices": {HOME: native_day(date(2026, 9, 13)) + native_day(date(2026, 9, 14))}}
    calls = []

    async def handler(call):
        calls.append(call.data)
        return deepcopy(payload)

    hass.services.async_register("tibber", "get_prices", handler, supports_response=SupportsResponse.ONLY)
    return payload, calls


async def test_native_service_snapshot_binds_one_home_and_same_current_price(hass, service):
    payload, calls = service
    payload["prices"][HOME][8]["price"] = -.05
    homes = await async_fetch_prices(hass)
    assert list(homes) == [HOME]
    snapshot = homes[HOME]
    states, attrs, errors = build_inputs({}, NATIVE, {}, dt_util.utcnow(), price_snapshot=snapshot)
    assert not errors
    assert states["sensor.opti_price_current_ct_kwh"] == -5
    assert attrs["sensor.opti_price_series"]["today"][8] == -5
    assert calls == [{"start": "2026-09-13T00:00:00+02:00", "end": "2026-09-15T00:00:00+02:00"}]
    assert hass.states.async_all() == []


@pytest.mark.parametrize("entries", [0, 2])
async def test_service_rejects_ambiguous_entry_selection_before_fetch(hass, entries):
    for index in range(entries):
        MockConfigEntry(domain="tibber", title=f"Synthetic {index}").add_to_hass(hass)
    with patch.object(type(hass.services), "async_call", new=AsyncMock()) as call:
        with pytest.raises(TibberPriceError, match="tibber_config_entries"):
            await async_fetch_prices(hass)
        call.assert_not_called()


@pytest.mark.parametrize("homes", [{}, {"one": [], "two": []}])
async def test_service_rejects_zero_or_multiple_response_homes(hass, service, homes):
    service[0]["prices"] = homes
    with pytest.raises(TibberPriceError, match="tibber_home_count"):
        await async_fetch_prices(hass)


@pytest.mark.parametrize("overrides,error", [
    ({"tibber_home": "another-home"}, "tibber_home_mismatch"),
    ({"tibber_home": ""}, "tibber_home_required"),
    ({"tibber_entry_id": "another-entry"}, "tibber_entry_mismatch"),
    ({"tibber_entry_id": ""}, "tibber_entry_required"),
    ({"tibber_eur_confirmed": False}, "tibber_eur_confirmation_required"),
    ({"tibber_eur_confirmed": "true"}, "tibber_eur_confirmation_required"),
    ({"price_unit": "ct/kWh"}, "tibber_eur_confirmation_required"),
    ({"price_provider": "unknown"}, "invalid_provider"),
    ({"price_max_age": 59}, "invalid_price_max_age"),
    ({"price_max_age": float("nan")}, "invalid_price_max_age"),
])
async def test_explicit_provider_binding_never_falls_back_to_entities(hass, service, overrides, error):
    snapshot = (await async_fetch_prices(hass))[HOME]
    states, attrs, errors = build_inputs({"sensor.opti_price_current_ct_kwh": 999},
        {**NATIVE, **overrides, "sources": {"price_current": "sensor.ignored"}}, {}, dt_util.utcnow(),
        price_snapshot=snapshot)
    assert states["sensor.opti_price_current_ct_kwh"] == "unavailable"
    assert states["sensor.opti_price_series"] == "unavailable"
    assert "sensor.opti_price_series" not in attrs
    assert errors == {"price_current": error, "price_series": error}


async def test_service_timeout_is_bounded_and_does_not_expose_error_data(hass, service, clock):
    async def blocked(*args, **kwargs):
        clock.tick(timedelta(seconds=16))
        await asyncio.Event().wait()

    with patch.object(type(hass.services), "async_call", side_effect=blocked):
        with pytest.raises(TibberPriceError, match="^tibber_timeout$"):
            await async_fetch_prices(hass)


@pytest.mark.parametrize("problem", ["gap", "duplicate", "naive", "order", "nan", "partial_tomorrow"])
async def test_native_intervals_must_prove_complete_days(hass, service, problem):
    rows = service[0]["prices"][HOME]
    if problem == "gap":
        rows.pop(5)
    elif problem == "duplicate":
        rows[5] = rows[4]
    elif problem == "naive":
        rows[5]["start_time"] = "2026-09-13T05:00:00"
    elif problem == "order":
        rows[4], rows[5] = rows[5], rows[4]
    elif problem == "nan":
        rows[5]["price"] = float("nan")
    else:
        rows.pop()
    with pytest.raises(TibberPriceError, match="invalid_price_series"):
        await async_fetch_prices(hass)


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
@pytest.mark.parametrize("minutes", [15, 60])
async def test_native_dst_intervals_preserve_actual_durations(hass, service, clock, day, hours, minutes):
    clock.move_to(datetime.combine(day, datetime.min.time(), tzinfo=BERLIN) + timedelta(minutes=5))
    service[0]["prices"][HOME] = native_day(day, minutes=minutes)
    snapshot = (await async_fetch_prices(hass))[HOME]
    assert len(snapshot.intervals) == hours * 60 // minutes
    assert all((i.end-i.start).total_seconds() == minutes * 60 for i in snapshot.intervals)
    assert snapshot.normalized(dt_util.utcnow(), 7200, HOME)[0] == 25


async def test_cached_tomorrow_becomes_today_by_its_own_dates_and_expires(hass, service, clock):
    clock.move_to(datetime(2026, 9, 13, 23, 50, tzinfo=BERLIN))
    service[0]["prices"][HOME][24:48] = native_day(date(2026, 9, 14), price=.4)
    snapshot = (await async_fetch_prices(hass))[HOME]
    clock.tick(timedelta(minutes=20))
    current, series, _ = snapshot.normalized(dt_util.utcnow(), 7200, HOME)
    assert current == 40 and series["today"] == [40]*24 and series["tomorrow"] == []
    clock.tick(timedelta(hours=2))
    with pytest.raises(TibberPriceError, match="missing_or_stale"):
        snapshot.normalized(dt_util.utcnow(), 7200, HOME)


async def test_missing_tomorrow_cannot_relabel_old_prices_at_midnight(hass, service, clock):
    clock.move_to(datetime(2026, 9, 13, 23, 50, tzinfo=BERLIN))
    service[0]["prices"][HOME] = native_day(date(2026, 9, 13))
    snapshot = (await async_fetch_prices(hass))[HOME]
    clock.tick(timedelta(minutes=20))
    with pytest.raises(TibberPriceError, match="invalid_price_series"):
        snapshot.normalized(dt_util.utcnow(), 7200, HOME)


@pytest.fixture
async def coordinator(hass, service):
    entry = MockConfigEntry(domain="opti_akku", title="Synthetic Opti",
        data={"host": "127.0.0.1", "port": 15020, "unit_id": 3, "profile": "sma_stp_se", "shadow_mode": True},
        options={**NATIVE, "single_inverter": True, "sources": {}})
    entry.add_to_hass(hass)
    engine = await hass.async_add_executor_job(StrategyEngine)
    result = OptiCoordinator(hass, entry, device(), engine)
    await result.async_restore()
    yield result
    await result.async_stop()
    result.device.async_apply.assert_not_awaited()


async def test_slow_fetch_does_not_block_device_refresh_or_shutdown(coordinator):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(_hass):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with patch("custom_components.opti_akku.coordinator.async_fetch_prices", side_effect=slow):
        data = await coordinator._async_update_data()
        await entered.wait()
        assert data["mode"] == "Akku Pause"
        assert not coordinator._update_lock.locked()
        coordinator.device.async_read.return_value["sensor.opti_soc"] = None
        next_data = await coordinator._async_update_data()
        assert next_data["mode"] == "Akku Pause"
        await coordinator.async_stop()
        assert cancelled.is_set() and coordinator._price_task.done()


async def test_cached_fetch_schedule_retry_and_original_ttl(coordinator, clock, service):
    await coordinator._async_update_data()
    await coordinator._price_task
    snapshot = coordinator._price_snapshot
    assert snapshot is not None
    assert len(service[1]) == 1
    clock.tick(timedelta(minutes=29))
    await coordinator._async_update_data()
    assert len(service[1]) == 1
    clock.tick(timedelta(minutes=1))
    with patch("custom_components.opti_akku.coordinator.async_fetch_prices", side_effect=TibberPriceError("tibber_timeout")) as fetch:
        await coordinator._async_update_data()
        await coordinator._price_task
        assert coordinator._price_snapshot is snapshot
        assert coordinator._price_snapshot.fetched_at == snapshot.fetched_at
        clock.tick(timedelta(minutes=4))
        await coordinator._async_update_data()
        assert fetch.call_count == 1
        clock.tick(timedelta(minutes=1))
        await coordinator._async_update_data()
        await coordinator._price_task
        assert fetch.call_count == 2
        clock.tick(timedelta(hours=2))
        data = await coordinator._async_update_data()
        await coordinator._price_task
        assert data["source_errors"]["price_current"] == "missing_or_stale"


async def test_runtime_rejects_changed_home_and_second_tibber_entry(coordinator, hass, service):
    await coordinator._async_update_data()
    await coordinator._price_task
    assert coordinator._price_snapshot is not None
    service[0]["prices"]["other-home"] = service[0]["prices"].pop(HOME)
    coordinator._price_next_fetch = None
    await coordinator._async_update_data()
    await coordinator._price_task
    assert coordinator._price_snapshot is None
    assert coordinator._price_provider_error == "tibber_home_mismatch"
    MockConfigEntry(domain="tibber", title="Synthetic second account").add_to_hass(hass)
    coordinator._price_next_fetch = None
    await coordinator._async_update_data()
    await coordinator._price_task
    assert coordinator._price_provider_error == "tibber_config_entries"


async def test_short_native_ttl_refreshes_before_expiry(coordinator, hass, clock, service):
    hass.config_entries.async_update_entry(coordinator.entry, options={**coordinator.entry.options, "price_max_age": 300})
    await coordinator._async_update_data()
    await coordinator._price_task
    assert (coordinator._price_next_fetch - dt_util.utcnow()).total_seconds() == 150
    clock.tick(timedelta(seconds=149))
    await coordinator._async_update_data()
    assert len(service[1]) == 1
    clock.tick(timedelta(seconds=1))
    await coordinator._async_update_data()
    await coordinator._price_task
    assert len(service[1]) == 2
    assert coordinator.data["source_errors"] == {}


async def test_replacement_account_with_same_home_name_does_not_take_over(coordinator):
    await coordinator._async_update_data()
    await coordinator._price_task
    replacement = replace(coordinator._price_snapshot, entry_id="replacement-account")
    coordinator._price_next_fetch = None
    with patch("custom_components.opti_akku.coordinator.async_fetch_prices", return_value={HOME: replacement}):
        await coordinator._async_update_data()
        await coordinator._price_task
    assert coordinator._price_snapshot is None
    assert coordinator._price_provider_error == "tibber_entry_mismatch"


@pytest.mark.parametrize("expire", ["ttl", "interval", "entry", "provider"])
async def test_final_write_guard_rechecks_native_snapshot_without_hardware(coordinator, hass, clock, expire):
    # Capture the guard with an inert test double. No physical write exists.
    await coordinator._async_update_data()
    await coordinator._price_task
    coordinator.shadow_mode = False
    coordinator.write_enabled = True
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator._price_next_fetch = dt_util.utcnow() + timedelta(hours=1)
    options = dict(coordinator.entry.options)
    if expire == "ttl":
        options["price_max_age"] = 60
        hass.config_entries.async_update_entry(coordinator.entry, options=options)
        clock.tick(timedelta(seconds=59))
    elif expire == "interval":
        clock.move_to(datetime(2026, 9, 13, 8, 59, 59, tzinfo=BERLIN))
    checked = []

    async def capture(_mode, _params, current):
        assert current()
        if expire == "entry":
            MockConfigEntry(domain="tibber", title="Synthetic second").add_to_hass(hass)
        elif expire == "provider":
            hass.config_entries.async_update_entry(coordinator.entry, options={**coordinator.entry.options, "price_provider": "entities"})
        else:
            clock.tick(timedelta(seconds=2))
        checked.append(current())

    coordinator.device.async_apply.side_effect = capture
    await coordinator._async_update_data()
    assert checked == [False]
    coordinator.write_enabled = False
    coordinator.device.async_apply.reset_mock()


@pytest.mark.freeze_time(tick=True, real_asyncio=True)
async def test_native_prices_remain_write_free_through_real_ha_tcp_shadow_lifecycle(hass, service, socket_enabled):
    from tests.simulator import SimulatedInverter

    simulator = await SimulatedInverter().start()
    entry = MockConfigEntry(domain="opti_akku", title="Synthetic native Shadow",
        data={"host": "127.0.0.1", "port": simulator.port, "unit_id": 3,
              "profile": "sma_stp_se", "shadow_mode": True},
        options={**NATIVE, "single_inverter": True, "sources": {}})
    entry.add_to_hass(hass)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        runtime = entry.runtime_data
        await runtime._price_task
        assert runtime.data["states"]["sensor.opti_price_current_ct_kwh"] == "25.0"
        runtime.settings["input_boolean.akku_opti_automatik"] = True
        runtime.write_enabled = True  # Shadow's immutable driver boundary must still hold.
        await runtime.async_refresh()
        assert simulator.writes == []
        assert await hass.config_entries.async_reload(entry.entry_id)
        await entry.runtime_data._price_task
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert simulator.writes == []
    finally:
        if getattr(entry, "runtime_data", None) is not None:
            await hass.config_entries.async_unload(entry.entry_id)
        await simulator.close()
