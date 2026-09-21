"""Copy Shadow configuration through real HA flows, without touching its store."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigEntryDisabler, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.config_flow import DEFINITIONS, SHADOW_COPY_OPTIONS
from custom_components.opti_akku.const import DOMAIN
from tests.test_config_flow import CONNECTION, PROBE, form_values, finish_wizard
from tests.test_arbitrage import CONFIG as ARBITRAGE_CONFIG


def shadow_entry(hass, *, data=None, options=None, settings=None, loaded=True,
                 unique_id="shadow:sma_stp_se:1234567890"):
    entry = MockConfigEntry(domain=DOMAIN, title="Opti Akku Shadow",
        unique_id=unique_id,
        data=data or {**CONNECTION, "serial_number": PROBE["serial_number"], "shadow_mode": True},
        options=options or {"sources": {}, "single_inverter": True, "settings_revision": "applied"})
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        settings={**{key: definition["default"] for key, definition in DEFINITIONS.items()},
                  **(settings or {})}, _settings_revision="applied", shadow_mode=True,
        write_enabled=False, connection_config=dict(entry.data))
    if loaded:
        entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


async def choose_shadow(hass, entry):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"backend": "shadow_copy"})
    assert result["step_id"] == "shadow_copy"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {
        "shadow_entry": entry.entry_id, "confirm_copy": True})


async def until_finish(hass, result):
    for _ in range(12):
        assert not result.get("errors"), result
        if result["step_id"] == "finish":
            return result
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result))
    raise AssertionError("Did not reach finish")


async def test_copies_effective_settings_but_never_runtime_or_permissions(hass, hass_storage):
    cfg = {"sources": {}, "single_inverter": True, "settings_revision": "pending",
           "settings": {"input_number.minsoc": 12}, "source_max_age": 0,
           "single_writer_confirmed": True, "write_enabled": True,
           "shadow_reference_mode": "sensor.old_mode", "migration": {"old": "data"},
           "future_unknown_option": {"write_enabled": True},
           "demand_forecast": {"enabled": True}, "ev_preparation": {"enabled": False},
           "source_observation": {"enabled": True}, "arbitrage_estimate": ARBITRAGE_CONFIG}
    source = shadow_entry(hass, options=cfg, settings={"input_number.minsoc": 8,
        "input_number.maxsoc": 87, "input_boolean.akku_opti_automatik": True,
        "input_boolean.opti_manuelle_ladegrenze": True})
    stored = {"version": 1, "minor_version": 1, "key": f"{DOMAIN}.{source.entry_id}",
              "data": {"write_enabled": True, "engine": {"private": 1}, "demand_forecast": {"hours": [1]}}}
    hass_storage[f"{DOMAIN}.{source.entry_id}"] = deepcopy(stored)
    before = deepcopy((dict(source.data), dict(source.options), vars(source.runtime_data)))
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)), \
         patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await choose_shadow(hass, source)
        assert form_values(result)["source_max_age"] == 0
        result = await until_finish(hass, result)
        assert form_values(result)["single_writer_confirmed"] is False
        assert form_values(result)["akku_opti_automatik"] is False
        result = await hass.config_entries.flow.async_configure(result["flow_id"],
            form_values(result, {"single_writer_confirmed": True}))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    created = result["result"]
    assert created.entry_id != source.entry_id
    assert created.unique_id == "sma_stp_se:1234567890"
    assert result["data"]["shadow_mode"] is False
    saved = result["options"]
    assert saved["settings"]["input_number.minsoc"] == 12
    assert saved["settings"]["input_number.maxsoc"] == 87
    assert saved["settings"]["input_boolean.opti_manuelle_ladegrenze"] is False
    assert saved["settings"]["input_boolean.akku_opti_automatik"] is False
    assert saved["settings_revision"] != "pending"
    for key in ("write_enabled", "migration", "future_unknown_option"):
        assert key not in saved
    assert saved.get("shadow_reference_mode") == ""
    for key in ("demand_forecast", "ev_preparation", "source_observation", "arbitrage_estimate"):
        assert all(saved[key][name] == value for name, value in cfg[key].items())
    assert before == (dict(source.data), dict(source.options), vars(source.runtime_data))
    assert hass_storage[f"{DOMAIN}.{source.entry_id}"] == stored
    assert f"{DOMAIN}.{created.entry_id}" not in hass_storage

    # Actually restore the new coordinator: no copied permissions or learned data.
    from custom_components.opti_akku.coordinator import OptiCoordinator
    from custom_components.opti_akku.engine import StrategyEngine
    from tests.test_coordinator import device
    engine = await hass.async_add_executor_job(StrategyEngine)
    coordinator = OptiCoordinator(hass, created, device(), engine)
    await coordinator.async_restore()
    assert coordinator.write_enabled is False
    assert coordinator.shadow_mode is False
    assert coordinator.settings["input_number.maxsoc"] == 87


@pytest.mark.parametrize("problem", ["no_confirmation", "unloaded", "not_shadow", "missing_identity", "changed_identity", "offline"])
async def test_copy_rejects_invalid_source_or_identity(hass, problem):
    source = shadow_entry(hass, loaded=problem != "unloaded")
    if problem in ("not_shadow", "missing_identity"):
        hass.config_entries.async_update_entry(source, data={**source.data,
            **({"shadow_mode": False} if problem == "not_shadow" else {"serial_number": None})})
        source.runtime_data.connection_config = dict(source.data)
    probe = AsyncMock(side_effect=OSError() if problem == "offline" else None,
        return_value={**PROBE, **({"serial_number": "different"} if problem == "changed_identity" else {})})
    with patch("custom_components.opti_akku.config_flow._probe", probe):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER},
            data={"backend": "shadow_copy"})
        if problem in ("unloaded", "not_shadow"):
            assert result["reason"] == "no_shadow_entries"
            return
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "shadow_entry": source.entry_id, "confirm_copy": problem != "no_confirmation"})
    expected = {"no_confirmation": "shadow_copy_confirmation_required", "missing_identity": "shadow_identity_missing",
                "changed_identity": "shadow_identity_changed", "offline": "cannot_connect"}
    assert result["errors"] == {"base": expected[problem]}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("change", ["options", "runtime", "removed", "during_probe", "identity", "offline", "duplicate"])
async def test_final_save_rechecks_source_identity_and_duplicate(hass, change):
    source = shadow_entry(hass)
    probe = AsyncMock(return_value=PROBE)
    with patch("custom_components.opti_akku.config_flow._probe", probe):
        result = await until_finish(hass, await choose_shadow(hass, source))
        if change == "options":
            hass.config_entries.async_update_entry(source, options={**source.options, "source_max_age": 30})
        elif change == "runtime":
            source.runtime_data.settings["input_number.maxsoc"] = 80
        elif change == "removed":
            # Simulate deletion without invoking unload on the synthetic runtime.
            source.mock_state(hass, ConfigEntryState.NOT_LOADED)
        elif change == "during_probe":
            async def changing_probe(*args):
                source.runtime_data.settings["input_number.maxsoc"] = 80
                return PROBE
            probe.side_effect = changing_probe
        elif change == "identity":
            probe.return_value = {**PROBE, "serial_number": "different"}
        elif change == "offline":
            probe.side_effect = TimeoutError()
        elif change == "duplicate":
            MockConfigEntry(domain=DOMAIN, data=CONNECTION, unique_id="sma_stp_se:1234567890",
                            disabled_by=ConfigEntryDisabler.USER).add_to_hass(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"],
            form_values(result, {"single_writer_confirmed": True}))
    if change == "duplicate":
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"
    else:
        expected = "shadow_identity_changed" if change == "identity" else "cannot_connect" if change == "offline" else "shadow_source_changed"
        assert result["step_id"] == "finish"
        assert result["errors"] == {"base": expected}
    assert source.data["shadow_mode"] is True


async def test_copy_requires_fresh_writer_consent_and_cancel_keeps_source(hass):
    source = shadow_entry(hass)
    before = deepcopy((dict(source.data), dict(source.options)))
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await until_finish(hass, await choose_shadow(hass, source))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result))
    assert result["errors"] == {"base": "shadow_writer_confirmation_required"}
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert before == (dict(source.data), dict(source.options))
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_shadow_options_explain_copy_without_mutation(hass):
    source = shadow_entry(hass)
    result = await hass.config_entries.options.async_init(source.entry_id)
    assert result["menu_options"][0] == "active_setup"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "active_setup"})
    assert result["step_id"] == "active_setup"
    assert result["data_schema"].schema == {}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "init"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_selected_shadow_unloads_while_selection_form_is_open(hass):
    source = shadow_entry(hass)
    shadow_entry(hass, unique_id="shadow:sma_stp_se:other",
                 data={**source.data, "host": "other-inverter.local", "serial_number": "other"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER},
        data={"backend": "shadow_copy"})
    source.mock_state(hass, ConfigEntryState.NOT_LOADED)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "shadow_entry": source.entry_id, "confirm_copy": True})
    assert result["step_id"] == "shadow_copy"
    assert result["errors"] == {"base": "shadow_source_changed"}


@pytest.mark.parametrize("strategy", [False, True])
async def test_huawei_copy_requires_real_control_bindings_without_service_calls(hass, strategy):
    from tests.test_huawei import SOURCES
    from tests.test_huawei_control import CONTROLS, make_controller
    controller, calls, _ = make_controller(hass)
    hass.states.async_set("sensor.house", 600, {"unit_of_measurement": "W"})
    device = controller.device
    source = shadow_entry(hass, data={"backend": "huawei_solar", "huawei_entry_id": device.entry_id,
        "huawei_device_id": device.device_id, "huawei_sources": SOURCES, "grid_positive": "export",
        "profile": "huawei_solar", "shadow_mode": True, "serial_number": "INV123"},
        options={"sources": {"house_consumption": "sensor.house"}, "single_inverter": False, "strategy_enabled": strategy})
    result = await choose_shadow(hass, source)
    assert result["step_id"] == "huawei_controls"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**CONTROLS, "huawei_grid_surplus": False})
    assert result["step_id"] == "sources"
    with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await finish_wizard(hass, result, {"finish": {"single_writer_confirmed": True}})
    assert result["data"]["shadow_mode"] is False
    assert result["data"]["huawei_controls"] == CONTROLS
    assert source.data["shadow_mode"] is True
    assert "huawei_controls" not in source.data
    assert calls == []


def test_copy_allowlist_excludes_permissions_and_runtime():
    assert not SHADOW_COPY_OPTIONS & {"write_enabled", "single_writer_confirmed", "migration", "engine", "shadow", "settings_revision"}


async def test_real_shadow_cycle_does_not_invalidate_reviewed_copy(hass):
    from custom_components.opti_akku.coordinator import OptiCoordinator
    from custom_components.opti_akku.engine import StrategyEngine
    from tests.test_coordinator import device
    source = shadow_entry(hass)
    engine = await hass.async_add_executor_job(StrategyEngine)
    backend = device()
    backend.async_probe.return_value = PROBE
    backend.async_read.return_value['sensor.opti_soc'] = 100
    runtime = OptiCoordinator(hass, source, backend, engine)
    source.runtime_data = runtime
    await runtime.async_restore()
    runtime.settings.update({'input_boolean.akku_opti_automatik': True,
                             'input_boolean.hausakku_aus_netz_laden': True,
                             'input_number.ladepreis': 0.4})
    with patch('custom_components.opti_akku.config_flow._probe', AsyncMock(return_value=PROBE)), \
         patch('custom_components.opti_akku.async_setup_entry', AsyncMock(return_value=True)):
        result = await until_finish(hass, await choose_shadow(hass, source))
        before = dict(runtime.settings)
        await runtime._async_update_data()
        assert runtime.settings != before
        assert runtime.settings['input_boolean.hausakku_aus_netz_laden'] is False
        result = await hass.config_entries.flow.async_configure(result['flow_id'],
            form_values(result, {'single_writer_confirmed': True}))
    assert result['type'] == FlowResultType.CREATE_ENTRY
    backend.async_apply.assert_not_awaited()


async def test_pending_patch_applied_during_copy_keeps_same_effective_settings(hass):
    source = shadow_entry(hass, options={'sources': {}, 'single_inverter': True,
        'settings_revision': 'next', 'settings': {'input_number.maxsoc': 88}})
    with patch('custom_components.opti_akku.config_flow._probe', AsyncMock(return_value=PROBE)), \
         patch('custom_components.opti_akku.async_setup_entry', AsyncMock(return_value=True)):
        result = await until_finish(hass, await choose_shadow(hass, source))
        source.runtime_data.settings['input_number.maxsoc'] = 88.0
        source.runtime_data._settings_revision = 'next'
        result = await hass.config_entries.flow.async_configure(result['flow_id'],
            form_values(result, {'single_writer_confirmed': True}))
    assert result['type'] == FlowResultType.CREATE_ENTRY
    assert result['options']['settings']['input_number.maxsoc'] == 88


async def test_copied_demand_revalidates_against_new_house_source(hass):
    hass.states.async_set('sensor.house', 500, {'unit_of_measurement': 'W'})
    source = shadow_entry(hass, options={'sources': {}, 'single_inverter': True,
        'demand_forecast': {'enabled': True, 'sources': {'heat_power': 'sensor.house'}}})
    with patch('custom_components.opti_akku.config_flow._probe', AsyncMock(return_value=PROBE)):
        result = await choose_shadow(hass, source)
        result = await hass.config_entries.flow.async_configure(result['flow_id'],
            form_values(result, {'house_consumption': 'sensor.house', 'single_inverter': False}))
        for _ in range(10):
            if result['step_id'] == 'demand':
                break
            result = await hass.config_entries.flow.async_configure(result['flow_id'], form_values(result))
        assert result['step_id'] == 'demand'
        result = await hass.config_entries.flow.async_configure(result['flow_id'], form_values(result))
    assert result['errors'] == {'heat_power': 'demand_heat_meter'}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_parallel_shadow_copy_is_not_started_twice(hass):
    source = shadow_entry(hass)
    with patch('custom_components.opti_akku.config_flow._probe', AsyncMock(return_value=PROBE)):
        first = await choose_shadow(hass, source)
        second = await choose_shadow(hass, source)
    assert first['step_id'] == 'sources'
    assert second['reason'] == 'already_in_progress'


@pytest.mark.parametrize('backend', [None, 'sma'])
async def test_older_sma_backend_copy(hass, backend):
    data = {**CONNECTION, 'shadow_mode': True, 'serial_number': PROBE['serial_number']}
    if backend is not None:
        data['backend'] = backend
    source = shadow_entry(hass, data=data)
    with patch('custom_components.opti_akku.config_flow._probe', AsyncMock(return_value=PROBE)), \
         patch('custom_components.opti_akku.async_setup_entry', AsyncMock(return_value=True)):
        result = await finish_wizard(hass, await choose_shadow(hass, source),
            {'finish': {'single_writer_confirmed': True}})
    assert result['result'].unique_id == 'sma_stp_se:1234567890'


async def test_copy_rejects_unsupported_probe(hass):
    source = shadow_entry(hass)
    with patch('custom_components.opti_akku.config_flow._probe', AsyncMock(return_value={**PROBE, 'inverter_status': None})):
        result = await choose_shadow(hass, source)
    assert result['errors'] == {'base': 'unsupported_device'}


@pytest.mark.parametrize('problem', ['identity', 'temperature', 'duplicate', 'controls'])
async def test_huawei_copy_final_checks_do_not_send_commands(hass, problem):
    from homeassistant.helpers import device_registry as dr
    from tests.test_huawei import SOURCES
    from tests.test_huawei_control import CONTROLS, make_controller
    controller, calls, _ = make_controller(hass)
    device = controller.device
    source = shadow_entry(hass, data={'backend': 'huawei_solar', 'huawei_entry_id': device.entry_id,
        'huawei_device_id': device.device_id, 'huawei_sources': SOURCES, 'grid_positive': 'export',
        'profile': 'huawei_solar', 'shadow_mode': True, 'serial_number': 'INV123'},
        options={'sources': {}, 'single_inverter': False, 'strategy_enabled': False})
    result = await choose_shadow(hass, source)
    if problem == 'controls':
        hass.states.async_remove(CONTROLS['charge_limit'])
        result = await hass.config_entries.flow.async_configure(result['flow_id'], CONTROLS)
        assert result['errors'] == {'base': 'huawei_write_access_missing'}
    else:
        result = await hass.config_entries.flow.async_configure(result['flow_id'], CONTROLS)
        result = await until_finish(hass, result)
        if problem == 'identity':
            dr.async_get(hass).async_update_device(device.device_id, serial_number='changed')
        elif problem == 'temperature':
            hass.states.async_remove(SOURCES['battery_temp'])
        else:
            MockConfigEntry(domain=DOMAIN, unique_id=f'huawei_solar:{device.device_id}',
                data={**source.data, 'shadow_mode': False}).add_to_hass(hass)
        result = await hass.config_entries.flow.async_configure(result['flow_id'],
            form_values(result, {'single_writer_confirmed': True}))
        if problem == 'duplicate':
            assert result['reason'] == 'already_configured'
        else:
            assert result['errors'] == {'base': 'shadow_identity_changed' if problem == 'identity' else 'huawei_temperature_required'}
    assert source.data['shadow_mode'] is True
    assert calls == []


def test_all_bundled_actions_changing_settings_are_volatile_for_copy():
    import json
    from pathlib import Path
    from custom_components.opti_akku.config_flow import SHADOW_COPY_VOLATILE_SETTINGS
    bundle = json.loads((Path(__file__).parents[1] / 'custom_components/opti_akku/resources/strategy.json').read_text())
    targets = set()
    def visit(value):
        if isinstance(value, dict):
            if 'action' in value:
                entities = value.get('target', {}).get('entity_id', [])
                targets.update([entities] if isinstance(entities, str) else entities)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(bundle)
    assert targets & DEFINITIONS.keys() == {'input_number.ladepreis', 'input_boolean.hausakku_aus_netz_laden'}
    assert targets & DEFINITIONS.keys() <= SHADOW_COPY_VOLATILE_SETTINGS


@pytest.mark.parametrize('configured', [False, True])
async def test_copy_reviews_existing_optional_sections_in_dependency_order(hass, configured):
    options = {'sources': {}, 'single_inverter': True}
    if configured:
        options.update(demand_forecast={'enabled': True}, ev_preparation={'enabled': False},
                       arbitrage_estimate=ARBITRAGE_CONFIG, source_observation={'enabled': True})
    source = shadow_entry(hass, options=options)
    steps = []
    with patch('custom_components.opti_akku.config_flow._probe', AsyncMock(return_value=PROBE)):
        result = await choose_shadow(hass, source)
        while result['step_id'] != 'finish':
            assert not result.get('errors')
            steps.append(result['step_id'])
            assert len(steps) < 20
            result = await hass.config_entries.flow.async_configure(result['flow_id'], form_values(result))
    assert steps[steps.index('notifications'):] == ['notifications', 'advanced'] + (
        ['demand', 'ev_preparation', 'arbitrage', 'observation'] if configured else [])
