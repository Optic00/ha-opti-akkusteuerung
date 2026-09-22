"""HA lifecycle and coordinator safety with real strategy and a fake device."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from threading import Event
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
    obj.command_execution_basis = "modbus_write_sequence"
    obj.setpoint_readback_capability = "not_supported"
    obj.setpoint_readback_limitation = "bms_and_setpoints_not_read_back"
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
    assert data["command_evidence"]["status"] == "observation"
    assert data["command_evidence"]["physical_effect"] == "not_verified"
    assert data["command_evidence"]["battery_power_observed_at"] is not None
    assert data["command_evidence"]["observed_after_execution"] is None


async def test_command_evidence_has_no_timestamp_without_fresh_power(coordinator):
    coordinator.device.async_read.side_effect = RuntimeError("offline")
    data = await coordinator._async_update_data()
    assert data["command_evidence"]["observed_battery_power_w"] is None
    assert data["command_evidence"]["battery_power_observed_at"] is None
    assert data["command_evidence"]["observed_after_execution"] is None


async def test_huawei_cached_entity_read_does_not_claim_measurement_time(
    coordinator, entry, hass
):
    coordinator.device.last_read_errors = {}
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "backend": "huawei_solar"}
    )
    data = await coordinator._async_update_data()
    assert data["command_evidence"]["observed_battery_power_w"] == 0
    assert data["command_evidence"]["battery_power_observed_at"] is None
    assert data["command_evidence"]["observed_after_execution"] is None


async def test_write_activation_and_master_off_pause(coordinator):
    coordinator.write_enabled = True
    data = await coordinator._async_update_data()
    assert data["mode"] == "Akku Pause"
    assert coordinator.device.async_apply.await_args.args[0] == "Akku Pause"
    assert data["command_evidence"]["status"] == "completed"
    assert data["command_evidence"]["setpoint_readback"] == "not_supported"


@pytest.mark.parametrize("mode", ["Akku nur Laden", "Akku Dynamisch", "Akku Netzladen"])
async def test_manual_modes_use_protected_explicit_charge_limit(coordinator, mode):
    coordinator.settings.update({
        "input_boolean.akku_opti_automatik": True,
        "input_boolean.opti_manuelle_ladegrenze": True,
        "input_number.akkusteuerung_max_ladestaerke": 4000,
    })
    coordinator.manual_mode = mode
    coordinator.write_enabled = True
    await coordinator._async_update_data()
    args = coordinator.device.async_apply.await_args.args
    assert args[0] == mode
    assert args[1]["charge_power_w"] == 4000
    coordinator.settings["input_boolean.akku_opti_automatik"] = False
    assert (await coordinator._async_update_data())["mode"] == "Akku Pause"


async def test_manual_charge_override_is_temporary_even_with_stored_options(coordinator, hass):
    key = "input_boolean.opti_manuelle_ladegrenze"
    await coordinator.async_apply_settings({key: True}, "temporary")
    assert coordinator.settings[key] is True
    snapshot = coordinator._stored_data()
    hass.config_entries.async_update_entry(coordinator.entry, options={
        **coordinator.entry.options, "settings": {key: True}, "settings_revision": "newer",
    })
    with patch.object(coordinator._store, "async_load", AsyncMock(return_value=snapshot)):
        await coordinator.async_restore()
    assert coordinator.settings[key] is False
    await coordinator._async_update_data()
    assert coordinator.settings[key] is False


async def test_command_evidence_only_marks_next_read_after_execution(coordinator):
    async def apply(*_args):
        coordinator.device.last_write = dt_util.utcnow() - timedelta(minutes=1)

    coordinator.write_enabled = True
    coordinator.device.async_apply.side_effect = apply

    first = await coordinator._async_update_data()
    second = await coordinator._async_update_data()

    assert first["command_evidence"]["observed_after_execution"] is False
    assert second["command_evidence"]["observed_after_execution"] is True


async def test_cleanup_write_is_after_same_update_observation(coordinator):
    from custom_components.opti_akku.device import StaleCommandError

    async def apply(*_args):
        coordinator.device.last_write = dt_util.utcnow() - timedelta(minutes=1)
        raise StaleCommandError("decision changed")

    coordinator.write_enabled = True
    coordinator.device.async_apply.side_effect = apply

    result = await coordinator._async_update_data()

    assert result["command_result_this_update"] == "superseded"
    assert result["command_evidence"]["observed_after_execution"] is False


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
@pytest.mark.parametrize("override", [False, True])
async def test_manual_direction_limits_combine_before_writing(
    coordinator, requested, temperature, soc, ev, expected, override
):
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.settings["input_boolean.opti_manuelle_ladegrenze"] = override
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


async def test_ev_state_age_does_not_invalidate_pending_command(coordinator, hass, entry):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "source_max_age": 1,
        "sources": {"ev1_mode": "select.car", "ev1_charging": "binary_sensor.car"}})
    hass.states.async_set("select.car", "pv")
    hass.states.async_set("binary_sensor.car", "off")
    coordinator.write_enabled = True
    completed = []

    async def apply(mode, params, current):
        assert current()
        later = dt_util.utcnow() + timedelta(seconds=2)
        with patch("custom_components.opti_akku.coordinator.dt_util.utcnow", return_value=later):
            assert current()
        completed.append(True)

    coordinator.device.async_apply.side_effect = apply
    await coordinator._async_update_data()
    coordinator.device.async_apply.side_effect = None
    coordinator.write_enabled = False
    assert completed == [True]

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


async def test_arbitrage_options_do_not_reload_or_pause_writer(
    hass, entry, coordinator
):
    from custom_components.opti_akku import _async_options_updated

    entry.runtime_data = coordinator
    coordinator.write_enabled = True
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "arbitrage_estimate": {
                "enabled": True,
                "battery_price_eur": 5000,
                "degradation_percent": 20,
                "cycles": 5000,
                "usable_capacity_kwh": 10,
                "charge_efficiency_percent": 90,
                "discharge_efficiency_percent": 90,
                "margin_ct": 2,
            },
        },
    )

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


async def test_idle_excluded_source_keeps_balance_but_outage_still_cancels_write(coordinator, hass):
    hass.config_entries.async_update_entry(coordinator.entry, options={**coordinator.entry.options,
        "plant_mode": "balance", "plant_meter_confirmed": True, "forecast_min_load_w": 0,
        "excluded_load_sources": ["sensor.ev_load"],
        "event_based_excluded_sources": ["sensor.ev_load"]})
    hass.states.async_set("sensor.ev_load", "0", {"unit_of_measurement": "W"})
    now = dt_util.utcnow() + timedelta(days=7)
    coordinator.write_enabled = True

    async def apply(mode, params, current):
        assert current()
        hass.states.async_set("sensor.ev_load", "unavailable", {"unit_of_measurement": "W"})
        assert not current()

    coordinator.device.async_apply.side_effect = apply
    with patch("custom_components.opti_akku.coordinator.dt_util.utcnow", return_value=now):
        data = await coordinator._async_update_data()
    coordinator.device.async_apply.assert_awaited_once()
    assert float(data["states"]["sensor.opti_house_raw_w"]) == 800
    assert data["attributes"]["sensor.opti_house_raw_w"]["stale_zero_sources"] == ["sensor.ev_load"]


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


async def test_command_evidence_sensor_is_additive_and_unrecorded(coordinator, entry):
    from homeassistant.const import MATCH_ALL
    from custom_components.opti_akku.sensor import (
        OptiAkkuDiagnosticSensor,
        OptiAkkuReportSensor,
    )

    coordinator.write_enabled = True
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    entry.runtime_data = coordinator
    evidence = OptiAkkuReportSensor(entry, "command_evidence")
    confirmation = OptiAkkuDiagnosticSensor(entry, "command_confirmation")
    last_write = OptiAkkuDiagnosticSensor(entry, "last_write")

    assert evidence.unique_id == f"{entry.entry_id}_diagnostic_command_evidence"
    assert evidence.native_value == "completed"
    assert evidence.extra_state_attributes["setpoint_readback"] == "not_supported"
    assert evidence._unrecorded_attributes == frozenset({MATCH_ALL})
    assert confirmation.unique_id == f"{entry.entry_id}_diagnostic_command_confirmation"
    assert last_write.unique_id == f"{entry.entry_id}_diagnostic_last_write"


async def test_last_write_values_sensor_exposes_compact_state_and_evidence(coordinator, entry):
    from custom_components.opti_akku.sensor import OptiAkkuReportSensor

    coordinator.device.last_write_values = {
        "summary": "40151=803, 40793=0",
        "status": "completed",
        "requested_mode": "Akku Pause",
        "written_at": "2026-09-21T12:00:00+00:00",
        "register_writes": [
            {"address": 40151, "value": 803},
            {"address": 40793, "value": 0},
        ],
        "device_effect": "not_verified",
    }
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    entry.runtime_data = coordinator
    sensor = OptiAkkuReportSensor(entry, "last_write_values")

    assert sensor.native_value == "40151=803, 40793=0"
    assert sensor.extra_state_attributes == {
        "status": "completed",
        "requested_mode": "Akku Pause",
        "written_at": "2026-09-21T12:00:00+00:00",
        "register_writes": [
            {"address": 40151, "value": 803},
            {"address": 40793, "value": 0},
        ],
        "device_effect": "not_verified",
    }


async def test_arbitrage_sensor_is_display_only(coordinator, entry, hass):
    from custom_components.opti_akku.sensor import OptiAkkuArbitrageSensor

    hass.states.async_set(
        "sensor.test_price", "0.10", {"unit_of_measurement": "EUR/kWh"}
    )
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "price_unit": "EUR/kWh",
            "sources": {
                **entry.options.get("sources", {}),
                "price_current": "sensor.test_price",
            },
        },
    )
    baseline = await coordinator._async_update_data()
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "arbitrage_estimate": {
                "enabled": True,
                "battery_price_eur": 5000,
                "degradation_percent": 20,
                "cycles": 5000,
                "usable_capacity_kwh": 10,
                "charge_efficiency_percent": 90,
                "discharge_efficiency_percent": 90,
                "margin_ct": 2,
            },
        },
    )
    data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(data)
    entry.runtime_data = coordinator
    sensor = OptiAkkuArbitrageSensor(entry, "arbitrage_estimate")

    assert data["mode"] == baseline["mode"]
    assert data["reason"] == baseline["reason"]
    coordinator.device.async_apply.assert_not_awaited()
    assert data["arbitrage_estimate"]["status"] == "ready"
    assert sensor.native_value == pytest.approx(6.79, abs=0.001)
    assert sensor.native_unit_of_measurement == "ct/kWh"
    assert sensor.extra_state_attributes["informational_only"] is True
    assert sensor.extra_state_attributes["controls_battery"] is False

    hass.states.async_set(
        "sensor.test_price", "unavailable", {"unit_of_measurement": "EUR/kWh"}
    )
    missing = await coordinator._async_update_data()
    assert missing["arbitrage_estimate"]["status"] == "price_missing"

    coordinator.strategy_enabled = False
    disabled = await coordinator._async_update_data()
    assert disabled["arbitrage_estimate"]["status"] == "strategy_disabled"

    coordinator.async_set_updated_data({})
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


@pytest.mark.parametrize(
    ("demand_status", "expected_profile_ready"),
    [("learning", False), ("ready", True)],
)
async def test_arbitrage_receives_passive_terminal_value_context(
    coordinator, entry, hass, demand_status, expected_profile_ready
):
    forecast_slots = [{
        "start": "2026-09-18T08:00:00+00:00",
        "end": "2026-09-18T08:30:00+00:00",
        "load_w": 1000,
        "pv_p10_w": 0,
    }]
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "demand_forecast": {"enabled": True, "sources": {}},
        },
    )
    with (
        patch.object(
            coordinator._demand_forecast,
            "update",
            return_value={
                "status": demand_status,
                "forecast_slots": forecast_slots,
                "pv_cover_from": "2026-09-18T08:30:00+00:00",
                "profile_ready": True,
            },
        ),
        patch(
            "custom_components.opti_akku.coordinator.build_arbitrage_estimate",
            return_value={"status": "not_configured", "controls_battery": False},
        ) as build,
    ):
        data = await coordinator._async_update_data()

    context = build.call_args.kwargs["terminal_context"]
    assert context["forecast_slots"] == forecast_slots
    assert context["profile_ready"] is expected_profile_ready
    assert context["battery_capacity_kwh"] == 12.8
    assert context["current_soc"] == 60
    assert data["arbitrage_estimate"]["controls_battery"] is False
    coordinator.device.async_apply.assert_not_awaited()


async def test_arbitrage_observation_failure_cannot_fail_update(
    coordinator, entry
):
    with patch(
        "custom_components.opti_akku.coordinator.build_arbitrage_estimate",
        side_effect=RuntimeError("display failed"),
    ):
        data = await coordinator._async_update_data()

    assert data["arbitrage_estimate"] == {
        "status": "error", "informational_only": True, "controls_battery": False
    }
    assert data["online"] is True
    coordinator.device.async_apply.assert_not_awaited()


def active_hold_options():
    return {
        "enabled": True,
        "hold_enabled": True,
        "battery_price_eur": 5000,
        "degradation_percent": 20,
        "cycles": 5000,
        "usable_capacity_kwh": 10,
        "charge_efficiency_percent": 90,
        "discharge_efficiency_percent": 90,
        "margin_ct": 2,
    }


def active_hold_report(now):
    return {
        "status": "ready",
        "hold_enabled": True,
        "terminal_value_status": "ready",
        "terminal_value_issued_at": now.isoformat(),
        "terminal_value_current_soc": 60,
        "terminal_value_battery_capacity_kwh": 12.8,
        "terminal_value_minimum_soc": 10,
        "terminal_value_maximum_soc": 95,
        "terminal_value_current_marginal_ct_kwh": 30,
        "throughput_cost_ct_kwh": 1,
        "battery_price_eur": 5000,
        "degradation_percent": 20,
        "cycles": 5000,
        "usable_capacity_kwh": 10,
        "charge_efficiency": 0.9,
        "discharge_efficiency": 0.9,
        "margin_ct": 2,
    }


def active_hold_evaluation(price=10):
    from custom_components.opti_akku.engine import Evaluation

    return Evaluation(
        {
            "sensor.opti_soc": 60,
            "sensor.opti_house_consumption_w": 800,
            "sensor.opti_battery_capacity_kwh": 12.8,
            "sensor.opti_battery_power_w": 0,
            "sensor.opti_battery_temp": 22,
            "sensor.opti_charge_power_w": 2000,
            "sensor.opti_price_current_ct_kwh": price,
            "input_number.minsoc": 10,
            "input_number.maxsoc": 95,
        },
        {},
        "Akku nur Entladen",
        "Peak-Leiter L2",
        {},
        decision_id="peak_l2",
    )


async def test_active_hold_never_inherits_force_charge_or_discharge_minima(
    coordinator, hass
):
    hass.config_entries.async_update_entry(
        coordinator.entry,
        data={**coordinator.entry.data, "backend": "sma_modbus"},
    )
    hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "arbitrage_estimate": active_hold_options(),
            "demand_forecast": {"enabled": True, "sources": {}},
        },
    )
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.settings["input_number.akkusteuerung_min_ladestaerke"] = 500
    coordinator.settings["input_number.akkusteuerung_min_entladestaerke"] = 500
    coordinator.write_enabled = True
    coordinator.async_set_updated_data(
        {
            "arbitrage_estimate": active_hold_report(dt_util.utcnow()),
            "engine_requested_mode": "Akku nur Entladen",
        }
    )
    with patch.object(
        coordinator.engine, "evaluate", return_value=active_hold_evaluation()
    ):
        data = await coordinator._async_update_data()

    assert data["mode"] == "Akku nur Laden"
    assert data["arbitrage_estimate"]["hold_status"] == "holding"
    params = coordinator.device.async_apply.await_args.args[1]
    assert params["min_charge_w"] == 0
    assert params["min_discharge_w"] == 0


async def test_safety_pause_reports_priority_over_active_hold(coordinator, hass):
    hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "arbitrage_estimate": active_hold_options(),
            "demand_forecast": {"enabled": True, "sources": {}},
        },
    )
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.write_enabled = True
    report = active_hold_report(dt_util.utcnow())
    coordinator.async_set_updated_data(
        {"arbitrage_estimate": report}
    )
    evaluation = active_hold_evaluation()
    evaluation.states["sensor.opti_battery_temp"] = 60

    with patch.object(coordinator.engine, "evaluate", return_value=evaluation):
        data = await coordinator._async_update_data()

    assert data["decision_id"] == "arbitrage_hold"
    assert data["mode"] == "Akku Pause"
    assert data["arbitrage_estimate"]["hold_status"] == "higher_priority"
    assert data["arbitrage_estimate"]["hold_would_control_battery"] is True
    assert data["arbitrage_estimate"]["hold_controls_battery"] is False
    assert data["arbitrage_estimate"]["controls_battery"] is False
    coordinator.device.async_apply.assert_awaited_once()
    assert coordinator.device.async_apply.await_args.args[0] == "Akku Pause"

    coordinator.async_set_updated_data(
        {
            **data,
            "arbitrage_estimate": {
                **report,
                "hold_status": data["arbitrage_estimate"]["hold_status"],
            },
        }
    )
    coordinator.device.async_apply.reset_mock()
    with patch.object(
        coordinator.engine,
        "evaluate",
        return_value=active_hold_evaluation(price=29),
    ):
        resumed = await coordinator._async_update_data()

    assert resumed["mode"] == "Akku nur Entladen"
    assert resumed["arbitrage_estimate"]["hold_status"] == "release"
    assert resumed["arbitrage_estimate"]["hold_would_control_battery"] is False
    coordinator.device.async_apply.assert_awaited_once()
    assert coordinator.device.async_apply.await_args.args[0] == "Akku nur Entladen"


async def test_active_hold_failure_retains_engine_decision(coordinator, hass):
    hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "arbitrage_estimate": active_hold_options(),
            "demand_forecast": {"enabled": True, "sources": {}},
        },
    )
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    with (
        patch.object(
            coordinator.engine, "evaluate", return_value=active_hold_evaluation()
        ),
        patch(
            "custom_components.opti_akku.coordinator.apply_discharge_hold",
            side_effect=RuntimeError("synthetic"),
        ),
    ):
        data = await coordinator._async_update_data()

    assert data["mode"] == "Akku nur Entladen"
    assert data["arbitrage_estimate"]["hold_status"] == "error"


@pytest.mark.parametrize(
    ("previous_decision", "previous_mode", "expected_previous_mode"),
    [
        ("arbitrage_hold", "Akku nur Laden", "Akku nur Entladen"),
        ("arbitrage_hold", "Akku Pause", "Akku Pause"),
        ("ev_prepare", "Akku nur Laden", "Akku nur Laden"),
    ],
)
async def test_only_active_hold_hides_policy_mode_from_engine_feedback(
    coordinator, previous_decision, previous_mode, expected_previous_mode
):
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.async_set_updated_data(
        {
            "mode": previous_mode,
            "decision_id": previous_decision,
            "engine_base_mode": "Akku nur Entladen",
        }
    )
    with patch.object(
        coordinator.engine,
        "evaluate",
        return_value=active_hold_evaluation(price=40),
    ) as evaluate:
        await coordinator._async_update_data()

    assert (
        evaluate.call_args.args[0]["input_select.akkusteuerung_modus"]
        == expected_previous_mode
    )


async def test_active_hold_is_blocked_for_huawei_backend(coordinator, hass):
    hass.config_entries.async_update_entry(
        coordinator.entry,
        data={**coordinator.entry.data, "backend": "huawei_solar"},
    )
    hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "arbitrage_estimate": active_hold_options(),
            "demand_forecast": {"enabled": True, "sources": {}},
        },
    )
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.async_set_updated_data(
        {
            "arbitrage_estimate": active_hold_report(dt_util.utcnow()),
            "mode": "Akku nur Entladen",
            "decision_id": "peak_l2",
        }
    )
    with (
        patch.object(
            coordinator.engine,
            "evaluate",
            return_value=active_hold_evaluation(),
        ),
        patch(
            "custom_components.opti_akku.coordinator.apply_discharge_hold"
        ) as apply_hold,
        patch.object(
            coordinator,
            "_safe_mode",
            side_effect=lambda requested, _states: (requested, None),
        ),
    ):
        data = await coordinator._async_update_data()

    apply_hold.assert_not_called()
    assert data["mode"] == "Akku nur Entladen"
    assert data["arbitrage_estimate"]["hold_status"] == "backend_not_supported"
    assert data["arbitrage_estimate"]["hold_controls_battery"] is False
    assert data["arbitrage_estimate"]["controls_battery"] is False


async def test_ev_preparation_reports_priority_over_active_hold(coordinator, hass):
    hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "arbitrage_estimate": active_hold_options(),
            "demand_forecast": {"enabled": True, "sources": {}},
            "ev_preparation": {
                "enabled": True,
                "vehicle_soc": "sensor.car",
                "charging": "binary_sensor.car_charging",
            },
        },
    )
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.async_set_updated_data(
        {"arbitrage_estimate": active_hold_report(dt_util.utcnow())}
    )
    hass.states.async_set("sensor.car", "20", {"unit_of_measurement": "%"})
    hass.states.async_set("binary_sensor.car_charging", "off")
    with (
        patch.object(
            coordinator.engine,
            "evaluate",
            return_value=active_hold_evaluation(),
        ),
        patch.object(
            coordinator._ev_preparation,
            "update",
            return_value={
                "status": "ready",
                "ready": True,
                "target_soc": 80,
                "surplus_before_battery_w": 700,
            },
        ),
    ):
        data = await coordinator._async_update_data()

    assert data["decision_id"] == "ev_preparation"
    assert data["engine_requested_mode"] == "Akku nur Laden"
    assert data["engine_base_mode"] == "Akku nur Entladen"
    assert data["arbitrage_estimate"]["hold_status"] == "higher_priority"
    assert data["arbitrage_estimate"]["hold_controls_battery"] is False


async def test_active_hold_write_guard_detects_entity_price_change(
    coordinator, hass
):
    hass.states.async_set(
        "sensor.hold_price", "0.10", {"unit_of_measurement": "EUR/kWh"}
    )
    hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "price_unit": "EUR/kWh",
            "sources": {"price_current": "sensor.hold_price"},
            "arbitrage_estimate": active_hold_options(),
            "demand_forecast": {"enabled": True, "sources": {}},
        },
    )
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.write_enabled = True
    coordinator.async_set_updated_data(
        {
            "arbitrage_estimate": active_hold_report(dt_util.utcnow()),
            "engine_requested_mode": "Akku nur Entladen",
        }
    )
    checked = []

    async def apply(_mode, _params, current):
        assert current()
        hass.states.async_set(
            "sensor.hold_price", "0.40", {"unit_of_measurement": "EUR/kWh"}
        )
        checked.append(current())

    coordinator.device.async_apply.side_effect = apply
    with patch.object(
        coordinator.engine, "evaluate", return_value=active_hold_evaluation()
    ):
        await coordinator._async_update_data()

    assert checked == [False]


async def test_active_hold_write_guard_also_invalidates_released_discharge(
    coordinator, hass
):
    hass.states.async_set(
        "sensor.hold_price", "0.40", {"unit_of_measurement": "EUR/kWh"}
    )
    hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "price_unit": "EUR/kWh",
            "sources": {"price_current": "sensor.hold_price"},
            "arbitrage_estimate": active_hold_options(),
            "demand_forecast": {"enabled": True, "sources": {}},
        },
    )
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.write_enabled = True
    coordinator.async_set_updated_data(
        {
            "arbitrage_estimate": active_hold_report(dt_util.utcnow()),
            "engine_requested_mode": "Akku nur Entladen",
        }
    )
    checked = []

    async def apply(_mode, _params, current):
        assert current()
        hass.states.async_set(
            "sensor.hold_price", "0.10", {"unit_of_measurement": "EUR/kWh"}
        )
        checked.append(current())

    coordinator.device.async_apply.side_effect = apply
    with patch.object(
        coordinator.engine,
        "evaluate",
        return_value=active_hold_evaluation(price=40),
    ):
        data = await coordinator._async_update_data()

    assert data["mode"] == "Akku nur Entladen"
    assert data["arbitrage_estimate"]["hold_status"] == "release"
    assert checked == [False]


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
    assert result["command_evidence"]["status"] == "pending"
    assert coordinator._last_apply is None
    assert coordinator._last_signature is None
    coordinator.device.async_apply.side_effect = None
    result = await coordinator._async_update_data()
    assert result["command_confirmation"] == "idle_or_confirmed"
    assert result["command_evidence"]["status"] == "completed"
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
    assert data['command_evidence']['physical_effect'] == 'not_verified'


async def test_shadow_profile_comparison_isolated_across_ready_error_and_disabled(coordinator):
    coordinator.shadow_mode = True
    coordinator.hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "demand_forecast": {"enabled": True, "use_for_peak_reserve": True},
        },
    )
    coordinator.settings["input_number.opti_peak_verbrauch_kw"] = 0.8
    demand_report = {
        "status": "ready", "observation_only": True, "context": "summer",
        "recent_coverage_seconds": 1800,
    }
    outcomes = [
        {"status": "ready", "observation_only": True, "blocks": {}},
        ValueError("comparison failed"),
        {"status": "disabled", "observation_only": True, "blocks": {}},
    ]
    active = []
    snapshots = []
    fixed_now = dt_util.utcnow()
    timezone = dt_util.DEFAULT_TIME_ZONE
    coordinator._demand_forecast.fingerprint = json.dumps(
        [coordinator._load_source_fingerprint, {}, str(timezone)], sort_keys=True
    )
    local = fixed_now.astimezone(timezone)
    for days in range(1, 22):
        day = (local - timedelta(days=days)).date()
        for hour in range(24):
            coordinator._demand_forecast.cells[
                f"{day.isoformat()}|{hour}|unknown"
            ] = [800 * 3600, 0, 0, 3600]
    coordinator._engine_snapshot = {
        **coordinator._engine_snapshot,
        "attributes": {"sensor.opti_target_soc": {"level": 2}},
    }
    comparison_builder = Mock(side_effect=outcomes)
    with (
        patch.object(coordinator._demand_forecast, "update", return_value=demand_report),
        patch("custom_components.opti_akku.coordinator.build_strategy_comparison",
              comparison_builder),
        patch("custom_components.opti_akku.coordinator.dt_util.utcnow", return_value=fixed_now),
    ):
        for expected in ("ready", "error", "disabled"):
            data = await coordinator._async_update_data()
            assert data["demand_forecast"]["status"] == "ready"
            comparison = data["demand_forecast"]["strategy_comparison"]
            assert comparison == {"status": expected, "observation_only": True, **(
                {"blocks": {}} if expected != "error" else {})}
            active.append({
                key: deepcopy(data[key])
                for key in ("states", "attributes", "mode", "reason", "source_errors")
            })
            snapshots.append(deepcopy(coordinator._engine_snapshot))
    assert comparison_builder.call_args_list[0].args[-1] == 2
    assert active[0] == active[1] == active[2]
    assert active[0]["states"]["sensor.opti_peak_load_profile"] == "profile"
    assert snapshots[0] == snapshots[1] == snapshots[2]
    coordinator.device.async_apply.assert_not_awaited()


async def test_active_profile_comparison_requires_enabled_demand(coordinator):
    with patch(
        "custom_components.opti_akku.coordinator.build_strategy_comparison"
    ) as comparison_builder:
        data = await coordinator._async_update_data()

    comparison_builder.assert_not_called()
    assert "strategy_comparison" not in data["demand_forecast"]

    coordinator.hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "demand_forecast": {"enabled": True},
        },
    )
    comparison_builder.reset_mock()
    comparison_builder.return_value = {
        "status": "ready",
        "observation_only": True,
        "blocks": {},
    }
    with patch(
        "custom_components.opti_akku.coordinator.build_strategy_comparison",
        comparison_builder,
    ):
        data = await coordinator._async_update_data()

    comparison_builder.assert_called_once()
    assert data["demand_forecast"]["strategy_comparison"] == {
        "status": "ready",
        "observation_only": True,
        "blocks": {},
    }
    assert data["states"]["sensor.opti_forecast_remaining_load_profile_kwh"] == "unavailable"
    assert data["attributes"]["sensor.opti_forecast_remaining_load_profile_kwh"] == {
        "status": "disabled",
        "reason": "active_profile_disabled",
    }


@pytest.mark.parametrize(
    ("profile", "expected_state"),
    [
        (
            {
                "status": "ready",
                "reason": "complete_online_profile",
                "profile_energy_kwh": 3.25,
                "window_hours": 5,
            },
            3.25,
        ),
        (
            {
                "status": "learning",
                "reason": "recent_profile_warming_up",
                "profile_energy_kwh": 3.25,
            },
            "unavailable",
        ),
    ],
)
async def test_only_ready_remaining_day_profile_reaches_engine(
    coordinator, profile, expected_state
):
    coordinator.hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "demand_forecast": {"enabled": True, "use_for_peak_reserve": True},
        },
    )
    demand_report = {
        "status": "ready",
        "observation_only": True,
        "context": "unknown",
        "recent_coverage_seconds": 1800,
    }
    original = coordinator.engine.evaluate
    evaluated_states = []

    def capture(states, attributes, now):
        evaluated_states.append((deepcopy(states), deepcopy(attributes)))
        return original(states, attributes, now)

    with (
        patch.object(coordinator._demand_forecast, "update", return_value=demand_report),
        patch(
            "custom_components.opti_akku.coordinator.remaining_day_profile",
            return_value=(profile, None),
        ),
        patch.object(coordinator.engine, "evaluate", side_effect=capture),
    ):
        result = await coordinator._async_update_data()

    assert evaluated_states[0][0]["sensor.opti_forecast_remaining_load_profile_kwh"] == expected_state
    assert evaluated_states[0][1]["sensor.opti_forecast_remaining_load_profile_kwh"] == profile
    assert result["states"]["sensor.opti_forecast_remaining_load_profile_kwh"] == str(expected_state)


async def test_active_profile_failure_falls_back_without_blocking_write(coordinator):
    coordinator.hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "demand_forecast": {"enabled": True, "use_for_peak_reserve": True},
        },
    )
    coordinator.write_enabled = True
    with patch(
        "custom_components.opti_akku.coordinator.remaining_day_profile",
        side_effect=ValueError("broken profile"),
    ):
        result = await coordinator._async_update_data()
    assert result["states"]["sensor.opti_forecast_remaining_load_profile_kwh"] == "unavailable"
    assert result["attributes"]["sensor.opti_forecast_remaining_load_profile_kwh"] == {
        "status": "error",
        "reason": "profile_error",
    }
    coordinator.device.async_apply.assert_awaited_once()


async def test_malformed_demand_sources_fail_back_without_blocking_update(coordinator):
    coordinator.hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "demand_forecast": {
                "enabled": True,
                "use_for_peak_reserve": True,
                "sources": ["invalid"],
            },
        },
    )

    result = await coordinator._async_update_data()

    assert result["states"]["sensor.opti_forecast_remaining_load_profile_kwh"] == "unavailable"
    assert result["demand_forecast"]["status"] == "error"


@pytest.mark.parametrize("shadow_mode", [False, True])
async def test_real_profile_comparison_stays_passive_across_two_updates(
    coordinator, shadow_mode
):
    coordinator.shadow_mode = shadow_mode
    coordinator.write_enabled = not shadow_mode
    coordinator.hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "demand_forecast": {"enabled": True, "use_for_peak_reserve": True},
        },
    )
    fixed_now = datetime(2026, 9, 16, 10, tzinfo=UTC)
    timezone = dt_util.DEFAULT_TIME_ZONE
    local = fixed_now.astimezone(timezone)
    coordinator._demand_forecast.fingerprint = json.dumps(
        [coordinator._load_source_fingerprint, {}, str(timezone)], sort_keys=True
    )
    for days in range(1, 22):
        day = (local - timedelta(days=days)).date()
        for hour in range(24):
            coordinator._demand_forecast.cells[
                f"{day.isoformat()}|{hour}|unknown"
            ] = [800 * 3600, 0, 0, 3600]
    coordinator._demand_forecast.previous = (
        fixed_now - timedelta(seconds=30), 800.0, 0.0, 0.0, "unknown"
    )
    coordinator._demand_forecast.recent.observe(
        800, fixed_now - timedelta(seconds=30), coordinator._load_source_fingerprint
    )
    demand_report = {
        "status": "ready", "observation_only": True, "context": "unknown",
        "recent_coverage_seconds": 1800, "extra_base_load_w": 0,
        "temperature_context": {}, "dhw_extra_kwh": 0,
        "heating_active": False, "dhw_active": False,
    }
    model_snapshot = deepcopy(coordinator._demand_forecast.snapshot())
    previous = deepcopy(coordinator._demand_forecast.previous)
    recent = deepcopy(coordinator._demand_forecast.recent)
    active = []
    engine_snapshots = []
    with (
        patch.object(coordinator._demand_forecast, "update", return_value=demand_report),
        patch("custom_components.opti_akku.coordinator.dt_util.utcnow", return_value=fixed_now),
    ):
        for _ in range(2):
            data = await coordinator._async_update_data()
            assert data["demand_forecast"]["strategy_comparison"]["observation_only"] is True
            active.append({
                key: deepcopy(data[key])
                for key in ("states", "attributes", "mode", "reason", "source_errors")
            })
            engine_snapshots.append(deepcopy(coordinator._engine_snapshot))

    assert active[0] == active[1]
    assert active[0]["states"]["sensor.opti_peak_load_profile"] == "profile"
    assert engine_snapshots[0] == engine_snapshots[1]
    assert coordinator._demand_forecast.snapshot() == model_snapshot
    assert coordinator._demand_forecast.previous == previous
    assert coordinator._demand_forecast.recent._fingerprint == recent._fingerprint
    assert coordinator._demand_forecast.recent._samples == recent._samples
    if shadow_mode:
        coordinator.device.async_apply.assert_not_awaited()
    else:
        coordinator.device.async_apply.assert_awaited()


async def test_active_profile_comparison_failure_isolated_from_write(coordinator):
    coordinator.hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "demand_forecast": {"enabled": True},
        },
    )
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.manual_mode = "Akku schnell Entladen"
    coordinator.write_enabled = True

    with patch(
        "custom_components.opti_akku.coordinator.build_strategy_comparison",
        side_effect=ValueError("comparison failed"),
    ):
        result = await coordinator._async_update_data()

    assert result["mode"] == "Akku schnell Entladen"
    assert result["write_enabled"] is True
    assert result["demand_forecast"]["strategy_comparison"] == {
        "status": "error",
        "observation_only": True,
    }
    assert coordinator.device.async_apply.await_args.args[0] == "Akku schnell Entladen"


@pytest.mark.parametrize(
    ("error", "expected"),
    [(RuntimeError("refused"), "failed"), (None, "superseded")],
)
async def test_new_failure_or_supersession_invalidates_current_command_evidence(
    coordinator, error, expected
):
    from custom_components.opti_akku.device import StaleCommandError

    coordinator.write_enabled = True
    first = await coordinator._async_update_data()
    assert first["command_evidence"]["status"] == "completed"
    coordinator._last_apply = None
    coordinator.device.async_apply.side_effect = error or StaleCommandError("changed")
    result = await coordinator._async_update_data()
    assert result["command_evidence"]["status"] == expected
    assert result["command_evidence"]["setpoint_readback"] == "not_assessed"
    assert result["command_evidence"]["physical_effect"] == "not_verified"


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
    coordinator.hass.config_entries.async_update_entry(
        coordinator.entry,
        options={
            **coordinator.entry.options,
            "demand_forecast": {"enabled": True},
        },
    )
    coordinator.settings['input_boolean.akku_opti_automatik'] = True
    coordinator.manual_mode = 'Akku schnell Entladen'
    coordinator.write_enabled = True
    baseline = await coordinator._async_update_data()
    baseline_call = coordinator.device.async_apply.await_args
    coordinator.device.async_apply.reset_mock()
    coordinator._last_apply = None
    comparison = {'status': 'ready', 'suggested_reserve_soc': 5, 'observation_only': True}
    profile_comparison = {
        'status': 'ready', 'observation_only': True, 'blocks': {}
    }
    with (
        patch.object(coordinator._demand_forecast, 'update',
                     side_effect=RuntimeError('synthetic') if failure else None,
                     return_value=comparison),
        patch('custom_components.opti_akku.coordinator.build_strategy_comparison',
              return_value=profile_comparison) as comparison_builder,
    ):
        result = await coordinator._async_update_data()
    assert result['mode'] == baseline['mode'] == 'Akku schnell Entladen'
    assert result['source_errors'] == baseline['source_errors']
    assert result['write_enabled'] is True
    assert coordinator.device.async_apply.await_args.args[:2] == baseline_call.args[:2]
    assert result['demand_forecast']['status'] == ('error' if failure else 'ready')
    assert result['demand_forecast']['strategy_comparison'] == profile_comparison
    comparison_builder.assert_called_once()


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


async def test_observation_selector_uses_valid_translation_key_without_changing_engine_mode(coordinator, entry):
    from custom_components.opti_akku.select import OptiAkkuModeSelect

    entry.runtime_data = coordinator
    coordinator.strategy_enabled = False
    entity = OptiAkkuModeSelect(entry)
    assert entity.current_option == "observation"
    assert entity.options[0] == "observation"
    await entity.async_select_option("observation")
    assert coordinator.manual_mode is None
    assert coordinator.write_enabled is False
    assert entity.current_option == "observation"


async def test_duplicate_endpoint_is_permanent_setup_error_not_automatic_retry(hass, entry):
    from custom_components.opti_akku import async_setup_entry
    from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

    hass.data["opti_akku_writers"] = {("127.0.0.1", 15020, 3): "existing-controller"}
    with pytest.raises(ConfigEntryError) as error:
        await async_setup_entry(hass, entry)
    assert not isinstance(error.value, ConfigEntryNotReady)
    assert hass.data["opti_akku_writers"][("127.0.0.1", 15020, 3)] == "existing-controller"


async def test_explicit_existing_grid_charging_preference_is_preserved(hass, entry):
    from homeassistant.helpers.storage import Store

    await Store(hass, 1, f"opti_akku.{entry.entry_id}", private=True).async_save({
        "settings": {"input_boolean.opti_prognose_netzladen": True}
    })
    coordinator = OptiCoordinator(hass, entry, device(), StrategyEngine())
    await coordinator.async_restore()
    try:
        assert coordinator.settings["input_boolean.opti_prognose_netzladen"] is True
    finally:
        await coordinator.async_stop()


async def test_switch_setting_requires_a_boolean(coordinator):
    with pytest.raises(HomeAssistantError, match="Wahrheitswert"):
        validate_setting(
            "input_boolean.akku_opti_automatik",
            "on",
            coordinator.settings,
        )


async def test_restore_ignores_unknown_settings_and_resets_reversed_limits(coordinator):
    await coordinator._store.async_save(
        {
            "settings": {
                "input_number.unknown_future_setting": 42,
                "input_number.minsoc": 90,
                "input_number.maxsoc": 80,
            }
        }
    )

    await coordinator.async_restore()

    assert "input_number.unknown_future_setting" not in coordinator.settings
    assert coordinator.settings["input_number.minsoc"] == 10
    assert coordinator.settings["input_number.maxsoc"] == 95
    assert "minsoc" in coordinator._last_error
    assert "maxsoc" in coordinator._last_error
    assert coordinator.write_enabled is False


async def test_device_without_serial_is_never_claimed_for_writes(coordinator, hass):
    coordinator._claim_device({"model": "unknown"})

    assert coordinator._serial_owner is None
    assert "opti_akku_serial_owners" not in hass.data


async def test_existing_home_assistant_sun_state_is_used_verbatim(coordinator, hass):
    hass.states.async_set(
        "sun.sun",
        "above_horizon",
        {"next_setting": "2026-09-15T18:00:00+02:00"},
    )
    states = {}
    attributes = {}

    coordinator._add_sun(states, attributes, dt_util.utcnow())

    assert states["sun.sun"] == "above_horizon"
    assert attributes["sun.sun"] == {
        "next_setting": "2026-09-15T18:00:00+02:00"
    }


async def test_shadow_without_demand_does_not_schedule_disabled_comparison(coordinator):
    coordinator.shadow_mode = True
    with patch("custom_components.opti_akku.coordinator.build_strategy_comparison") as compare:
        result = await coordinator._async_update_data()
    compare.assert_not_called()
    assert "strategy_comparison" not in result["demand_forecast"]
    assert "shadow_summary" in result
    coordinator.device.async_apply.assert_not_awaited()


async def test_standard_comparison_start_stop_publish_without_refresh_or_device_call(coordinator):
    coordinator._online = True
    coordinator.data = {"mode": "Akku Pause"}
    coordinator.async_refresh = AsyncMock()
    coordinator._store.async_save = AsyncMock()
    coordinator._schedule_refresh = Mock()
    coordinator._async_unsub_refresh = Mock()
    coordinator.device.reset_mock()

    await coordinator.async_start_comparison()
    assert coordinator.data["shadow_status"] == "running"
    await coordinator.async_stop_comparison()
    assert coordinator.data["shadow_status"] == "stopped"

    coordinator.async_refresh.assert_not_awaited()
    coordinator._schedule_refresh.assert_not_called()
    coordinator._async_unsub_refresh.assert_not_called()
    coordinator.device.async_apply.assert_not_awaited()
    coordinator.device.async_read.assert_not_awaited()
    coordinator.device.async_probe.assert_not_awaited()


async def test_shadow_methods_keep_standard_guard(coordinator):
    coordinator._online = True
    with pytest.raises(HomeAssistantError, match="Shadow"):
        await coordinator.async_start_shadow()
    with pytest.raises(HomeAssistantError, match="Shadow"):
        await coordinator.async_stop_shadow()


async def test_running_standard_journal_failure_does_not_interrupt_control(coordinator):
    coordinator.write_enabled = True
    coordinator._shadow_snapshot = {"status": "running", "reference_entity": ""}
    coordinator._shadow.state = dict(coordinator._shadow_snapshot)
    with patch.object(coordinator._shadow, "record", side_effect=OSError("disk full")):
        result = await coordinator._async_update_data()
    coordinator.device.async_apply.assert_awaited_once()
    assert result["shadow_status"] == "error"
    assert result["shadow_summary"]["error"] == "journal_write_failed"


async def test_standard_journal_records_during_unchanged_command_cycle(coordinator):
    await coordinator._async_update_data()
    coordinator.write_enabled = True
    coordinator.data = {"shadow_status": "idle"}
    coordinator._store.async_save = AsyncMock()
    coordinator.async_refresh = AsyncMock()
    await coordinator.async_start_comparison()
    coordinator.device.async_apply.reset_mock()

    result = await coordinator._async_update_data()

    coordinator.device.async_apply.assert_awaited_once()
    assert result["shadow_status"] == "running"
    assert result["shadow_summary"]["samples"] == 1
    assert result["shadow_summary"]["deadline"] == coordinator._shadow_snapshot["deadline"]


async def test_standard_journal_restores_same_session_without_auto_start(coordinator, hass, entry):
    coordinator._online = True
    coordinator.data = {"shadow_status": "idle"}
    coordinator._store.async_save = AsyncMock()
    await coordinator.async_start_comparison()
    saved = coordinator._stored_data()

    restored = OptiCoordinator(
        hass, entry, device(), await hass.async_add_executor_job(StrategyEngine))
    restored._store.async_load = AsyncMock(return_value=saved)
    await restored.async_restore()

    assert restored._shadow_snapshot["status"] == "running"
    assert restored._shadow_snapshot["session_id"] == coordinator._shadow_snapshot["session_id"]
    assert restored._shadow_snapshot["deadline"] == coordinator._shadow_snapshot["deadline"]
    await restored.async_stop()


async def test_journal_stop_and_restart_wait_for_inflight_sample(coordinator, hass):
    coordinator._online = True
    coordinator.data = {"shadow_status": "idle"}
    coordinator._store.async_save = AsyncMock()
    await coordinator.async_start_comparison()
    first_session = coordinator._shadow_snapshot["session_id"]
    original_append = coordinator._shadow._append
    entered = Event()
    release = Event()

    def blocking_append(row, *, exclusive=False):
        if row.get("type") == "sample":
            entered.set()
            assert release.wait(2)
        return original_append(row, exclusive=exclusive)

    sample = {
        "mode": "Akku Pause", "reason": "test", "online": True,
        "source_errors": {}, "device_errors": {}, "states": {},
        "write_enabled": False, "strategy_enabled": True, "price_status": "ready",
    }
    with patch.object(coordinator._shadow, "_append", side_effect=blocking_append):
        record = asyncio.create_task(coordinator._async_record_journal(sample, dt_util.utcnow()))
        assert await hass.async_add_executor_job(entered.wait, 2)
        stop = asyncio.create_task(coordinator.async_stop_comparison())
        await asyncio.sleep(0)
        assert not stop.done()
        release.set()
        await record
        await stop

    await coordinator.async_start_comparison()
    assert coordinator._shadow_snapshot["session_id"] != first_session


@pytest.mark.parametrize("stale_data", [{}, {"mode": "Akku Pause", "states": {}}])
async def test_stopping_does_not_record_stale_or_empty_data(coordinator, stale_data):
    coordinator._shadow_snapshot = {"status": "running", "session_id": "a" * 32}
    coordinator._shadow.state = dict(coordinator._shadow_snapshot)
    before = coordinator._shadow.snapshot()
    coordinator._stopping = True

    with patch.object(coordinator._shadow, "record") as record:
        await coordinator._async_record_journal(stale_data, dt_util.utcnow())

    record.assert_not_called()
    assert coordinator._shadow.snapshot() == before
    assert coordinator._shadow_snapshot == before


@pytest.mark.parametrize(
    ("method", "shadow", "online", "stopping", "message"),
    [
        ("async_start_shadow", False, True, False, "Shadow"),
        ("async_start_shadow", True, False, False, "Shadow"),
        ("async_start_shadow", True, True, True, "Shadow"),
        ("async_start_comparison", True, True, False, "24-Stunden"),
        ("async_start_comparison", False, False, False, "24-Stunden"),
        ("async_start_comparison", False, True, True, "24-Stunden"),
        ("async_stop_shadow", False, True, False, "Shadow"),
        ("async_stop_shadow", True, True, True, "Shadow"),
        ("async_stop_comparison", True, True, False, "Standard"),
        ("async_stop_comparison", False, True, True, "Standard"),
    ],
)
async def test_journal_method_guards(
    coordinator, method, shadow, online, stopping, message
):
    coordinator.shadow_mode = shadow
    coordinator._online = online
    coordinator._stopping = stopping
    with pytest.raises(HomeAssistantError, match=message):
        await getattr(coordinator, method)()


@pytest.mark.parametrize("error", [ValueError("running"), OSError("disk")])
async def test_journal_start_io_errors_are_user_visible(coordinator, error):
    coordinator._store.async_save = AsyncMock()
    with patch.object(coordinator._shadow, "start", side_effect=error):
        with pytest.raises(HomeAssistantError, match="kann nicht angelegt"):
            await coordinator._async_start_journal(refresh=False)
    coordinator._store.async_save.assert_not_awaited()


async def test_journal_stop_io_error_is_user_visible(coordinator):
    coordinator._store.async_save = AsyncMock()
    with patch.object(coordinator._shadow, "stop", side_effect=OSError("disk")):
        with pytest.raises(HomeAssistantError, match="nicht abgeschlossen"):
            await coordinator._async_stop_journal(refresh=False)
    coordinator._store.async_save.assert_not_awaited()


async def test_journal_status_publish_without_coordinator_data_is_noop(coordinator):
    coordinator.data = None
    coordinator.async_update_listeners = Mock()
    coordinator._publish_journal_status()
    coordinator.async_update_listeners.assert_not_called()


async def test_shadow_journal_reference_and_blocked_attempt_summary(coordinator, hass):
    coordinator.shadow_mode = True
    coordinator.device.blocked_write_attempts = "invalid"
    coordinator._shadow_snapshot = {"status": "running", "reference_entity": "sensor.mode"}
    hass.states.async_set("sensor.mode", "Akku Pause")
    data = {"mode": "Akku Pause"}
    stopped = {"status": "stopped", "session_id": "a" * 32}
    with patch.object(coordinator._shadow, "record", return_value=stopped) as record:
        await coordinator._async_record_journal(data, dt_util.utcnow())
    assert record.call_args.args[3] == "Akku Pause"
    assert data["shadow_summary"]["blocked_write_attempts"] == 0


async def test_internal_shadow_start_stop_refresh_paths(coordinator):
    coordinator.data = {"shadow_status": "idle"}
    coordinator._store.async_save = AsyncMock()
    coordinator.async_refresh = AsyncMock()

    await coordinator._async_start_journal(refresh=True)
    await coordinator._async_stop_journal(refresh=True)

    assert coordinator.async_refresh.await_count == 2


async def test_integration_stop_waits_for_journal_before_final_save(coordinator, hass):
    coordinator._online = True
    coordinator.data = {"shadow_status": "idle"}
    coordinator._store.async_save = AsyncMock()
    await coordinator.async_start_comparison()
    coordinator._store.async_save.reset_mock()
    original_append = coordinator._shadow._append
    entered = Event()
    release = Event()

    def blocking_append(row, *, exclusive=False):
        if row.get("type") == "sample":
            entered.set()
            assert release.wait(2)
        return original_append(row, exclusive=exclusive)

    sample = {
        "mode": "Akku Pause", "reason": "test", "online": True,
        "source_errors": {}, "device_errors": {}, "states": {},
        "write_enabled": False, "strategy_enabled": True, "price_status": "ready",
    }
    with patch.object(coordinator._shadow, "_append", side_effect=blocking_append):
        record = asyncio.create_task(
            coordinator._async_record_journal(sample, dt_util.utcnow()))
        assert await hass.async_add_executor_job(entered.wait, 2)
        stop = asyncio.create_task(coordinator.async_stop())
        await asyncio.sleep(0.05)
        coordinator._store.async_save.assert_not_awaited()
        release.set()
        await record
        await stop

    saved = coordinator._store.async_save.await_args.args[0]
    assert saved["shadow"]["status"] == "running"
    assert saved["shadow"]["samples"] == 1
    assert saved["shadow"] == coordinator._shadow_snapshot


async def test_static_metadata_is_reused_without_mutating_previous_results(coordinator):
    with patch("custom_components.opti_akku.coordinator._build_metadata",
               side_effect=AssertionError("static metadata rebuilt during update")):
        first = await coordinator._async_update_data()
        first["metadata"]["sensor.opti_soc"]["name"] = "Modified externally"
        second = await coordinator._async_update_data()
    assert second["metadata"]["sensor.opti_soc"]["name"] == "Ladezustand"
    assert second["metadata"]["sensor.opti_battery_power_w"]["device_class"] == "power"
