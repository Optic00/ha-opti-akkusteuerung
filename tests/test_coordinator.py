"""HA lifecycle and coordinator safety with real strategy and a fake device."""

import asyncio
from datetime import timedelta
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.coordinator import OptiCoordinator, validate_setting
from custom_components.opti_akku.engine import StrategyEngine


def device():
    obj = AsyncMock()
    obj.async_probe.return_value = {"inverter_status": 235, "model": "STP10.0-3SE-40", "serial_number": "123456789"}
    obj.async_read.return_value = {
        "sensor.opti_soc": 60, "sensor.opti_battery_capacity_kwh": 12.8,
        "sensor.opti_battery_temp": 22, "sensor.opti_battery_power_w": 0,
        "sensor.opti_inverter_status": 235, "sensor.opti_house_balance_w": 800,
        "sensor.opti_pv_power_w": 2500, "sensor.opti_pv_generation_w": 2600,
        "sensor.opti_grid_export_w": 1700, "sensor.opti_grid_import_w": 0,
    }
    obj.last_write = None
    return obj


@pytest.fixture
def entry(hass):
    result = MockConfigEntry(domain="opti_akku", title="Opti Test",
        data={"host": "127.0.0.1", "port": 15020, "unit_id": 3, "profile": "sma_stp_se"},
        options={"single_writer_confirmed": True, "single_inverter": True, "sources": {}})
    result.add_to_hass(hass)
    return result


@pytest.fixture
async def coordinator(hass, entry):
    engine = await hass.async_add_executor_job(StrategyEngine)
    result = OptiCoordinator(hass, entry, device(), engine)
    await result.async_restore()
    yield result
    await result.async_stop()


async def test_default_is_observation_only(coordinator):
    data = await coordinator._async_update_data()
    assert data["mode"] == "Akku Pause"
    assert not data["write_enabled"]
    coordinator.device.async_apply.assert_not_awaited()
    assert data["states"]["sensor.opti_soc"] == "60"


async def test_write_activation_and_master_off_pause(coordinator):
    coordinator.write_enabled = True
    data = await coordinator._async_update_data()
    assert data["mode"] == "Akku Pause"
    assert coordinator.device.async_apply.await_args.args[0] == "Akku Pause"


async def test_source_failure_stops_manual_discharge(coordinator):
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.manual_mode = "Akku schnell Entladen"
    coordinator.write_enabled = True
    coordinator.device.async_read.return_value["sensor.opti_soc"] = None
    data = await coordinator._async_update_data()
    assert data["mode"] == "Akku Pause"
    assert coordinator.device.async_apply.await_args.args[0] == "Akku Pause"


async def test_min_soc_applies_to_manual_mode(coordinator):
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.manual_mode = "Akku schnell Entladen"
    coordinator.device.async_read.return_value["sensor.opti_soc"] = 5
    data = await coordinator._async_update_data()
    assert data["mode"] == "Akku Pause"


async def test_master_off_also_blocks_manual_mode(coordinator):
    coordinator.manual_mode = "Akku schnell Laden"
    coordinator.write_enabled = True
    data = await coordinator._async_update_data()
    assert data["mode"] == "Akku Pause"
    assert data["reason"] == "Steuerung deaktiviert"


@pytest.mark.parametrize("mode", ["Akku Automatisch", "Akku Dynamisch"])
@pytest.mark.parametrize("soc_values,blocked", [
    ([95, 94.9, 95, 93, 92], "Akku nur Entladen"),
    ([10, 10.1, 10, 12, 13], "Akku nur Laden"),
])
async def test_manual_soc_hysteresis(coordinator, mode, soc_values, blocked):
    coordinator.manual_mode = mode
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    for index, soc in enumerate(soc_values):
        coordinator.device.async_read.return_value["sensor.opti_soc"] = soc
        data = await coordinator._async_update_data()
        assert data["mode"] == (mode if index == len(soc_values) - 1 else blocked)


@pytest.mark.parametrize("mode", ["Akku schnell Laden", "Akku 0.2C Laden", "Akku Automatisch"])
async def test_hot_manual_presets_use_computed_charge_ceiling(coordinator, mode):
    from custom_components.opti_akku.sma import SmaDevice

    coordinator.manual_mode = mode
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.write_enabled = True
    coordinator.device.async_read.return_value["sensor.opti_battery_temp"] = 45
    data = await coordinator._async_update_data()
    params = coordinator.device.async_apply.await_args.args[1]
    ceiling = float(data["states"]["sensor.opti_charge_power_w"])
    assert 0 < ceiling < 2000
    assert params["max_charge_w"] == ceiling
    assert params["charge_setpoint_w"] <= ceiling
    driver = SmaDevice(Mock())
    driver._model_id = 19051
    encoded = driver._parameters(mode, params)
    assert encoded["power_02c_w"] <= ceiling


async def test_read_disconnect_does_not_reuse_telemetry(coordinator):
    await coordinator._async_update_data()
    coordinator.write_enabled = True
    coordinator.device.async_read.side_effect = TimeoutError
    data = await coordinator._async_update_data()
    assert data["states"]["sensor.opti_soc"] == "unavailable"
    assert data["mode"] == "Akku Pause"
    assert not data["online"]
    coordinator.device.async_apply.assert_not_awaited()


async def test_write_requires_explicit_single_writer_confirmation(coordinator, hass, entry):
    await coordinator._async_update_data()
    hass.config_entries.async_update_entry(entry, options={"single_writer_confirmed": False})
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_write_enabled(True)
    assert not coordinator.write_enabled


async def test_ordered_limits_reject_invalid_settings(coordinator):
    with pytest.raises(HomeAssistantError):
        validate_setting("input_number.minsoc", 99, coordinator.settings)
    with pytest.raises(HomeAssistantError):
        validate_setting("input_number.maxsoc", float("nan"), coordinator.settings)


async def test_actual_ha_entry_and_entities(hass, entry):
    inverter = device()
    with patch("custom_components.opti_akku.async_get_unit"), patch("custom_components.opti_akku.SmaDevice", return_value=inverter):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entities = [s for s in hass.states.async_all() if s.entity_id.startswith(("sensor.opti", "number.opti", "switch.opti", "select.opti"))]
        assert {s.domain for s in entities} >= {"sensor", "switch", "select"}
        assert not any(s.domain == "number" for s in entities)
        assert not any(s.domain in ("input_boolean", "input_number", "input_select", "automation") for s in hass.states.async_all())
        inverter.async_apply.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_ha_reload_restores_settings_and_ids_without_forced_mode(hass, entry):
    inverter = device()
    with patch("custom_components.opti_akku.async_get_unit"), patch("custom_components.opti_akku.SmaDevice", return_value=inverter):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        before = entry.runtime_data
        entities_before = {state.entity_id for state in hass.states.async_all()}
        await before.async_set_setting("input_number.maxsoc", 91)
        await before.async_set_setting("input_boolean.akku_opti_automatik", True)
        await before.async_set_mode("Akku schnell Entladen")
        await before.async_set_write_enabled(True)
        before._manual_charge_blocked = True
        before._manual_discharge_blocked = True
        inverter.async_apply.reset_mock()

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        after = entry.runtime_data
        assert after is not before
        assert before._stopped
        assert after.settings["input_number.maxsoc"] == 91
        assert after.settings["input_boolean.akku_opti_automatik"] is True
        assert after.write_enabled is True
        assert after.manual_mode is None
        assert after._manual_charge_blocked is True
        assert after._manual_discharge_blocked is True
        assert {state.entity_id for state in hass.states.async_all()} == entities_before
        assert inverter.async_apply.await_args_list[0].args[0] == "Akku Pause"
        assert all(call.args[0] != "Akku schnell Entladen" for call in inverter.async_apply.await_args_list)
        assert hass.data["opti_akku_serial_owners"]["123456789"] == entry.entry_id
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_normal_ha_stop_pauses_once(coordinator, hass):
    coordinator.write_enabled = True
    await coordinator._async_update_data()
    coordinator.device.async_apply.reset_mock()
    await hass.async_stop()
    await hass.async_block_till_done()
    coordinator.device.async_apply.assert_awaited_once()
    assert coordinator.device.async_apply.await_args.args[0] == "Akku Pause"
    await coordinator.async_stop()
    coordinator.device.async_apply.assert_awaited_once()


async def test_reprobe_missing_expected_identity_blocks_writes(coordinator, hass, entry):
    hass.config_entries.async_update_entry(entry, data={**entry.data, "serial_number": "123456789"})
    await coordinator._async_update_data()
    coordinator.write_enabled = True
    coordinator.device.async_probe.return_value["serial_number"] = None
    data = await coordinator._async_update_data()
    assert data["online"] is False
    coordinator.device.async_apply.assert_not_awaited()


async def test_same_serial_cannot_have_second_controller(coordinator, hass):
    await coordinator._async_update_data()
    second = MockConfigEntry(domain="opti_akku", data={"host": "inverter.local", "port": 502, "unit_id": 3})
    second.add_to_hass(hass)
    second_coordinator = OptiCoordinator(hass, second, device(), StrategyEngine())
    try:
        data = await second_coordinator._async_update_data()
        assert not data["online"]
        assert hass.data["opti_akku_serial_owners"]["123456789"] == coordinator.entry.entry_id
    finally:
        await second_coordinator.async_stop()
    assert hass.data["opti_akku_serial_owners"]["123456789"] == coordinator.entry.entry_id


async def test_manual_ev_blocks_discharge_but_preserves_charging(coordinator):
    await coordinator._async_update_data()
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.settings["input_boolean.opti_ev_akku_pause"] = True
    states = {**coordinator.device.async_read.return_value,
              "sensor.opti_house_consumption_w": 800,
              "binary_sensor.opti_ev_schnellladung": "on"}
    coordinator.manual_mode = "Akku schnell Entladen"
    assert coordinator._safe_mode(coordinator.manual_mode, states)[0] == "Akku nur Laden"
    coordinator.manual_mode = "Akku Netzladen"
    assert coordinator._safe_mode(coordinator.manual_mode, states)[0] == "Akku Netzladen"


@pytest.mark.parametrize("requested,temperature,soc,ev,expected", [
    ("Akku Automatisch", 22, 10, False, "Akku nur Laden"),
    ("Akku Dynamisch", 22, 10, False, "Akku nur Laden"),
    ("Akku Automatisch", 22, 95, False, "Akku nur Entladen"),
    ("Akku Dynamisch", 22, 95, False, "Akku nur Entladen"),
    ("Akku Automatisch", 0, 60, False, "Akku nur Entladen"),
    ("Akku Dynamisch", 0, 60, False, "Akku nur Entladen"),
    ("Akku Automatisch", 0, 10, False, "Akku Pause"),
    ("Akku Dynamisch", 50, 10, False, "Akku Pause"),
    ("Akku Automatisch", 22, 95, True, "Akku Pause"),
    ("Akku Dynamisch", 0, 60, True, "Akku Pause"),
    ("Akku schnell Entladen", 22, 95, True, "Akku Pause"),
    ("Akku nur Entladen", 0, 60, True, "Akku Pause"),
    ("Akku Dynamisch", 22, 60, True, "Akku nur Laden"),
    ("Akku nur Laden", 0, 60, False, "Akku Pause"),
    ("Akku schnell Laden", 4.9, 60, False, "Akku Pause"),
    ("Akku Netzladen", 50, 60, False, "Akku Pause"),
    ("Akku 0.2C Laden", 50, 60, False, "Akku Pause"),
    ("Akku nur Laden", 5, 60, False, "Akku nur Laden"),
    ("Akku schnell Laden", 49.9, 60, False, "Akku schnell Laden"),
    ("Akku nur Entladen", 0, 60, False, "Akku nur Entladen"),
    ("Akku Pause", 22, 60, True, "Akku Pause"),
])
async def test_manual_direction_limits_combine_before_writing(
    coordinator, requested, temperature, soc, ev, expected
):
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.settings["input_boolean.opti_ev_akku_pause"] = ev
    coordinator.manual_mode = requested
    coordinator.write_enabled = True
    coordinator.device.async_read.return_value.update({
        "sensor.opti_battery_temp": temperature,
        "sensor.opti_soc": soc,
    })
    # Keep the genuine engine/temperature calculation. Only the external EV
    # latch is supplied directly so these cases do not depend on its delay.
    original = coordinator.engine.evaluate

    def evaluate(*args, **kwargs):
        result = original(*args, **kwargs)
        result.states["binary_sensor.opti_ev_schnellladung"] = "on" if ev else "off"
        return result

    with patch.object(coordinator.engine, "evaluate", side_effect=evaluate):
        data = await coordinator._async_update_data()
    assert data["mode"] == expected
    assert coordinator.device.async_apply.await_args.args[0] == expected
    if expected != requested:
        assert data["reason"] != "Manuelle Auswahl"


async def test_manual_temperature_policy_does_not_change_strategy_derating(coordinator):
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.device.async_read.return_value["sensor.opti_battery_temp"] = 0
    data = await coordinator._async_update_data()
    assert float(data["states"]["sensor.opti_charge_power_w"]) > 0
    assert "manuell" not in data["reason"].lower()


async def test_normal_power_updates_do_not_starve_write_sequence(coordinator, hass, entry):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "sources": {"house_consumption": "sensor.house"}})
    hass.states.async_set("sensor.house", "800", {"unit_of_measurement": "W"})
    coordinator.async_listen_sources()
    coordinator.write_enabled = True

    async def apply(mode, params, current):
        assert current()
        for watts in (810, 820, 830):
            hass.states.async_set("sensor.house", str(watts), {"unit_of_measurement": "W"})
            await asyncio.sleep(0)
            assert current(), "A fresh power sample must not cancel every write"
        hass.states.async_set("sensor.house", "unavailable", {"unit_of_measurement": "W"})
        await asyncio.sleep(0)
        assert not current(), "Data loss must invalidate the pending command"

    coordinator.device.async_apply.side_effect = apply
    await coordinator._async_update_data()
    coordinator.device.async_apply.side_effect = None
    coordinator.write_enabled = False
    await coordinator.async_stop()
    coordinator._unsubscribe()  # HA entry callbacks can run after the explicit stop.


async def test_ev_freshness_expires_without_new_state_event(coordinator, hass, entry):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "source_max_age": 1,
        "sources": {"ev1_mode": "select.car", "ev1_charging": "binary_sensor.car"}})
    hass.states.async_set("select.car", "pv")
    hass.states.async_set("binary_sensor.car", "off")
    coordinator.write_enabled = True

    async def apply(mode, params, current):
        assert current()
        later = dt_util.utcnow() + timedelta(seconds=2)
        with patch("custom_components.opti_akku.coordinator.dt_util.utcnow", return_value=later):
            assert not current()

    coordinator.device.async_apply.side_effect = apply
    await coordinator._async_update_data()
    coordinator.device.async_apply.side_effect = None
    coordinator.write_enabled = False

async def test_wizard_settings_seed_once_and_entity_changes_survive(coordinator, hass, entry):
    hass.config_entries.async_update_entry(entry, options={**entry.options,
        "settings_revision": "wizard-1", "settings": {"input_number.maxsoc": 89}})
    await coordinator.async_restore()
    assert coordinator.settings["input_number.maxsoc"] == 89
    await coordinator.async_set_setting("input_number.maxsoc", 92)
    await coordinator.async_restore()
    assert coordinator.settings["input_number.maxsoc"] == 92


async def test_invalid_stored_value_preserves_other_settings_and_disarms(coordinator):
    await coordinator._store.async_save({"settings": {"input_number.minsoc": "bad",
        "input_number.maxsoc": 91, "input_number.opti_forecast_optimismus": 40},
        "write_enabled": True, "writer_binding": coordinator._writer_binding})
    await coordinator.async_restore()
    assert coordinator.settings["input_number.maxsoc"] == 91
    assert coordinator.settings["input_number.opti_forecast_optimismus"] == 40
    assert coordinator.settings["input_number.minsoc"] == 10
    assert coordinator.write_enabled is False
    assert "minsoc" in coordinator._last_error


@pytest.mark.parametrize("binding", [None, ["other", 502, 3, "other-serial"]])
async def test_write_permission_is_bound_to_saved_device(coordinator, binding):
    await coordinator._store.async_save({"write_enabled": True, "writer_binding": binding})
    await coordinator.async_restore()
    assert coordinator.write_enabled is False


@pytest.mark.parametrize("cleanup_failed", [False, True])
async def test_superseded_command_only_alarms_if_cleanup_failed(coordinator, cleanup_failed):
    from custom_components.opti_akku.sma import StaleCommandError
    coordinator.write_enabled = True
    coordinator._last_apply = time.monotonic()
    confirmed = coordinator._last_apply
    err = StaleCommandError("newer prices")
    err.cleanup_failed = cleanup_failed
    coordinator.device.async_apply.side_effect = err
    result = await coordinator._async_update_data()
    assert bool(result["last_error"]) == cleanup_failed
    assert ("write" in coordinator.alerts._active) == cleanup_failed
    assert coordinator._last_apply == confirmed
    assert coordinator._last_signature is None


async def test_notification_choice_does_not_reload_writer(hass, entry, coordinator):
    from custom_components.opti_akku import _async_options_updated
    entry.runtime_data = coordinator
    hass.config_entries.async_update_entry(entry, options={**entry.options, "notification_service": "notify.phone"})
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        await _async_options_updated(hass, entry)
    reload.assert_not_awaited()
    coordinator.device.async_apply.assert_not_awaited()


async def test_startup_does_not_claim_240_seconds_of_stall(coordinator):
    coordinator.write_enabled = True
    coordinator.device.async_read.side_effect = OSError("offline")
    result = await coordinator._async_update_data()
    assert result["states"]["binary_sensor.opti_write_stalled"] == "off"
    coordinator._write_monitor_since -= 241
    result = await coordinator._async_update_data()
    assert result["states"]["binary_sensor.opti_write_stalled"] == "on"


async def test_reconcile_uses_monotonic_time_across_utc_rollback(coordinator):
    from custom_components.opti_akku.const import RECONCILE_SECONDS

    clock = [100.0]
    coordinator.write_enabled = True
    with patch("custom_components.opti_akku.coordinator.time.monotonic",
               side_effect=lambda: clock[0]):
        await coordinator._async_update_data()
        coordinator.device.async_apply.reset_mock()
        clock[0] += RECONCILE_SECONDS
        rolled_back = dt_util.utcnow() - timedelta(hours=1)
        with patch("custom_components.opti_akku.coordinator.dt_util.utcnow",
                   return_value=rolled_back):
            await coordinator._async_update_data()
    coordinator.device.async_apply.assert_awaited_once()


async def test_write_stall_uses_monotonic_time_across_utc_rollback(coordinator):
    clock = [100.0]
    coordinator.write_enabled = True
    with patch("custom_components.opti_akku.coordinator.time.monotonic",
               side_effect=lambda: clock[0]):
        await coordinator._async_update_data()
        clock[0] += 241
        coordinator.device.async_read.side_effect = OSError("offline")
        with patch("custom_components.opti_akku.coordinator.dt_util.utcnow",
                   return_value=dt_util.utcnow() - timedelta(hours=1)):
            result = await coordinator._async_update_data()
    assert result["states"]["binary_sensor.opti_write_stalled"] == "on"


async def test_block_violation_uses_monotonic_time_across_utc_rollback(coordinator):
    clock = [100.0]
    coordinator.write_enabled = True
    coordinator.device.async_read.return_value["sensor.opti_battery_power_w"] = -500
    with patch("custom_components.opti_akku.coordinator.time.monotonic",
               side_effect=lambda: clock[0]):
        await coordinator._async_update_data()
        clock[0] += 1
        await coordinator._async_update_data()
        clock[0] += 30
        with patch("custom_components.opti_akku.coordinator.dt_util.utcnow",
                   return_value=dt_util.utcnow() - timedelta(hours=1)):
            result = await coordinator._async_update_data()
    assert result["states"]["binary_sensor.opti_block_violation"] == "on"


async def test_write_read_guard_uses_monotonic_elapsed_time(coordinator):
    clock = [100.0]
    accepted = []

    async def delayed_apply(_mode, _params, current):
        clock[0] += 31
        accepted.append(current())

    coordinator.write_enabled = True
    coordinator.device.async_apply.side_effect = delayed_apply
    with patch("custom_components.opti_akku.coordinator.time.monotonic",
               side_effect=lambda: clock[0]), patch(
                   "custom_components.opti_akku.coordinator.dt_util.utcnow",
                   return_value=dt_util.utcnow() - timedelta(hours=1)):
        await coordinator._async_update_data()
    assert accepted == [False]


async def test_plant_balance_feeds_one_profile_and_keeps_inverter_ac(coordinator, hass):
    options = {**coordinator.entry.options, "plant_mode": "balance", "plant_meter_confirmed": True,
               "additional_ac_sources": ["sensor.second_ac"], "excluded_load_sources": ["sensor.ev_load"],
               "forecast_min_load_w": 150}
    hass.config_entries.async_update_entry(coordinator.entry, options=options)
    hass.states.async_set("sensor.second_ac", "1", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.ev_load", "500", {"unit_of_measurement": "W"})
    data = await coordinator._async_update_data()
    assert float(data["states"]["sensor.opti_house_raw_w"]) == 1800
    assert float(data["states"]["sensor.opti_house_consumption_w"]) == 1800
    assert float(data["states"]["sensor.opti_house_consumption_60min_w"]) == 1300
    assert float(data["states"]["sensor.opti_pv_power_w"]) == 2500
    assert "sensor.opti_house_consumption_60min_w" not in coordinator.engine.snapshot()["samples"]
    assert data["load_profile"]["warming_up"] is True
    hass.states.async_set("sensor.second_ac", "unavailable", {"unit_of_measurement": "kW"})
    data = await coordinator._async_update_data()
    assert data["states"]["sensor.opti_house_consumption_w"] == "unavailable"
    assert data["states"]["sensor.opti_house_consumption_60min_w"] == "unavailable"
    assert data["source_errors"]["plant:sensor.second_ac"] == "invalid_value"


async def test_plant_source_invalidated_during_write_cancels_guard(coordinator, hass):
    hass.config_entries.async_update_entry(coordinator.entry, options={**coordinator.entry.options,
        "plant_mode": "balance", "plant_meter_confirmed": True, "forecast_min_load_w": 0,
        "additional_ac_sources": ["sensor.second_ac"]})
    hass.states.async_set("sensor.second_ac", 500, {"unit_of_measurement": "W"})
    coordinator.write_enabled = True
    async def apply(mode, params, current):
        assert current()
        hass.states.async_set("sensor.second_ac", "unavailable", {"unit_of_measurement": "W"})
        assert not current()
    coordinator.device.async_apply.side_effect = apply
    await coordinator._async_update_data()
    coordinator.device.async_apply.assert_awaited_once()


async def test_disabled_strategy_observes_without_engine_or_provider(hass, entry):
    hass.config_entries.async_update_entry(entry, options={"strategy_enabled": False, "sources": {},
        "single_writer_confirmed": True, "price_provider": "tibber"})
    engine = await hass.async_add_executor_job(StrategyEngine)
    coordinator = OptiCoordinator(hass, entry, device(), engine)
    await coordinator.async_restore()
    try:
        with patch.object(engine, "evaluate", side_effect=AssertionError("Strategy must not run")):
            data = await coordinator._async_update_data()
        assert data["mode"] == "Beobachtung"
        assert not data["source_errors"]
        assert coordinator._price_task is None
        coordinator.device.async_apply.assert_not_awaited()
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_write_enabled(True)
    finally:
        await coordinator.async_stop()


async def test_manual_adapter_without_strategy_needs_no_house_and_keeps_soc_guard(hass, entry):
    hass.config_entries.async_update_entry(entry, options={"strategy_enabled": False, "sources": {},
        "single_writer_confirmed": True})
    engine = await hass.async_add_executor_job(StrategyEngine)
    coordinator = OptiCoordinator(hass, entry, device(), engine)
    await coordinator.async_restore()
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    try:
        await coordinator.async_set_mode("Akku schnell Entladen")
        await coordinator.async_set_write_enabled(True)
        assert coordinator.device.async_apply.await_args.args[0] == "Akku schnell Entladen"
        coordinator.device.async_read.return_value["sensor.opti_soc"] = 1
        data = await coordinator._async_update_data()
        assert data["mode"] == "Akku Pause"
        assert coordinator.device.async_apply.await_args.args[0] == "Akku Pause"
        assert "Akku Dynamisch" not in coordinator.supported_modes
        await coordinator.async_set_mode("Beobachtung")
        coordinator.device.async_apply.reset_mock()
        await coordinator._async_update_data()
        coordinator.device.async_apply.assert_not_awaited()
        assert not coordinator.write_enabled
    finally:
        await coordinator.async_stop()


async def test_manual_without_strategy_preserves_ev_lock_and_release_delay(hass, entry):
    hass.config_entries.async_update_entry(entry, options={"strategy_enabled": False, "single_writer_confirmed": True,
        "sources": {"ev1_mode": "select.ev", "ev1_charging": "binary_sensor.ev"}})
    hass.states.async_set("select.ev", "now")
    hass.states.async_set("binary_sensor.ev", "on")
    engine = await hass.async_add_executor_job(StrategyEngine)
    coordinator = OptiCoordinator(hass, entry, device(), engine)
    await coordinator.async_restore()
    coordinator.settings.update({"input_boolean.akku_opti_automatik": True, "input_boolean.opti_ev_akku_pause": True})
    try:
        coordinator.manual_mode = "Akku schnell Entladen"
        data = await coordinator._async_update_data()
        assert data["mode"] != "Akku schnell Entladen"
        assert data["states"]["binary_sensor.opti_ev_schnellladung"] == "on"
        hass.states.async_set("binary_sensor.ev", "unavailable")
        data = await coordinator._async_update_data()
        assert data["states"]["binary_sensor.opti_ev_schnellladung"] == "on"
        now = dt_util.utcnow()
        states = {"binary_sensor.opti_ev_lp1_schnell": "off", "binary_sensor.opti_ev_lp2_schnell": "off"}
        coordinator._add_manual_ev_safety(states, now)
        coordinator._add_manual_ev_safety(states, now + timedelta(seconds=299))
        assert states["binary_sensor.opti_ev_schnellladung"] == "on"
        coordinator._add_manual_ev_safety(states, now + timedelta(seconds=300))
        assert states["binary_sensor.opti_ev_schnellladung"] == "off"
    finally:
        await coordinator.async_stop()


async def test_invalid_plant_options_are_diagnosed_without_profile_exception(coordinator, hass):
    hass.config_entries.async_update_entry(coordinator.entry, options={**coordinator.entry.options,
        "plant_mode": "balance", "plant_meter_confirmed": True, "forecast_min_load_w": 5001})
    data = await coordinator._async_update_data()
    assert data["source_errors"]["plant:forecast_min_load_w"] == "invalid_value"
    assert data["mode"] == "Akku Pause"
    assert data["load_profile"]["valid"] is False


async def test_manual_write_permission_is_not_restored_as_strategy_permission(hass, entry):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "strategy_enabled": False})
    engine = await hass.async_add_executor_job(StrategyEngine)
    old = OptiCoordinator(hass, entry, device(), engine)
    await old.async_restore()
    old.write_enabled = True
    saved = old._stored_data()
    assert saved["strategy_enabled"] is False
    old.write_enabled = False
    await old.async_stop()
    hass.config_entries.async_update_entry(entry, options={**entry.options, "strategy_enabled": True})
    new = OptiCoordinator(hass, entry, device(), await hass.async_add_executor_job(StrategyEngine))
    await new._store.async_save(saved)
    try:
        await new.async_restore()
        assert new.manual_mode is None
        assert new.write_enabled is False
        await new._async_update_data()
        new.device.async_apply.assert_not_awaited()
    finally:
        await new.async_stop()


async def test_unproven_control_release_is_visible(coordinator, entry):
    from custom_components.opti_akku.sensor import OptiAkkuDiagnosticSensor
    coordinator.device.supports_control_release = False
    data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(data)
    entry.runtime_data = coordinator
    sensor = OptiAkkuDiagnosticSensor(entry, "control_release")
    assert sensor.native_value == "not_supported"
    assert "not_confirmed" in sensor.options
    coordinator.device.supports_control_release = True
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    assert sensor.native_value == "not_confirmed"


async def test_backward_clock_keeps_coordinator_safety_evaluation(coordinator, hass):
    hass.config_entries.async_update_entry(coordinator.entry, options={**coordinator.entry.options,
        "plant_mode": "balance", "plant_meter_confirmed": True, "forecast_min_load_w": 0})
    coordinator.write_enabled = True
    await coordinator._async_update_data()
    coordinator.device.async_apply.reset_mock()
    coordinator.device.async_read.return_value["sensor.opti_soc"] = 1
    with patch("custom_components.opti_akku.coordinator.dt_util.utcnow", return_value=dt_util.utcnow() - timedelta(minutes=2)):
        data = await coordinator._async_update_data()
    assert data["mode"] == "Akku Pause"
    assert data["load_profile"]["warming_up"] is True
    assert data["load_profile"]["coverage_seconds"] == 0
    coordinator.device.async_apply.assert_awaited_once()
    assert coordinator.device.async_apply.await_args.args[0] == "Akku Pause"


async def test_pending_huawei_phase_does_not_refresh_confirmation_timestamp(coordinator):
    from custom_components.opti_akku.device import PendingCommandError
    coordinator.write_enabled = True
    coordinator.device.async_apply.side_effect = PendingCommandError("pending")
    result = await coordinator._async_update_data()
    assert result["command_confirmation"] == "pending"
    assert coordinator._last_apply is None
    assert coordinator._last_signature is None
    coordinator.device.async_apply.side_effect = None
    result = await coordinator._async_update_data()
    assert result["command_confirmation"] == "idle_or_confirmed"
    assert coordinator._last_apply is not None


async def test_huawei_writer_binding_changes_when_actuators_change(hass, entry):
    hass.config_entries.async_update_entry(entry, data={**entry.data, "backend": "huawei_solar", "huawei_entry_id": "provider",
                  "huawei_device_id": "inverter", "huawei_controls": {"mode": "select.old"}})
    engine = await hass.async_add_executor_job(StrategyEngine)
    old = OptiCoordinator(hass, entry, device(), engine)
    snapshot = {"write_enabled": True, "writer_binding": old._writer_binding, "strategy_enabled": True}
    hass.config_entries.async_update_entry(entry, data={**entry.data, "huawei_controls": {"mode": "select.new"}})
    new = OptiCoordinator(hass, entry, device(), engine)
    new._store.async_load = AsyncMock(return_value=snapshot)
    await new.async_restore()
    assert not new.write_enabled
    await old.async_stop()
    await new.async_stop()


async def test_offline_disable_persists_owed_huawei_pause_and_reconnect_finishes(hass, entry):
    class Phased:
        PHASED = True
        supported_modes = ("Akku Pause",)
        supports_control_release = False
    dev = Phased()
    dev.async_probe = AsyncMock(return_value={"serial_number": "123456789", "inverter_status": 235})
    dev.async_read = device().async_read
    dev.async_apply = AsyncMock()
    dev.async_shutdown_control = AsyncMock()
    engine = await hass.async_add_executor_job(StrategyEngine)
    c = OptiCoordinator(hass, entry, dev, engine)
    await c.async_restore()
    c.write_enabled = True
    c._pause_pending = True
    c._pause_binding = c._writer_binding
    c._online = False
    # Keep the disable-time refresh offline; no command is sent yet.
    dev.async_probe.side_effect = RuntimeError("offline")
    await c.async_set_write_enabled(False)
    assert c._stored_data()["pause_pending"]
    dev.async_shutdown_control.assert_not_awaited()
    dev.async_probe.side_effect = None
    await c._async_update_data()
    dev.async_shutdown_control.assert_awaited_once()
    assert not c._stored_data()["pause_pending"]
    assert not c.write_enabled
    await c.async_stop()


async def test_owed_pause_is_not_redirected_to_a_new_binding(hass, entry):
    class Phased:
        PHASED = True
        supported_modes = ("Akku Pause",)
    dev = Phased()
    dev.async_shutdown_control = AsyncMock()
    engine = await hass.async_add_executor_job(StrategyEngine)
    c = OptiCoordinator(hass, entry, dev, engine)
    c._store.async_load = AsyncMock(return_value={"pause_pending": True, "pause_binding": ["other"], "writer_binding": ["other"]})
    await c.async_restore()
    assert c._pause_pending
    assert c._stored_data()["pause_binding"] == ["other"]
    with pytest.raises(HomeAssistantError, match="anderen Bindung"):
        await c.async_set_write_enabled(True)
    await c._async_finish_huawei_pause()
    dev.async_shutdown_control.assert_not_awaited()
    await c.async_stop()


async def test_restored_huawei_writer_records_new_pause_before_any_command(hass, entry):
    class Phased:
        PHASED = True
        supported_modes = ("Akku Pause",)
    engine = await hass.async_add_executor_job(StrategyEngine)
    c = OptiCoordinator(hass, entry, Phased(), engine)
    c._store.async_load = AsyncMock(return_value={"write_enabled": True, "writer_binding": c._writer_binding,
                                                "pause_pending": False, "strategy_enabled": True})
    c._store.async_save = AsyncMock()
    await c.async_restore()
    assert c.write_enabled and c._pause_pending
    saved = c._store.async_save.call_args.args[0]
    assert saved["pause_pending"] is True and saved["pause_binding"] == c._writer_binding
    c.write_enabled = False
    await c.async_stop()


async def test_shadow_restore_preserves_debt_without_hardware_calls(hass, entry):
    class Phased:
        PHASED = True
        supported_modes = ()
    hass.config_entries.async_update_entry(entry, data={**entry.data, "shadow_mode": True})
    engine = await hass.async_add_executor_job(StrategyEngine)
    c = OptiCoordinator(hass, entry, Phased(), engine)
    c._store.async_load = AsyncMock(return_value={"pause_pending": True, "pause_binding": ["old"]})
    await c.async_restore()
    assert not c.write_enabled
    assert c._stored_data()["pause_pending"] is True
    assert c._stored_data()["pause_binding"] == ["old"]
    await c.async_stop()


async def test_operating_report_failure_is_isolated_from_control(coordinator):
    coordinator.write_enabled = True
    with patch.object(coordinator._operating_report, 'observe', side_effect=ValueError('broken report')):
        data = await coordinator._async_update_data()
    coordinator.device.async_apply.assert_awaited_once()
    assert data['operating_report']['status'] == 'error'
    assert data['command_result_this_update'] == 'confirmed'


async def test_report_exposes_failed_attempt_and_observation(coordinator):
    data = await coordinator._async_update_data()
    assert data['operating_report']['last_control']['command_result_this_update'] == 'not_attempted'
    coordinator.write_enabled = True
    coordinator.device.async_apply.side_effect = RuntimeError('device refused')
    data = await coordinator._async_update_data()
    assert data['operating_report']['write_failures'] == 1
    assert data['command_result_this_update'] == 'failed'
    assert coordinator._stored_data()['operating_report']['write_failures'] == 1


async def test_report_sensors_expose_attributes_without_dict_count(coordinator, entry):
    from custom_components.opti_akku.sensor import OptiAkkuDiagnosticSensor
    entry.runtime_data = coordinator
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    report = OptiAkkuDiagnosticSensor(entry, 'operating_report')
    plan = OptiAkkuDiagnosticSensor(entry, 'reserve_plan')
    assert report.native_value in report.options
    assert 'totals' in report.extra_state_attributes
    assert plan.native_value in plan.options
    assert 'planned_reserve_soc' in plan.extra_state_attributes
    assert plan.entity_category is None


async def test_report_restore_and_source_change_start_new_report(hass, coordinator, entry):
    await coordinator._async_update_data()
    saved = coordinator._stored_data()
    restored = OptiCoordinator(hass, entry, device(), coordinator.engine)
    try:
        with patch.object(restored._store, 'async_load', return_value=saved):
            await restored.async_restore()
        assert restored._operating_report.snapshot()['restarts'] == 1
        saved['load_source_fingerprint'] = 'different plant'
        fresh = OptiCoordinator(hass, entry, device(), coordinator.engine)
        try:
            with patch.object(fresh._store, 'async_load', return_value=saved):
                await fresh.async_restore()
            assert fresh._operating_report.snapshot()['started_at'] is None
        finally:
            await fresh.async_stop()
    finally:
        await restored.async_stop()


async def test_plan_enum_and_recorder_exclusion_in_ha(hass, coordinator, entry):
    import logging
    from homeassistant.helpers.entity_component import EntityComponent
    from custom_components.opti_akku.sensor import OptiAkkuReportSensor
    entry.runtime_data = coordinator
    coordinator.async_set_updated_data({'reserve_plan': {'status': 'no_valid_plan'}})
    entity = OptiAkkuReportSensor(entry, 'reserve_plan')
    entity.entity_id = 'sensor.test_reserve_plan'
    entity._attr_device_info = None  # Standalone test platform; no device registration.
    component = EntityComponent(logging.getLogger(__name__), 'sensor', hass)
    await component.async_add_entities([entity])
    try:
        state = hass.states.get(entity.entity_id)
        assert state.state == 'no_valid_plan'
        assert '*' in entity._state_info['unrecorded_attributes']
    finally:
        await entity.async_remove()


@pytest.mark.parametrize('confirmed', [False, True])
async def test_report_preserves_pending_phase_semantics(coordinator, confirmed):
    from custom_components.opti_akku.device import PendingCommandError
    coordinator.write_enabled = True
    pending = PendingCommandError('phase')
    pending.confirmed = confirmed
    coordinator.device.async_apply.side_effect = pending
    data = await coordinator._async_update_data()
    expected = 'safe_phase_confirmed' if confirmed else 'pending'
    assert data['operating_report']['last_control']['command_result_this_update'] == expected
    assert data['operating_report']['write_failures'] == 0


@pytest.mark.parametrize('failure', [False, True])
async def test_demand_observation_cannot_change_actuator_command(coordinator, failure):
    """New comparison, including its failure, is downstream of the same write."""
    coordinator.settings['input_boolean.akku_opti_automatik'] = True
    coordinator.manual_mode = 'Akku schnell Entladen'
    coordinator.write_enabled = True
    baseline = await coordinator._async_update_data()
    baseline_call = coordinator.device.async_apply.await_args
    coordinator.device.async_apply.reset_mock()
    coordinator._last_apply = None
    comparison = {'status': 'ready', 'suggested_reserve_soc': 5, 'observation_only': True}
    with patch.object(coordinator._demand_forecast, 'update',
                      side_effect=RuntimeError('synthetic') if failure else None,
                      return_value=comparison):
        result = await coordinator._async_update_data()
    assert result['mode'] == baseline['mode'] == 'Akku schnell Entladen'
    assert result['source_errors'] == baseline['source_errors']
    assert result['write_enabled'] is True
    assert coordinator.device.async_apply.await_args.args[:2] == baseline_call.args[:2]
    assert result['demand_forecast']['status'] == ('error' if failure else 'ready')


async def test_history_import_is_observer_only_and_idempotent(coordinator, hass, entry):
    from datetime import datetime, UTC

    options = {
        **entry.options,
        "demand_forecast": {"enabled": True, "history_house": "sensor.history"},
    }
    hass.config_entries.async_update_entry(entry, options=options)
    coordinator.write_enabled = True
    coordinator.data = await coordinator._async_update_data()
    before_calls = coordinator.device.async_apply.await_count
    previous = coordinator._demand_forecast.previous
    recent = coordinator._demand_forecast.recent
    accuracy = coordinator._demand_forecast.accuracy
    at = (dt_util.utcnow() - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    rows = {at.isoformat(): {"house_w": 500}}
    recorder = Mock()
    recorder.async_add_executor_job = AsyncMock(return_value=rows)
    with patch("homeassistant.components.recorder.get_instance", return_value=recorder):
        await coordinator.async_import_demand_history()
        await coordinator.async_import_demand_history()
    assert len(coordinator._demand_forecast.history.rows) == 1
    assert coordinator._demand_forecast.previous == previous
    assert coordinator._demand_forecast.recent is recent
    assert coordinator._demand_forecast.accuracy is accuracy
    assert coordinator.device.async_apply.await_count == before_calls
    assert coordinator.write_enabled
    assert not coordinator._history_import_running
    args = recorder.async_add_executor_job.call_args.args[0].args
    assert args[-1].astimezone(dt_util.DEFAULT_TIME_ZONE).hour == 0
    assert args[-1] <= datetime.combine(
        dt_util.now().date(), datetime.min.time(), dt_util.DEFAULT_TIME_ZONE
    ).astimezone(UTC)


async def test_history_import_save_failure_rolls_back(coordinator, hass, entry):
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "demand_forecast": {"enabled": True, "history_house": "sensor.history"},
        },
    )
    previous = coordinator._demand_forecast.history
    at = (dt_util.utcnow() - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    recorder = Mock()
    recorder.async_add_executor_job = AsyncMock(return_value={at.isoformat(): {"house_w": 500}})
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch.object(coordinator._store, "async_save", side_effect=OSError),
    ):
        with pytest.raises(HomeAssistantError):
            await coordinator.async_import_demand_history()
    assert coordinator._demand_forecast.history is previous
    assert not coordinator._history_import_running


async def test_history_import_rejects_configuration_change_during_read(coordinator, hass, entry):
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "demand_forecast": {"enabled": True, "history_house": "sensor.history"},
        },
    )

    async def changed(_):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "demand_forecast": {"enabled": False}}
        )
        return {}

    recorder = Mock()
    recorder.async_add_executor_job = AsyncMock(side_effect=changed)
    with patch("homeassistant.components.recorder.get_instance", return_value=recorder):
        with pytest.raises(HomeAssistantError):
            await coordinator.async_import_demand_history()
    assert not coordinator._demand_forecast.history.rows


async def test_sma_reconnect_gate_does_not_replay_old_command(coordinator):
    coordinator.write_enabled = True
    await coordinator._async_update_data()
    coordinator.device.async_apply.reset_mock()
    coordinator.device.async_probe.side_effect = OSError("temporary")
    offline = await coordinator._async_update_data()
    assert offline["connection_status"]["status"] == "offline"
    coordinator.device.async_probe.side_effect = None
    coordinator.device.async_read.return_value["sensor.opti_inverter_status"] = 16777213
    waiting = await coordinator._async_update_data()
    assert waiting["command_confirmation"] == "waiting_ready"
    coordinator.device.async_apply.assert_not_awaited()
    coordinator.device.async_read.return_value["sensor.opti_inverter_status"] = 235
    first = await coordinator._async_update_data()
    assert first["connection_status"]["status"] == "recovering"
    coordinator.device.async_apply.assert_not_awaited()
    second = await coordinator._async_update_data()
    assert second["connection_status"]["status"] == "ready"
    coordinator.device.async_apply.assert_awaited_once()
    assert second["operating_report"]["write_failures"] == 0


async def test_source_observation_failure_cannot_block_control(coordinator):
    coordinator.write_enabled = True
    with patch.object(
        coordinator._source_observation, "update", side_effect=RuntimeError("synthetic")
    ):
        result = await coordinator._async_update_data()
    assert result["source_observation"]["status"] == "error"
    coordinator.device.async_apply.assert_awaited_once()


async def test_peak_profile_error_falls_back_without_blocking_control(coordinator):
    coordinator.write_enabled = True
    with patch("custom_components.opti_akku.coordinator.peak_load_profile", side_effect=ValueError("broken profile")):
        data = await coordinator._async_update_data()
    assert data["attributes"]["sensor.opti_peak_load_profile"]["status"] == "fallback"
    assert data["attributes"]["sensor.opti_peak_load_profile"]["hours"] == {}
    coordinator.device.async_apply.assert_awaited()


async def test_ev_preparation_caps_pv_charge_and_never_sets_force_minima(coordinator,hass):
    hass.config_entries.async_update_entry(coordinator.entry,options={**coordinator.entry.options,'ev_preparation':{'enabled':True,'vehicle_soc':'sensor.car','charging':'binary_sensor.car_charging'}})
    coordinator.settings['input_boolean.akku_opti_automatik']=True
    coordinator.settings['input_number.akkusteuerung_min_ladestaerke']=500
    coordinator.settings['input_number.akkusteuerung_min_entladestaerke']=500
    coordinator.write_enabled=True
    hass.states.async_set('sensor.car','20',{'unit_of_measurement':'%'})
    hass.states.async_set('binary_sensor.car_charging','off')
    from custom_components.opti_akku.engine import Evaluation
    result=Evaluation({'sensor.opti_soc':'60','sensor.opti_house_consumption_w':'800','sensor.opti_battery_capacity_kwh':'12.8','sensor.opti_battery_power_w':'0','sensor.opti_battery_temp':'22','sensor.opti_charge_power_w':'2000'}, {},'Akku nur Entladen','ueber Ziel-SoC',{}, decision_id='above_target')
    with patch.object(coordinator.engine,'evaluate',return_value=result), patch.object(coordinator._ev_preparation,'update',return_value={'status':'ready','ready':True,'target_soc':80,'surplus_before_battery_w':700}):
        data=await coordinator._async_update_data()
    assert data['mode']=='Akku nur Laden'
    params=coordinator.device.async_apply.await_args.args[1]
    assert params['charge_power_w']==700
    assert params['min_charge_w']==0
    assert params['min_discharge_w']==0


@pytest.mark.parametrize("soc,charging,expected", [(21, "off", True), (40, "off", False), (45, "off", False), ("unavailable", "off", False), (21, "on", False), (21, "unavailable", False)])
async def test_ev_write_guard_ignores_only_irrelevant_soc_changes(coordinator, hass, soc, charging, expected):
    hass.config_entries.async_update_entry(coordinator.entry, options={**coordinator.entry.options,
        "ev_preparation": {"enabled": True, "vehicle_soc": "sensor.car", "charging": "binary_sensor.car_charging"}})
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.write_enabled = True
    hass.states.async_set("sensor.car", "20", {"unit_of_measurement": "%"})
    hass.states.async_set("binary_sensor.car_charging", "off")
    checked = []

    async def apply(mode, params, current):
        assert current()
        hass.states.async_set("sensor.car", str(soc), {"unit_of_measurement": "%"})
        hass.states.async_set("binary_sensor.car_charging", charging)
        checked.append(current())

    coordinator.device.async_apply.side_effect = apply
    await coordinator._async_update_data()
    coordinator.device.async_apply.side_effect = None
    assert checked == [expected]
