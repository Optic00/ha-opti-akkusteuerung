"""Synthetic HA services; no inverter is contacted."""

from unittest.mock import AsyncMock, patch
import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from custom_components.opti_akku.device import PendingCommandError, StaleCommandError
from custom_components.opti_akku.huawei_control import (
    HuaweiController,
    HuaweiControlError,
    MSC,
    TOU,
)
from custom_components.opti_akku.sma import (
    MODES,
    PAUSE,
    AUTO,
    GRID_CHARGE,
    FAST_DISCHARGE,
    CHARGE_02C,
)
from tests.test_huawei import make_backend
from custom_components.opti_akku.huawei_control import NUMBER_KEYS

CONTROLS = {
    "mode": "select.test_mode",
    "excess_pv": "select.test_excess",
    "grid_charge": "switch.test_grid",
    "charge_limit": "number.test_charge",
    "discharge_limit": "number.test_discharge",
    "grid_limit": "number.test_grid",
    "cutoff_soc": "number.test_cutoff",
    "forced_status": "sensor.test_force",
}
PARAMS = dict(
    max_charge_w=4000,
    max_discharge_w=4000,
    charge_power_w=1500,
    charge_setpoint_w=2000,
    discharge_setpoint_w=1000,
    capacity_wh=10000,
    max_soc=95,
    min_charge_w=0,
    min_discharge_w=0,
)


def make_controller(hass, *, grid_surplus=False):
    device, entry, inverter, battery = make_backend(hass)
    dr.async_get(hass).async_update_device(battery.id, model="Batteries")
    device.grid_charge_for_surplus = grid_surplus
    registry = er.async_get(hass)
    for role, entity in CONTROLS.items():
        domain, name = entity.split(".")
        registry.async_get_or_create(
            domain,
            "huawei_solar",
            role,
            config_entry=entry,
            device_id=battery.id
            if role in ("mode", "excess_pv", "forced_status", "grid_charge")
            else inverter.id,
            suggested_object_id=name,
            translation_key=NUMBER_KEYS.get(role),
        )
        if role == "mode":
            value, attrs = MSC, {"options": [MSC, TOU]}
        elif role == "excess_pv":
            value, attrs = "charge", {"options": ["charge", "fed_to_grid"]}
        elif role == "grid_charge":
            value, attrs = "off", {}
        elif role == "forced_status":
            value, attrs = "Stopped", {}
        else:
            value = 100 if role == "cutoff_soc" else 5000
            attrs = {
                "min": 20 if role == "cutoff_soc" else 0,
                "max": 100 if role == "cutoff_soc" else 5000,
                "step": 0.1 if role == "cutoff_soc" else 1,
                "unit_of_measurement": "%" if role == "cutoff_soc" else "W",
            }
        hass.states.async_set(entity, value, attrs)
    calls = []

    def set_state(entity, value):
        hass.states.async_set(entity, value, hass.states.get(entity).attributes)

    async def handler(call):
        calls.append((call.domain, call.service, dict(call.data)))
        if call.domain == "homeassistant":
            set_state(CONTROLS["forced_status"], hass.states.get(CONTROLS["forced_status"]).state)
        elif call.domain == "number":
            set_state(call.data["entity_id"], call.data["value"])
        elif call.domain == "select":
            set_state(call.data["entity_id"], call.data["option"])
        elif call.domain == "switch":
            set_state(call.data["entity_id"], "on" if call.service == "turn_on" else "off")
        elif call.service == "set_tou_periods":
            set_state(CONTROLS["grid_charge"], "off")
        else:
            set_state(
                CONTROLS["forced_status"],
                "Stopped" if call.service == "stop_forcible_charge" else "Discharging",
            )

    for domain, services in {
        "number": ["set_value"],
        "select": ["select_option"],
        "switch": ["turn_on", "turn_off"],
        "huawei_solar": ["set_tou_periods", "stop_forcible_charge", "forcible_discharge"],
        "homeassistant": ["update_entity"],
    }.items():
        for service in services:
            hass.services.async_register(domain, service, handler)
    controller = HuaweiController(device, CONTROLS, sleep=AsyncMock())
    device.controller = controller
    return controller, calls, handler


@pytest.mark.parametrize("mode", MODES)
async def test_all_modes_confirm_targets(hass, mode):
    c, calls, _ = make_controller(hass)
    await c.apply(mode, PARAMS, lambda: True)
    assert c.last_mode == mode and c.last_write is not None
    if mode == PAUSE:
        assert float(hass.states.get(CONTROLS["charge_limit"]).state) == 0
        assert float(hass.states.get(CONTROLS["discharge_limit"]).state) == 0
    if mode in (GRID_CHARGE, "Akku schnell Laden", CHARGE_02C):
        assert hass.states.get(CONTROLS["mode"]).state == TOU
        assert hass.states.get(CONTROLS["grid_charge"]).state == "on"
        assert float(hass.states.get(CONTROLS["cutoff_soc"]).state) == 95
        i = next(i for i, (_, action, _) in enumerate(calls) if action == "set_tou_periods")
        assert all(
            data.get("value", 0) == 0
            for _, _, data in calls[:i]
            if data.get("entity_id") in (CONTROLS["charge_limit"], CONTROLS["discharge_limit"])
        )
    if mode == FAST_DISCHARGE:
        assert calls[-1][1:] == (
            "forcible_discharge",
            {"device_id": c.validate(), "power": 1000, "duration": 30},
        )


async def test_02c_capacity_and_ceiling(hass):
    c, _, _ = make_controller(hass)
    await c.apply(CHARGE_02C, {**PARAMS, "capacity_wh": 5000, "max_charge_w": 700}, lambda: True)
    assert float(hass.states.get(CONTROLS["charge_limit"]).state) == 700


@pytest.mark.parametrize("value", [19, float("nan"), -1, True])
async def test_invalid_cutoff_before_writes(hass, value):
    c, calls, _ = make_controller(hass)
    with pytest.raises(HuaweiControlError):
        await c.apply(GRID_CHARGE, {**PARAMS, "max_soc": value}, lambda: True)
    assert calls == []


async def test_shadow_cannot_write(hass):
    c, calls, _ = make_controller(hass)
    with pytest.raises(PermissionError):
        await c.device.async_apply(AUTO, PARAMS, lambda: True)
    assert calls == []


async def test_pending_readback_retries_once(hass):
    c, _, handler = make_controller(hass)
    attempted = 0

    async def delayed(call):
        nonlocal attempted
        if call.data.get("entity_id") == CONTROLS["charge_limit"]:
            attempted += 1
            if attempted == 1:
                return
        await handler(call)

    hass.services.async_register("number", "set_value", delayed)
    with patch("custom_components.opti_akku.huawei_control.time.monotonic", return_value=100):
        with pytest.raises(PendingCommandError):
            await c.apply(AUTO, PARAMS, lambda: True)
    assert c.last_write is None
    with patch("custom_components.opti_akku.huawei_control.time.monotonic", return_value=116):
        await c.apply(AUTO, PARAMS, lambda: True)
    assert c.last_mode == AUTO and attempted == 3


async def test_stale_force_status_not_confirmation(hass):
    c, _, _ = make_controller(hass)

    async def ignored(call):
        pass

    hass.services.async_register("huawei_solar", "stop_forcible_charge", ignored)
    with pytest.raises(PendingCommandError):
        await c.apply(AUTO, PARAMS, lambda: True)
    assert c.last_write is None and c._steps[0][0] == "force_stop"


async def test_stale_intent_cleanup_locks(hass):
    c, calls, handler = make_controller(hass)
    current = True

    async def supersede(call):
        nonlocal current
        await handler(call)
        current = False

    hass.services.async_register("number", "set_value", supersede)
    with pytest.raises(StaleCommandError) as exc:
        await c.apply(GRID_CHARGE, PARAMS, lambda: current)
    assert not exc.value.cleanup_failed
    assert not any(data.get("value", 0) > 0 for _, _, data in calls)
    assert not any(service == "set_tou_periods" for _, service, _ in calls)


async def test_foreign_battery_rejected(hass):
    c, calls, _ = make_controller(hass)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=c.device.entry_id,
        identifiers={("huawei_solar", "OTHER")},
        model="Batteries",
    )
    er.async_get(hass).async_update_entity(CONTROLS["forced_status"], device_id=other.id)
    with pytest.raises(HuaweiControlError):
        await c.apply(AUTO, PARAMS, lambda: True)
    assert calls == []


async def test_missing_service_blocks_activation(hass):
    c, calls, _ = make_controller(hass)
    hass.services.async_remove("huawei_solar", "forcible_discharge")
    with pytest.raises(HuaweiControlError):
        await c.apply(AUTO, PARAMS, lambda: True)
    assert not calls


async def test_shutdown_pause(hass):
    c, _, _ = make_controller(hass)
    c.device.read_only = False
    await c.apply(FAST_DISCHARGE, PARAMS, lambda: True)
    await c.device.async_shutdown_control()
    assert hass.states.get(CONTROLS["forced_status"]).state == "Stopped"
    assert float(hass.states.get(CONTROLS["charge_limit"]).state) == 0
    assert float(hass.states.get(CONTROLS["discharge_limit"]).state) == 0
    assert hass.states.get(CONTROLS["mode"]).state == MSC
    assert not c._steps


@pytest.mark.parametrize("enabled", [True, False])
async def test_surplus_setting(hass, enabled):
    c, _, _ = make_controller(hass, grid_surplus=enabled)
    await c.apply(AUTO, PARAMS, lambda: True)
    assert hass.states.get(CONTROLS["grid_charge"]).state == ("on" if enabled else "off")


async def test_cleanup_attempts_remaining_restrictions(hass):
    c, calls, _ = make_controller(hass)
    hass.states.async_set(CONTROLS["charge_limit"], "unavailable")
    assert not await c._cleanup()
    assert float(hass.states.get(CONTROLS["discharge_limit"]).state) == 0
    assert any(action == "stop_forcible_charge" for _, action, _ in calls)


async def test_unconfirmed_zero_never_enables_after_retry_exhausted(hass):
    c, calls, handler = make_controller(hass)

    async def ignored(call):
        if call.data.get("entity_id") != CONTROLS["charge_limit"]:
            await handler(call)

    hass.services.async_register("number", "set_value", ignored)
    for now in (100, 116):
        with patch("custom_components.opti_akku.huawei_control.time.monotonic", return_value=now):
            with pytest.raises(PendingCommandError):
                await c.apply(GRID_CHARGE, PARAMS, lambda: True)
    with patch("custom_components.opti_akku.huawei_control.time.monotonic", return_value=132):
        with pytest.raises(HuaweiControlError):
            await c.apply(GRID_CHARGE, PARAMS, lambda: True)
    assert not any(action == "set_tou_periods" for _, action, _ in calls)
    assert not any(data.get("value", 0) > 0 for _, _, data in calls)
    assert "FAILED" in c.last_error


async def test_completed_restriction_drift_aborts_enabling(hass):
    c, calls, handler = make_controller(hass)

    async def delayed(call):
        if call.data.get("entity_id") != CONTROLS["discharge_limit"]:
            await handler(call)

    hass.services.async_register("number", "set_value", delayed)
    with pytest.raises(PendingCommandError):
        await c.apply(GRID_CHARGE, PARAMS, lambda: True)
    hass.states.async_set(
        CONTROLS["charge_limit"], 5000, hass.states.get(CONTROLS["charge_limit"]).attributes
    )
    hass.services.async_register("number", "set_value", handler)
    with pytest.raises(HuaweiControlError, match="restriction changed"):
        await c.apply(GRID_CHARGE, PARAMS, lambda: True)
    assert not any(action == "set_tou_periods" for _, action, _ in calls)


async def test_invalid_new_request_pauses_previous_forced_discharge(hass):
    c, _, _ = make_controller(hass)
    await c.apply(FAST_DISCHARGE, PARAMS, lambda: True)
    with pytest.raises(HuaweiControlError):
        await c.apply(GRID_CHARGE, {**PARAMS, "max_soc": 10}, lambda: True)
    assert hass.states.get(CONTROLS["forced_status"]).state == "Stopped"
    assert float(hass.states.get(CONTROLS["discharge_limit"]).state) == 0


async def test_active_factory_defaults_off_then_explicit_manual_control(
    hass, enable_custom_integrations
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from tests.test_huawei import SOURCES

    c, calls, handler = make_controller(hass)
    entry = MockConfigEntry(
        domain="opti_akku",
        data={
            "backend": "huawei_solar",
            "profile": "huawei_solar",
            "shadow_mode": False,
            "huawei_entry_id": c.device.entry_id,
            "huawei_device_id": c.device.device_id,
            "huawei_sources": SOURCES,
            "grid_positive": "export",
            "huawei_controls": CONTROLS,
            "serial_number": "INV123",
        },
        options={
            "strategy_enabled": False,
            "plant_mode": "balance",
            "plant_meter_confirmed": True,
            "sources": {},
            "single_writer_confirmed": True,
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.opti_akku.async_get_unit", side_effect=AssertionError("No Modbus socket")
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # HA platform setup installs the generic entity services. Replace their
        # transport endpoints with the synthetic Huawei provider after setup.
        for domain, services in {
            "number": ["set_value"],
            "select": ["select_option"],
            "switch": ["turn_on", "turn_off"],
        }.items():
            for service in services:
                hass.services.async_register(domain, service, handler)
        coordinator = entry.runtime_data
        assert (
            PAUSE in coordinator.supported_modes and FAST_DISCHARGE in coordinator.supported_modes
        )
        assert not coordinator.write_enabled and not calls
        await coordinator.async_set_mode(PAUSE)
        await coordinator.async_set_write_enabled(True)
        assert coordinator.write_enabled
        assert coordinator._last_apply is not None
        assert calls
        await coordinator.async_set_write_enabled(False)
        assert not coordinator.write_enabled
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cancellation_finishes_cleanup_before_unlocking(hass):
    import asyncio

    c, calls, handler = make_controller(hass)
    first = True

    async def interrupted(call):
        nonlocal first
        if first:
            first = False
            raise asyncio.CancelledError
        await handler(call)

    hass.services.async_register("number", "set_value", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await c.apply(GRID_CHARGE, PARAMS, lambda: True)
    assert not c._lock.locked()
    assert float(hass.states.get(CONTROLS["charge_limit"]).state) == 0
    assert float(hass.states.get(CONTROLS["discharge_limit"]).state) == 0
    assert not any(action == "set_tou_periods" for _, action, _ in calls)


async def test_dynamic_target_drift_preserves_completed_restrictions(hass):
    from custom_components.opti_akku.sma import DYNAMIC

    c, calls, handler = make_controller(hass)

    async def delayed(call):
        if call.data.get("entity_id") != CONTROLS["discharge_limit"]:
            await handler(call)

    hass.services.async_register("number", "set_value", delayed)
    with pytest.raises(PendingCommandError):
        await c.apply(DYNAMIC, PARAMS, lambda: True)
    assert c._steps[0][0] == "discharge_limit"
    hass.services.async_register("number", "set_value", handler)
    hass.states.async_set(
        CONTROLS["discharge_limit"], 0, hass.states.get(CONTROLS["discharge_limit"]).attributes
    )
    await c.apply(DYNAMIC, {**PARAMS, "charge_power_w": 1300}, lambda: True)
    assert float(hass.states.get(CONTROLS["charge_limit"]).state) == 1300
    assert (
        sum(
            data.get("entity_id") == CONTROLS["charge_limit"] and data.get("value") == 0
            for _, _, data in calls
        )
        == 1
    )


async def test_force_status_refresh_is_requested_before_retry(hass):
    c, calls, _ = make_controller(hass)

    async def ignored(call):
        calls.append((call.domain, call.service, dict(call.data)))

    hass.services.async_register("huawei_solar", "stop_forcible_charge", ignored)
    with pytest.raises(PendingCommandError):
        await c.apply(AUTO, PARAMS, lambda: True)
    await c.apply(AUTO, PARAMS, lambda: True)
    assert sum(service == "stop_forcible_charge" for _, service, _ in calls) == 1
    assert any(
        domain == "homeassistant" and service == "update_entity" for domain, service, _ in calls
    )


async def test_steady_pause_does_not_repeat_force_stop(hass):
    c, calls, _ = make_controller(hass)
    await c.apply(PAUSE, PARAMS, lambda: True)
    calls.clear()
    await c.apply(PAUSE, PARAMS, lambda: True)
    assert calls == []


async def test_successful_cleanup_is_not_repeated_on_immediate_shutdown(hass):
    c, calls, _ = make_controller(hass)
    c.device.read_only = False
    assert await c._cleanup()
    calls.clear()
    await c.device.async_shutdown_control()
    assert calls == []


async def test_wrong_huawei_number_parameter_is_rejected(hass):
    c, calls, _ = make_controller(hass)
    er.async_get(hass).async_update_entity(
        CONTROLS["charge_limit"], translation_key="active_power_fixed_value_derating"
    )
    with pytest.raises(HuaweiControlError, match="Wrong Huawei battery parameter"):
        await c.apply(AUTO, PARAMS, lambda: True)
    assert calls == []


async def test_step_budget_yields_before_cumulative_service_latency(hass):
    c, _, handler = make_controller(hass)
    clock = 100.0

    async def slow(call):
        nonlocal clock
        await handler(call)
        clock += 6

    hass.services.async_register("number", "set_value", slow)
    with patch(
        "custom_components.opti_akku.huawei_control.time.monotonic", side_effect=lambda: clock
    ):
        with pytest.raises(PendingCommandError):
            await c.apply(GRID_CHARGE, PARAMS, lambda: clock < 130)
    assert clock == 106
    assert c._steps[0][0] == "discharge_limit"
    assert not c.last_error


async def test_grid_charge_uses_dynamic_target_not_fast_charge_preset(hass):
    c, _, _ = make_controller(hass)
    await c.apply(GRID_CHARGE, {**PARAMS, "charge_power_w": 600, "charge_setpoint_w": 3000}, lambda: True)
    assert float(hass.states.get(CONTROLS["charge_limit"]).state) == 600
    assert float(hass.states.get(CONTROLS["grid_limit"]).state) == 600
