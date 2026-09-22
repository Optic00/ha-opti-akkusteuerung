"""Real HA 2026.9.1 + actual shared Modbus library + TCP loopback simulator."""

import asyncio

import pytest
from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.components.modbus.connection import DATA_MODBUS_CONNECTIONS
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from modbus_connection import ModbusTcpParams

from custom_components.opti_akku.const import DOMAIN
from tests.simulator import SimulatedInverter


@pytest.fixture
async def simulator(socket_enabled, hass):
    simulator = await SimulatedInverter().start()
    yield simulator
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)
    await simulator.close()


async def configure(hass, simulator, *, shadow=False):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"backend": "sma_modbus"})
    assert result["step_id"] == "sma_connection"
    result = await hass.config_entries.flow.async_configure(result["flow_id"],
        {"host": "127.0.0.1", "port": simulator.port, "unit_id": 3, "profile": "sma_stp_se", "shadow_mode": shadow})
    assert result["step_id"] == "sources", result
    from tests.test_config_flow import finish_wizard
    result = await finish_wizard(hass, result, {"sources": {
        "single_inverter": True, "plant_meter_confirmed": True},
        "finish": {} if shadow else {"single_writer_confirmed": True}})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = result["result"]
    await hass.async_block_till_done()
    return entry


async def test_shadow_never_writes_across_real_ha_lifecycle(hass, simulator, fail_on_log_exception):
    from homeassistant.exceptions import HomeAssistantError

    entry = await configure(hass, simulator, shadow=True)
    coordinator = entry.runtime_data
    assert coordinator.shadow_mode
    assert entry.options["single_writer_confirmed"] is False
    assert not hass.data.get("opti_akku_serial_owners", {})
    assert not hass.data.get("opti_akku_writers", {})
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_write_enabled") is None
    with pytest.raises(HomeAssistantError, match="Shadow"):
        await coordinator.async_set_write_enabled(True)
    button = registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_shadow_start")
    assert button
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    deadline = coordinator.data["shadow_summary"]["deadline"]
    assert coordinator.data["shadow_status"] == "running"
    with pytest.raises(HomeAssistantError):
        await coordinator.async_start_shadow()
    coordinator.write_enabled = True  # Exercise defense against corrupted/restored flags.
    await coordinator.async_refresh()
    await coordinator.async_set_write_enabled(False)
    await coordinator._store.async_save({**coordinator._stored_data(), "write_enabled": True})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.write_enabled is False
    assert coordinator.data["shadow_summary"]["deadline"] == deadline
    old_session = coordinator.data["shadow_summary"]["session_id"]
    stop_button = registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_shadow_stop")
    assert stop_button
    await hass.services.async_call("button", "press", {"entity_id": stop_button}, blocking=True)
    assert coordinator.data["shadow_status"] == "stopped"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.data["shadow_status"] == "stopped"
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert coordinator.data["shadow_status"] == "running"
    assert coordinator.data["shadow_summary"]["session_id"] != old_session
    coordinator.write_enabled = True
    await hass.async_stop()
    await hass.async_block_till_done()
    assert not simulator.writes, "Shadow setup, start, refresh, reload and HA stop must send zero write FCs"
    assert coordinator.device.blocked_write_attempts == 0


async def test_config_flow_read_write_and_shared_connection(hass, simulator, fail_on_log_exception):
    entry = await configure(hass, simulator)
    coordinator = entry.runtime_data
    assert coordinator.data["states"]["sensor.opti_soc"] == "60"
    assert coordinator.data["identity"]["serial_number"] == "123456789"
    assert not simulator.writes, "Setup and read-only probe must never write"
    assert entry.options["sources"] == {}, "Fresh install must not need legacy aliases"
    test_button = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_notification_test")
    assert test_button
    comparison_start = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_comparison_start")
    comparison_stop = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_comparison_stop")
    comparison_status = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_diagnostic_comparison_status")
    assert comparison_start and comparison_stop and comparison_status
    assert hass.states.get(comparison_status).state == "idle"
    await hass.services.async_call("button", "press", {"entity_id": test_button}, blocking=True)
    assert not simulator.writes, "Notification test cannot touch inverter registers"
    assert len(hass.data[DATA_MODBUS_CONNECTIONS]) == 1
    accepted = simulator.accepted
    async with async_get_temporary_unit(hass, ModbusTcpParams(host="127.0.0.1", port=simulator.port), 3) as other_unit:
        assert await other_unit.read_holding_registers(30845, 2) == [0, 60]
        assert simulator.accepted == accepted, "The second consumer must reuse HA's socket"

    # Exercise the entities' real HA service handlers, not coordinator methods.
    registry = er.async_get(hass)
    master = registry.async_get_entity_id("switch", "opti_akku", f"{entry.entry_id}_input_boolean.akku_opti_automatik")
    writes = registry.async_get_entity_id("switch", "opti_akku", f"{entry.entry_id}_write_enabled")
    if writes is None:
        writes = registry.async_get_entity_id("switch", "opti_akku", f"{entry.entry_id}_enable_writes")
    assert master and writes
    await hass.services.async_call("switch", "turn_on", {"entity_id": master}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": writes}, blocking=True)
    await hass.async_block_till_done()
    assert simulator.writes
    assert coordinator.data["last_write"] is not None
    assert any(address == 40151 for address, values in simulator.writes)
    assert coordinator.data["last_error"] is None
    await hass.services.async_call("switch", "turn_off", {"entity_id": writes}, blocking=True)
    await hass.async_block_till_done()
    assert coordinator.write_enabled is False
    assert simulator.writes[-1] == (41259, [0, 303])
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.data[DATA_MODBUS_CONNECTIONS]
    await asyncio.sleep(0)


async def test_missing_soc_causes_pause_on_real_transport(hass, simulator, fail_on_log_exception):
    entry = await configure(hass, simulator)
    coordinator = entry.runtime_data
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.manual_mode = "Akku schnell Entladen"
    coordinator.write_enabled = True
    simulator.set_u32(30845, 0xFFFFFFFF)
    await coordinator.async_refresh()
    assert coordinator.data["states"]["sensor.opti_soc"] == "unavailable"
    assert coordinator.data["mode"] == "Akku Pause"
    assert simulator.writes[-1] == (41259, [0, 303])
    assert not any(address == 40149 for address, values in simulator.writes)
    coordinator.write_enabled = False
    assert await hass.config_entries.async_unload(entry.entry_id)

async def test_options_settings_do_not_reload_running_shadow(hass, simulator):
    from unittest.mock import patch
    from tests.test_config_flow import form_values
    from homeassistant.helpers.entity import EntityCategory

    entry = await configure(hass, simulator, shadow=True)
    coordinator = entry.runtime_data
    await coordinator.async_start_shadow()
    deadline = coordinator.data["shadow_summary"]["deadline"]
    registry = er.async_get(hass)
    old_ids = {x.unique_id: x.entity_id for x in er.async_entries_for_config_entry(registry, entry.entry_id)}
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "battery"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"maxsoc": 90}))
    # An unrelated concurrent entity edit must not be overwritten by the draft.
    await coordinator.async_set_setting("input_number.opti_forecast_optimismus", 35)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    with patch.object(hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload) as reload:
        await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
        await hass.async_block_till_done()
        reload.assert_not_called()
    assert entry.runtime_data is coordinator
    assert coordinator.settings["input_number.maxsoc"] == 90
    assert coordinator.settings["input_number.opti_forecast_optimismus"] == 35
    assert coordinator.data["shadow_summary"]["deadline"] == deadline
    assert not simulator.writes
    assert old_ids == {x.unique_id: x.entity_id for x in er.async_entries_for_config_entry(registry, entry.entry_id)}
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if "_input_number." in entity.unique_id:
            assert entity.entity_category == EntityCategory.CONFIG
        if entity.unique_id.endswith("_sensor.house_battery_load_30_mins"):
            assert entity.entity_category == EntityCategory.DIAGNOSTIC
            assert entity.disabled_by is not None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.settings["input_number.maxsoc"] == 90
    assert entry.runtime_data.settings["input_number.opti_forecast_optimismus"] == 35

async def test_options_patch_preserves_engine_update_while_waiting_for_lock(hass, simulator):
    from tests.test_config_flow import form_values

    entry = await configure(hass, simulator, shadow=True)
    coordinator = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "battery"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"maxsoc": 90}))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    async with coordinator._update_lock:
        await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
        assert entry.options["settings"] == {"input_number.maxsoc": 90}
        await asyncio.sleep(0)
        # Same write as engine.helper_updates while its update cycle owns the lock.
        coordinator.settings["input_number.ladepreis"] = 0.43
    await hass.async_block_till_done()
    assert coordinator.settings["input_number.ladepreis"] == 0.43
    assert coordinator.settings["input_number.maxsoc"] == 90
    assert not simulator.writes


async def test_invalid_pending_settings_patch_is_discarded(hass, simulator):
    entry = await configure(hass, simulator, shadow=True)
    coordinator = entry.runtime_data
    old_settings = dict(coordinator.settings)
    old_revision = coordinator._settings_revision
    hass.config_entries.async_update_entry(entry, options={**entry.options,
        "settings": {"input_number.minsoc": 99}, "settings_revision": "invalid"})
    await hass.async_block_till_done()
    assert coordinator.settings == old_settings
    assert entry.options["settings"] == {}
    assert entry.options["settings_revision"] == old_revision
    assert "nicht übernommen" in coordinator.data["last_error"]
    assert not simulator.writes

@pytest.mark.parametrize("language,connection_name,power_name", [("de", "Verbindung", "Batterieleistung"), ("en", "Connection", "Battery power")])
async def test_translated_entity_names_reach_ha(hass, simulator, language, connection_name, power_name):
    hass.config.language = language
    entry = await configure(hass, simulator, shadow=True)
    registry = er.async_get(hass)
    for domain, key, name in [("binary_sensor", "binary_sensor.opti_connection", connection_name),
                              ("sensor", "sensor.opti_battery_power_w", power_name)]:
        entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}")
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["friendly_name"] == f"Opti Akku Shadow {name}"


async def test_settings_entities_opt_in_and_ev_requires_sources(hass, simulator):
    from homeassistant.exceptions import HomeAssistantError
    entry = await configure(hass, simulator, shadow=True)
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    config_entities = [e for e in entities if "_input_number." in e.unique_id or "_input_boolean." in e.unique_id]
    for entity in config_entities:
        if entity.unique_id.endswith("_input_boolean.akku_opti_automatik"):
            assert entity.disabled_by is None
        else:
            assert entity.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    coordinator = entry.runtime_data
    with pytest.raises(HomeAssistantError, match="evcc"):
        await coordinator.async_set_setting("input_boolean.opti_ev_akku_pause", True)
    assert coordinator.settings["input_boolean.opti_ev_akku_pause"] is False
    with pytest.raises(HomeAssistantError, match="evcc"):
        await coordinator.async_apply_settings({"input_boolean.opti_ev_akku_pause": True}, "invalid-ev")
    # Explicit user enablement must survive a reload, with the same entity ID.
    number = next(e for e in config_entities if e.unique_id.endswith("_input_number.minsoc"))
    registry.async_update_entity(number.entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(number.entity_id).disabled_by is None
    assert hass.states.get(number.entity_id) is not None
    assert not simulator.writes
