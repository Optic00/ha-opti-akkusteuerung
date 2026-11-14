"""Standby reserve before a planned grid charge (issue #222)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.alerts import SMA_STANDBY_STATUS_CODES
from custom_components.opti_akku.battery_model import BatteryModel
from custom_components.opti_akku.engine import Evaluation
from custom_components.opti_akku.standby_reserve import (
    STANDBY_RESERVE_DECISION,
    apply_standby_reserve,
    grid_charge_blocked,
    next_grid_charge,
)

TZ = ZoneInfo("Europe/Berlin")
# Lossless and free: the precharge condition reduces to window, spread and peaks.
NEUTRAL = BatteryModel(1.0, 1.0, 0.0, 0.0, "configured")


def local(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).astimezone(UTC)


def day_prices(default, *ranges):
    """96 quarter-hour prices; ranges are ((h, m), (h, m), price), end exclusive."""
    values = [default] * 96
    for (h1, m1), (h2, m2), price in ranges:
        for index in range((h1 * 60 + m1) // 15, (h2 * 60 + m2) // 15):
            values[index] = price
    return values


def series(cheap_from=(2, 0), cheap_to=(4, 0), peak_from=(7, 0), peak_to=(9, 0)):
    """Evening 32 ct, a cheap window at 22 ct and a 45 ct morning peak."""
    today = day_prices(32.0, ((17, 0), (20, 0), 40.0))
    tomorrow = day_prices(32.0, (peak_from, peak_to, 45.0), (cheap_from, cheap_to, 22.0))
    return {"today": today, "tomorrow": tomorrow}


def window(**overrides):
    args = dict(now=local(14, 23, 5), timezone=TZ, price_series=series(),
                horizon_end=local(15, 10, 0).isoformat(), sunrise=local(15, 7, 40).isoformat(),
                grid_charging=True, peak_reserve_active=True, surcharge_ct=5, spread_ct=10,
                feed_in_ct=7, maxsoc=95, battery_model=NEUTRAL)
    args.update(overrides)
    return next_grid_charge(**args)


def test_cheap_night_window_before_sunrise_is_planned():
    result = window()
    assert result["status"] == "planned"
    assert result["kind"] == "grid_precharge"
    assert result["at"] == local(15, 2, 0).isoformat()
    assert result["sunrise"] == local(15, 7, 40).isoformat()


def test_window_after_sunrise_is_not_planned():
    # The cheap slot lies after sunrise: PV wakes the inverter before that.
    # Without the cheap slot the strategy would charge right away, so the
    # same prices with that slot before sunrise are planned.
    late = series(cheap_from=(7, 45), cheap_to=(8, 15), peak_from=(8, 30), peak_to=(10, 0))
    result = window(price_series=late, horizon_end=local(15, 11, 0).isoformat())
    assert result["status"] == "none"
    assert window(price_series=late, horizon_end=local(15, 11, 0).isoformat(),
                  sunrise=local(15, 8, 0).isoformat())["status"] == "planned"


def test_too_small_spread_plans_no_window():
    assert window(spread_ct=30)["status"] == "none"


def test_uneconomic_window_with_default_model_is_not_planned():
    # 45 ct does not pay for 22 ct grid energy plus losses, wear and margin
    # under a strict model; the strategy would not precharge either.
    strict = BatteryModel(0.9, 0.9, 30.0, 10.0, "configured")
    assert window(battery_model=strict)["status"] == "none"


@pytest.mark.parametrize("overrides,status", [
    ({"grid_charging": False}, "grid_charging_off"),
    ({"peak_reserve_active": False}, "none"),
    ({"sunrise": None}, "data_missing"),
    ({"horizon_end": "kaputt"}, "data_missing"),
    ({"price_series": {}}, "data_missing"),
    ({"surcharge_ct": None}, "data_missing"),
])
def test_missing_preconditions_plan_nothing(overrides, status):
    assert window(**overrides)["status"] == status


@pytest.mark.parametrize("peak_reserve_active", [True, False])
def test_price_below_feed_in_before_sunrise_needs_no_peak(peak_reserve_active):
    """First grid branch of decisions.yaml: independent of peaks, also after the last one."""
    prices = {"today": day_prices(32.0), "tomorrow": day_prices(32.0, ((5, 0), (5, 30), 6.0))}
    result = window(price_series=prices, peak_reserve_active=peak_reserve_active, surcharge_ct=None)
    assert (result["status"], result["kind"]) == ("planned", "grid_below_feed_in")
    assert result["at"] == local(15, 5, 0).isoformat()
    # The same slot after sunrise: PV wakes the inverter first.
    assert window(price_series=prices, peak_reserve_active=peak_reserve_active,
                  sunrise=local(15, 4, 50).isoformat())["status"] == "none"


PLANNED = {"status": "planned", "at": local(15, 2, 0).isoformat(), "kind": "grid_precharge"}


def evaluation(mode="Akku Dynamisch", soc=3, minsoc=0, sun="below_horizon", decision_id="above_target"):
    return Evaluation(
        {"sensor.opti_strategie_vorschau": mode, "sensor.opti_soc": soc,
         "input_number.minsoc": minsoc, "sun.sun": sun},
        {"sensor.opti_strategie_vorschau": {"grund": "ueber Ziel-SoC"}},
        mode, "ueber Ziel-SoC", {}, decision_id=decision_id)


def apply(result=None, *, reserve=3, backend="sma", plan=PLANNED, latched=False, eligible=True):
    return apply_standby_reserve(result or evaluation(), reserve_soc=reserve, backend=backend,
                                 window=plan, latched=latched, eligible=eligible, timezone=TZ)


@pytest.mark.parametrize("mode", ["Akku Dynamisch", "Akku nur Entladen", "Akku Automatisch"])
def test_reserve_blocks_discharge_when_grid_charge_is_planned(mode):
    held, report, latched = apply(evaluation(mode))
    assert held.mode == "Akku nur Laden"
    assert held.decision_id == STANDBY_RESERVE_DECISION
    assert held.reason == "Standby-Reserve (3 %, Netzladen ab 02:00 geplant)"
    assert held.states["sensor.opti_strategie_vorschau"] == "Akku nur Laden"
    assert held.attributes["sensor.opti_strategie_vorschau"]["grund"] == held.reason
    assert (report["status"], report["controls_battery"], latched) == ("holding", True, True)
    assert report["grid_charge"] == PLANNED


def test_reserve_releases_two_points_above_once_latched():
    assert apply(evaluation(soc=4))[1]["status"] == "armed"
    assert apply(evaluation(soc=5), latched=True)[1]["status"] == "holding"
    held, report, latched = apply(evaluation(soc=6), latched=True)
    assert (held.mode, report["status"], latched) == ("Akku Dynamisch", "armed", False)


@pytest.mark.parametrize("mode", ["Akku nur Laden", "Akku Pause", "Akku Netzladen"])
def test_non_discharging_modes_stay_and_keep_the_latch(mode):
    source = evaluation(mode)
    held, report, latched = apply(source)
    assert held is source
    assert (report["status"], report["controls_battery"], latched) == ("not_discharging", False, True)


@pytest.mark.parametrize("kwargs,source,status", [
    ({"reserve": 0}, None, "disabled"),
    ({"reserve": None}, None, "disabled"),
    ({"backend": "huawei_solar"}, None, "backend_not_supported"),
    ({"eligible": False}, None, "manual_or_strategy_disabled"),
    ({}, evaluation(sun="above_horizon"), "daylight"),
    ({"plan": {"status": "none"}}, None, "no_grid_charge_planned"),
    ({}, evaluation(minsoc=3), "minsoc_covers"),
    ({}, evaluation(minsoc=10, soc=10), "minsoc_covers"),
    ({}, evaluation(soc="unavailable"), "data_missing"),
])
def test_reserve_releases_without_all_preconditions(kwargs, source, status):
    source = source or evaluation()
    held, report, latched = apply(source, latched=True, **kwargs)
    assert held is source
    assert (report["status"], report["controls_battery"], latched) == (status, False, False)


def blocked_data(**overrides):
    data = {"online": True, "mode": "Akku Netzladen",
            "connection_status": {"status": "not_ready", "inverter_status": 16777213},
            "states": {"sun.sun": "below_horizon", "sensor.opti_pv_generation_w": 0,
                       "sensor.opti_battery_power_w": 0, "sensor.opti_soc": 0,
                       "input_number.minsoc": 0},
            "standby_reserve": {"reserve_soc": 3.0}}
    data.update(overrides)
    return data


def test_grid_charge_blocked_needs_standby_code_night_and_grid_request():
    assert grid_charge_blocked(blocked_data(), SMA_STANDBY_STATUS_CODES)
    night = blocked_data()["states"]
    # Up to the reserve plus its release band counts as empty.
    assert grid_charge_blocked(blocked_data(states={**night, "sensor.opti_soc": 5}), SMA_STANDBY_STATUS_CODES)
    for overrides in (
        # A safety rule replaced the grid charge wish (e.g. Pause): not this incident.
        {"mode": "Akku Pause"},
        {"states": {**night, "sensor.opti_soc": 6}},
        {"states": {**night, "sensor.opti_pv_generation_w": 120}},
        {"states": {**night, "sensor.opti_battery_power_w": -300}},
        {"states": {**night, "sensor.opti_battery_power_w": "unavailable"}},
        {"states": {**night, "input_number.minsoc": None}},
        {"online": False},
        {"connection_status": {"status": "ready", "inverter_status": 235}},
        {"connection_status": {"status": "not_ready", "inverter_status": 4242}},
        {"states": {**night, "sun.sun": "above_horizon"}},
        {"states": None},
        {"connection_status": None},
    ):
        assert not grid_charge_blocked(blocked_data(**overrides), SMA_STANDBY_STATUS_CODES)


@pytest.fixture
async def alerts(hass):
    from homeassistant.components import persistent_notification
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.opti_akku.alerts import HealthAlerts

    await hass.config.async_set_time_zone("Europe/Berlin")
    await persistent_notification.async_setup(hass, {})
    entry = MockConfigEntry(domain="opti_akku", title="Opti Test", data={}, options={})
    entry.add_to_hass(hass)
    obj = HealthAlerts(hass, entry)
    yield obj
    await obj.async_stop()


def health(**overrides):
    defaults = {"write_enabled": True, "mode": "Akku Netzladen",
                "standby_reserve": {"status": "disabled", "reserve_soc": 0.0}}
    return blocked_data(**{**defaults, **overrides})


def at(seconds):
    from datetime import timedelta
    from unittest.mock import patch

    return patch("custom_components.opti_akku.alerts.dt_util.utcnow",
                 return_value=local(15, 2, 0) + timedelta(seconds=seconds))


async def test_blocked_grid_charge_opens_a_repair_issue_until_it_works_again(hass, alerts):
    from homeassistant.helpers import issue_registry as ir

    registry = ir.async_get(hass)
    issue_id = f"grid_charge_standby_{alerts.entry.entry_id}"
    with at(0):
        data = health()
        alerts.update(data)
    assert data["connection_status"]["standby"] == "grid_charge_blocked"
    with at(119):
        alerts.update(health())
    assert registry.async_get_issue("opti_akku", issue_id) is None
    with at(120):
        alerts.update(health())
    issue = registry.async_get_issue("opti_akku", issue_id)
    assert issue is not None
    assert issue.translation_key == "grid_charge_standby"
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.is_persistent is True
    assert issue.translation_placeholders == {"entry": "Opti Test", "time": "02:00", "reserve": "0"}
    # Morning: the standby ended without grid charging; the issue stays visible.
    with at(6 * 3600):
        alerts.update(health(mode="Akku Dynamisch",
                             connection_status={"status": "ready", "inverter_status": 235},
                             states={"sun.sun": "above_horizon"}))
    assert registry.async_get_issue("opti_akku", issue_id) is not None
    # A ready device alone, or a failed grid charge command, proves nothing.
    for result in ("not_attempted", "failed", "superseded"):
        with at(20 * 3600):
            alerts.update(health(connection_status={"status": "ready", "inverter_status": 235},
                                 command_result_this_update=result,
                                 command_confirmation="idle_or_confirmed"))
        assert registry.async_get_issue("opti_akku", issue_id) is not None
    # A restart or reload keeps the issue and its lifecycle.
    from custom_components.opti_akku.alerts import HealthAlerts

    await alerts.async_stop()
    assert registry.async_get_issue("opti_akku", issue_id) is not None
    alerts = HealthAlerts(hass, alerts.entry)
    # A confirmed grid charge closes it.
    with at(24 * 3600):
        alerts.update(health(connection_status={"status": "ready", "inverter_status": 235},
                             command_result_this_update="confirmed"))
    assert registry.async_get_issue("opti_akku", issue_id) is None


async def test_no_repair_issue_without_write_permission_or_for_short_blips(hass, alerts):
    from homeassistant.helpers import issue_registry as ir

    registry = ir.async_get(hass)
    issue_id = f"grid_charge_standby_{alerts.entry.entry_id}"
    for seconds in (0, 300):
        with at(seconds):
            alerts.update(health(write_enabled=False))
    assert registry.async_get_issue("opti_akku", issue_id) is None
    with at(400):
        alerts.update(health())
    with at(450):
        alerts.update(health(connection_status={"status": "ready", "inverter_status": 235}))
    with at(600):
        alerts.update(health())
    assert registry.async_get_issue("opti_akku", issue_id) is None


async def test_removing_the_entry_removes_the_repair_issue(hass, alerts):
    from homeassistant.helpers import issue_registry as ir

    for seconds in (0, 120):
        with at(seconds):
            alerts.update(health(standby_reserve={"reserve_soc": 3.0}))
    issue_id = f"grid_charge_standby_{alerts.entry.entry_id}"
    registry = ir.async_get(hass)
    assert registry.async_get_issue("opti_akku", issue_id).translation_placeholders["reserve"] == "3"
    await alerts.async_stop()
    assert registry.async_get_issue("opti_akku", issue_id) is not None
    from custom_components.opti_akku import async_remove_entry

    await async_remove_entry(hass, alerts.entry)
    assert registry.async_get_issue("opti_akku", issue_id) is None
