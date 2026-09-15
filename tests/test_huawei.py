from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.huawei import HuaweiConfigurationError, HuaweiDevice


SOURCES = {
    "soc": "sensor.huawei_soc",
    "battery_capacity_kwh": "sensor.huawei_capacity",
    "battery_power_w": "sensor.huawei_battery_power",
    "pv_power_w": "sensor.huawei_active_power",
    "grid_power_w": "sensor.huawei_grid_power",
    "battery_temp": "sensor.huawei_battery_temp",
    "pv_generation_w": "sensor.huawei_input_power",
}
VALUES = {
    "soc": (55, "%"),
    "battery_capacity_kwh": (12800, "Wh"),
    "battery_power_w": (-1.2, "kW"),
    "pv_power_w": (3.4, "kW"),
    "grid_power_w": (500, "W"),
    "battery_temp": (23.5, "°C"),
    "pv_generation_w": (4.1, "kW"),
}


def make_entry(hass, domain="huawei_solar", loaded=True):
    entry = MockConfigEntry(domain=domain)
    entry.add_to_hass(hass)
    if loaded:
        entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


def make_backend(hass, *, entry=None, source_device="child", **kwargs):
    entry = entry or make_entry(hass)
    devices = dr.async_get(hass)
    inverter = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("huawei_solar", "INV123")},
        manufacturer="Huawei",
        model="SUN2000-10KTL-M1",
        serial_number="INV123",
    )
    child = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("huawei_solar", "INV123/storage")},
        via_device_id=inverter.id,
    )
    entity_device_id = child.id if source_device == "child" else inverter.id
    registry = er.async_get(hass)
    for role, entity_id in SOURCES.items():
        registry.async_get_or_create(
            "sensor",
            "huawei_solar",
            f"INV123_{role}",
            config_entry=entry,
            device_id=entity_device_id,
            suggested_object_id=entity_id.removeprefix("sensor."),
        )
        value, unit = VALUES[role]
        hass.states.async_set(entity_id, value, {"unit_of_measurement": unit})
    return HuaweiDevice(
        hass,
        entry_id=entry.entry_id,
        device_id=inverter.id,
        sources=SOURCES,
        **kwargs,
    ), entry, inverter, child


async def test_probe_uses_device_registry_identity(hass):
    backend, _, _, _ = make_backend(hass)
    assert await backend.async_probe() == {
        "manufacturer": "Huawei",
        "model": "SUN2000-10KTL-M1",
        "serial_number": "INV123",
        "backend": "huawei_solar",
        "backend_health": True,
        "inverter_status": True,
    }


async def test_read_normalizes_units_and_signed_grid(hass):
    backend, _, _, _ = make_backend(hass)
    values = await backend.async_read()
    assert values["sensor.opti_soc"] == 55
    assert values["sensor.opti_battery_capacity_kwh"] == 12.8
    assert values["sensor.opti_battery_power_w"] == -1200
    assert values["sensor.opti_pv_power_w"] == 3400
    assert values["sensor.opti_pv_generation_w"] == 4100
    assert values["sensor.opti_grid_import_w"] == 0
    assert values["sensor.opti_grid_export_w"] == 500
    assert values["sensor.opti_inverter_status"] == "unavailable"
    assert not backend.last_read_errors


async def test_explicit_import_positive_grid_sign(hass):
    backend, _, _, _ = make_backend(hass, grid_positive="import")
    values = await backend.async_read()
    assert values["sensor.opti_grid_import_w"] == 500
    assert values["sensor.opti_grid_export_w"] == 0


@pytest.mark.parametrize(
    ("role", "value", "unit", "error"),
    [
        ("soc", "unavailable", "%", "invalid_value"),
        ("soc", float("nan"), "%", "invalid_value"),
        ("soc", True, "%", "invalid_value"),
        ("battery_capacity_kwh", 10, "J", "unsupported_unit"),
    ],
)
async def test_invalid_values_fail_closed(hass, role, value, unit, error):
    backend, _, _, _ = make_backend(hass)
    hass.states.async_set(SOURCES[role], value, {"unit_of_measurement": unit})
    values = await backend.async_read()
    key = {
        "soc": "sensor.opti_soc",
        "battery_capacity_kwh": "sensor.opti_battery_capacity_kwh",
    }[role]
    assert values[key] is None
    assert backend.last_read_errors[role] == error


async def test_missing_and_stale_states_fail_closed(hass):
    backend, _, _, _ = make_backend(hass)
    hass.states.async_remove(SOURCES["soc"])
    old = dt_util.utcnow() - timedelta(hours=1)
    hass.states.async_set(SOURCES["pv_power_w"], 1000, {"unit_of_measurement": "W"})
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(dt_util, "utcnow", lambda: old + timedelta(hours=2))
        values = await backend.async_read()
    assert values["sensor.opti_soc"] is None
    assert values["sensor.opti_pv_power_w"] is None
    assert backend.last_read_errors["soc"] == "missing_or_stale"
    assert backend.last_read_errors["pv_power_w"] == "missing_or_stale"


@pytest.mark.parametrize(("domain", "loaded"), [("other", True), ("huawei_solar", False)])
async def test_probe_rejects_wrong_or_unloaded_entry(hass, domain, loaded):
    entry = make_entry(hass, domain=domain, loaded=loaded)
    backend, _, _, _ = make_backend(hass, entry=entry)
    with pytest.raises(HuaweiConfigurationError):
        await backend.async_probe()


async def test_probe_rejects_entity_from_unrelated_device(hass):
    backend, entry, _, _ = make_backend(hass)
    devices = dr.async_get(hass)
    unrelated = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("huawei_solar", "OTHER")},
    )
    registry = er.async_get(hass)
    registry.async_update_entity(SOURCES["soc"], device_id=unrelated.id)
    with pytest.raises(HuaweiConfigurationError):
        await backend.async_probe()


@pytest.mark.parametrize("kind", ["platform", "entry"])
async def test_probe_rejects_entity_from_other_platform_or_entry(hass, kind):
    backend, entry, inverter, _ = make_backend(hass)
    registry = er.async_get(hass)
    source_entry = entry
    platform = "template"
    if kind == "entry":
        source_entry = make_entry(hass)
        platform = "huawei_solar"
    entity = registry.async_get_or_create(
        "sensor",
        platform,
        f"wrong_{kind}",
        config_entry=source_entry,
        device_id=inverter.id,
        suggested_object_id=f"wrong_{kind}",
    )
    bad_sources = {**SOURCES, "soc": entity.entity_id}
    hass.states.async_set(entity.entity_id, 50, {"unit_of_measurement": "%"})
    bad = HuaweiDevice(
        hass,
        entry_id=backend.entry_id,
        device_id=backend.device_id,
        sources=bad_sources,
    )
    with pytest.raises(HuaweiConfigurationError):
        await bad.async_probe()


async def test_probe_rejects_missing_or_foreign_selected_device(hass):
    backend, _, _, _ = make_backend(hass)
    missing = HuaweiDevice(
        hass,
        entry_id=backend.entry_id,
        device_id="missing-device",
        sources=SOURCES,
    )
    with pytest.raises(HuaweiConfigurationError):
        await missing.async_probe()

    other_entry = make_entry(hass)
    foreign = dr.async_get(hass).async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={("huawei_solar", "FOREIGN")},
    )
    wrong = HuaweiDevice(
        hass,
        entry_id=backend.entry_id,
        device_id=foreign.id,
        sources=SOURCES,
    )
    with pytest.raises(HuaweiConfigurationError):
        await wrong.async_probe()


async def test_apply_is_blocked_without_service_calls(hass):
    backend, _, _, _ = make_backend(hass)
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as call:
        with pytest.raises(PermissionError):
            await backend.async_apply("Akku Pause", {}, lambda: True)
        call.assert_not_awaited()


async def test_real_opti_huawei_setup_never_acquires_modbus_or_writes(hass, enable_custom_integrations):
    from unittest.mock import patch
    from homeassistant.exceptions import HomeAssistantError
    backend, source_entry, inverter, _ = make_backend(hass)
    opti = MockConfigEntry(domain="opti_akku", title="Huawei shadow", data={
        "backend": "huawei_solar", "profile": "huawei_solar", "shadow_mode": True,
        "huawei_entry_id": source_entry.entry_id, "huawei_device_id": inverter.id,
        "huawei_sources": SOURCES, "grid_positive": "export"}, options={
        "strategy_enabled": True, "plant_mode": "balance", "plant_meter_confirmed": True,
        "forecast_min_load_w": 0, "sources": {}})
    opti.add_to_hass(hass)
    with patch("custom_components.opti_akku.async_get_unit", side_effect=AssertionError("No Modbus acquisition")):
        assert await hass.config_entries.async_setup(opti.entry_id)
        await hass.async_block_till_done()
        coordinator = opti.runtime_data
        assert coordinator.data["identity"]["manufacturer"] == "Huawei"
        assert coordinator.shadow_mode
        assert coordinator.supported_modes == ()
        assert coordinator.data["online"]
        hass.states.async_set(SOURCES["pv_generation_w"], "unavailable", {"unit_of_measurement": "kW"})
        await coordinator.async_refresh()
        assert coordinator.data["online"] is True
        assert coordinator.data["device_errors"] == {"pv_generation_w": "invalid_value"}
        hass.states.async_set(SOURCES["soc"], "unavailable", {"unit_of_measurement": "%"})
        await coordinator.async_refresh()
        assert coordinator.data["online"] is False
        assert "soc" in coordinator.data["device_errors"]
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_write_enabled(True)
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_mode("Akku schnell Laden")
        assert await hass.config_entries.async_unload(opti.entry_id)


async def test_huawei_factory_requires_controls_for_standard_entry(hass):
    from custom_components.opti_akku import async_setup_entry
    from homeassistant.exceptions import ConfigEntryError
    entry = MockConfigEntry(domain="opti_akku", data={"backend": "huawei_solar", "shadow_mode": False})
    entry.add_to_hass(hass)
    with pytest.raises(ConfigEntryError, match="actuator bindings"):
        await async_setup_entry(hass, entry)


async def test_plant_meter_sibling_is_allowed_but_battery_sibling_is_not(hass):
    backend, entry, inverter, _ = make_backend(hass)
    devices = dr.async_get(hass)
    primary = devices.async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("huawei_solar", "PRIMARY")})
    devices.async_update_device(inverter.id, via_device_id=primary.id)
    meter = devices.async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("huawei_solar", "PLANT_METER")}, via_device_id=primary.id)
    registry = er.async_get(hass)
    registry.async_update_entity(SOURCES["grid_power_w"], device_id=meter.id)
    assert (await backend.async_probe())["backend_health"] is True
    registry.async_update_entity(SOURCES["soc"], device_id=meter.id)
    with pytest.raises(HuaweiConfigurationError):
        await backend.async_probe()


async def test_malformed_huawei_constructor_is_config_entry_error(hass):
    from custom_components.opti_akku import async_setup_entry
    from homeassistant.exceptions import ConfigEntryError
    entry = MockConfigEntry(domain="opti_akku", data={"backend": "huawei_solar", "shadow_mode": True})
    entry.add_to_hass(hass)
    with pytest.raises(ConfigEntryError, match="Invalid Huawei configuration"):
        await async_setup_entry(hass, entry)
