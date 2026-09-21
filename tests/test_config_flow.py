"""HA-native onboarding, source validation, and cancellation semantics."""
from unittest.mock import AsyncMock, patch
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from datetime import timedelta
from types import SimpleNamespace
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.opti_akku.const import DOMAIN
from tests.test_price_sources import dated_prices

CONNECTION = {"host": "192.0.2.10", "port": 502, "unit_id": 3, "profile": "sma_stp_se", "shadow_mode": False}
PROBE = {"inverter_status": 235, "model": "STP10.0-3SE-40", "serial_number": "1234567890"}
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def configure_options(hass, flow_id, user_input):
    """Navigate the real submenu before submitting an existing feature test."""
    section = user_input.get("next_step_id")
    parents = {
        **dict.fromkeys(("ev_preparation", "demand", "arbitrage", "notifications"), "features"),
        **dict.fromkeys(("connection", "observation", "advanced"), "maintenance"),
    }
    if section in parents and hass.config_entries.options.async_get(flow_id)["step_id"] == "init":
        await hass.config_entries.options.async_configure(flow_id, {"next_step_id": parents[section]})
    return await hass.config_entries.options.async_configure(flow_id, user_input)


def form_values(result, overrides=None):
    """Act like the frontend: submit displayed defaults and suggestions."""
    values = {}
    for marker in result["data_schema"].schema:
        if callable(marker.default):
            value = marker.default()
            import voluptuous as vol
            if value is not vol.UNDEFINED:
                values[marker.schema] = value
        if marker.description and "suggested_value" in marker.description:
            values[marker.schema] = marker.description["suggested_value"]
    values.update(overrides or {})
    return values


async def finish_wizard(hass, result, overrides=None):
    overrides = overrides or {}
    for _ in range(10):
        if result["type"] == FlowResultType.CREATE_ENTRY:
            return result
        assert not result.get("errors"), result
        values = form_values(result, overrides.get(result["step_id"], {}))
        result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
    raise AssertionError("Wizard did not finish")


async def begin(hass, connection=None):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER}, data=connection or CONNECTION)


async def test_complete_wizard_seeds_settings_and_never_arms(hass):
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)), patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await finish_wizard(hass, await begin(hass), {"sources": {"single_inverter": True, "plant_meter_confirmed": True}, "battery": {"minsoc": 7, "maxsoc": 90}, "finish": {"single_writer_confirmed": True}})
    assert result["options"]["settings"]["input_number.minsoc"] == 7
    assert result["options"]["settings"]["input_number.maxsoc"] == 90
    assert result["options"]["settings"]["input_boolean.akku_opti_automatik"] is False
    assert result["options"]["single_writer_confirmed"] is True
    assert result["result"].unique_id == "sma_stp_se:1234567890"


async def test_shadow_default_and_no_writer_field(hass):
    connection = {k: v for k, v in CONNECTION.items() if k != "shadow_mode"}
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)), patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await finish_wizard(hass, await begin(hass, connection), {"sources": {"single_inverter": True, "plant_meter_confirmed": True}})
    assert result["data"]["shadow_mode"] is True
    assert result["options"]["single_writer_confirmed"] is False
    assert result["result"].unique_id.startswith("shadow:")


async def test_config_flow_cannot_connect(hass):
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(side_effect=OSError("offline"))):
        result = await begin(hass)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_initial_unsupported_device_preserves_connection_for_retry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"backend": "sma_modbus"}
    )
    submitted = {
        "host": " inverter.local ",
        "port": 1502,
        "unit_id": 7,
        "profile": "sma_stp_se",
        "shadow_mode": True,
        "migrate_legacy": False,
    }
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value={})):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], submitted)

    assert result["step_id"] == "sma_connection"
    assert result["errors"] == {"base": "unsupported_device"}
    assert form_values(result) == {**submitted, "host": "inverter.local"}

    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], form_values(result)
        )
    assert result["step_id"] == "sources"


async def test_serial_alias_is_duplicate(hass):
    MockConfigEntry(domain=DOMAIN, unique_id="sma_stp_se:1234567890", data=CONNECTION).add_to_hass(hass)
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass, {**CONNECTION, "host": "alias"})
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("state,unit,error", [("300", "kWh", "unsupported_unit"), ("unavailable", "W", "invalid_value"), ("-4", "W", "negative_value")])
async def test_source_validation(hass, state, unit, error):
    hass.states.async_set("sensor.house", state, {"unit_of_measurement": unit})
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "house_consumption": "sensor.house", "single_inverter": False,
            "plant_mode": "external",
        })
    assert result["errors"]["house_consumption"] == error


async def test_missing_house_and_stale_source(hass):
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "single_inverter": False, "plant_mode": "external",
        })
        assert result["errors"]["house_consumption"] == "house_required"
        hass.states.async_set("sensor.house", "500", {"unit_of_measurement": "W"})
        with patch("custom_components.opti_akku.config_flow.dt_util.utcnow", return_value=dt_util.utcnow()+timedelta(hours=1)):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {
                "house_consumption": "sensor.house", "single_inverter": False,
                "plant_mode": "external",
            })
    assert result["errors"]["house_consumption"] == "missing_or_stale"


async def test_price_unit_mismatch_and_incomplete_ev(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": {}, "single_inverter": True})
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.price", "30", {"unit_of_measurement": "ct/kWh", "today":
        dated_prices(dt_util.now().date(), timezone=dt_util.DEFAULT_TIME_ZONE, price=30)})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "tariff"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"price_current": "sensor.price", "price_series": "sensor.price", "price_unit": "EUR/kWh"}))
    assert result["errors"]["price_current"] == "price_unit_mismatch"
    hass.config_entries.options.async_abort(result["flow_id"])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "features"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "ev"})
    hass.states.async_set("sensor.ev_power", "1000", {"unit_of_measurement": "W"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"ev1_power": "sensor.ev_power"}))
    assert result["errors"]["base"] == "ev_pair_required"


def test_smart_cost_uses_binary_sensor_selector():
    from custom_components.opti_akku.config_flow import _sources_schema

    schema = _sources_schema({"sources": {}}, "ev")
    validators = {marker.schema: validator for marker, validator in schema.schema.items()}
    assert validators["ev1_smart_cost"].config["domain"] == ["binary_sensor"]
    assert validators["ev2_smart_cost"].config["domain"] == ["binary_sensor"]


@pytest.mark.parametrize("include_smart_cost", [False, True])
async def test_ev_pair_accepts_optional_smart_cost(hass, include_smart_cost):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("select.ev_mode", "pv")
    hass.states.async_set("binary_sensor.ev_charging", "on")
    hass.states.async_set("binary_sensor.ev_smart_cost", "on")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "features"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ev"}
    )
    values = {
        "ev1_mode": "select.ev_mode",
        "ev1_charging": "binary_sensor.ev_charging",
    }
    if include_smart_cost:
        values["ev1_smart_cost"] = "binary_sensor.ev_smart_cost"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, values)
    )
    assert result["type"] == FlowResultType.MENU
    assert not result.get("errors")


async def test_smart_cost_without_ev_pair_is_rejected(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("binary_sensor.ev_smart_cost", "on")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "features"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ev"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {
            "ev1_smart_cost": "binary_sensor.ev_smart_cost",
        })
    )
    assert result["errors"]["base"] == "ev_pair_required"


async def test_options_clear_source_and_cancel_preserves_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={**CONNECTION, "shadow_mode": True}, options={"sources": {"pv_power": "sensor.pv"}, "single_inverter": True, "settings": {"input_number.minsoc": 8}})
    entry.add_to_hass(hass)
    original = dict(entry.options)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "sources"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"single_inverter": True})
    assert entry.options == original
    hass.config_entries.options.async_abort(result["flow_id"])
    assert entry.options == original
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "sources"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"single_inverter": True})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert "single_writer_confirmed" not in {k.schema for k in result["data_schema"].schema}
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["sources"] == {}
    assert entry.data["shadow_mode"] is True
    assert entry.options["settings"]["input_number.minsoc"] == 8


async def test_connection_change_never_carries_writer_permission(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="sma_stp_se:1234567890", data={**CONNECTION, "serial_number": "1234567890"}, options={"single_writer_confirmed": True, "sources": {}})
    entry.add_to_hass(hass)
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value={**PROBE, "serial_number": "new"})):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await configure_options(hass, result["flow_id"], {"next_step_id": "connection"})
        connection = {k:v for k,v in CONNECTION.items() if k != "shadow_mode"}
        result = await hass.config_entries.options.async_configure(result["flow_id"], connection)
        assert entry.data["serial_number"] == "1234567890"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"single_writer_confirmed": True}))
    assert entry.data["serial_number"] == "new"
    assert entry.options["single_writer_confirmed"] is False


async def test_ordered_limits_remain_editable_as_one_transaction(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": {}})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "battery"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"minsoc": 96, "maxsoc": 97}))
    assert result["type"] == FlowResultType.MENU
    assert not entry.options.get("settings")

@pytest.mark.parametrize("unit,selected,expected", [(None, "EUR/kWh", "unsupported_unit"), ("EUR/MWh", "EUR/kWh", "unsupported_unit"),
    ("€/kWh", "EUR/kWh", None), ("Cent/kWh", "ct/kWh", None), ("cents/kWh", "ct/kWh", None), ("Ct/kWh", "ct/kWh", None), ("cent/kWh", "ct/kWh", None),
    ("€/kWh", "ct/kWh", "price_unit_mismatch")])
async def test_tariff_unit_whitelist(hass, unit, selected, expected):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": {}, "single_inverter": True})
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.price", "30", {"unit_of_measurement": unit, "today":
        dated_prices(dt_util.now().date(), timezone=dt_util.DEFAULT_TIME_ZONE, price=30)})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "tariff"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result,
        {"price_current": "sensor.price", "price_series": "sensor.price", "price_unit": selected}))
    if expected:
        assert result["errors"]["price_current"] == expected
    else:
        assert result["type"] == FlowResultType.MENU


async def test_reverted_draft_does_not_store_a_settings_patch(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": {}})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for maxsoc in (90, 95):
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "battery"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"maxsoc": maxsoc}))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert not entry.options["settings"]
    assert not entry.options.get("settings_revision")

@pytest.mark.parametrize("cancel_pending", [False, True])
async def test_offline_pending_patch_survives_another_options_save(hass, cancel_pending):
    from homeassistant.helpers.storage import Store

    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": {},
        "settings": {"input_number.minsoc": 7}, "settings_revision": "pending"})
    entry.add_to_hass(hass)
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", private=True).async_save({
        "settings": {"input_number.minsoc": 10, "input_number.maxsoc": 95}, "settings_revision": "applied"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "battery"})
    changes = {"minsoc": 10} if cancel_pending else {"maxsoc": 90}
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, changes))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    expected = {} if cancel_pending else {"input_number.minsoc": 7, "input_number.maxsoc": 90}
    assert entry.options["settings"] == expected


async def test_concurrent_options_save_rejects_stale_draft(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": {}})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "battery"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"maxsoc": 90}))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    hass.config_entries.async_update_entry(entry, options={"sources": {}, "price_unit": "ct/kWh"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert result["errors"]["base"] == "configuration_changed"
    assert entry.options["price_unit"] == "ct/kWh"
    assert "settings" not in entry.options


async def test_pending_patch_ignores_removed_setting_keys(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": {},
        "settings": {"input_number.removed_setting": 99, "input_number.minsoc": 7}, "settings_revision": "pending"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["settings"] == {"input_number.minsoc": 7}


async def test_unselected_features_leave_balancing_off_and_drop_suggestion(hass):
    hass.states.async_set("sensor.byd_zellspreizung_ruhe", "2", {"unit_of_measurement": "mV"})
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)), patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await finish_wizard(hass, await begin(hass), {"sources": {"single_inverter": True, "plant_meter_confirmed": True}})
    assert result["options"]["settings"]["input_number.opti_balancing_intervall_tage"] == 0
    assert result["options"]["settings"]["input_boolean.opti_ev_akku_pause"] is False
    assert "cell_spread" not in result["options"]["sources"]


async def test_options_menu_groups_optional_features(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "features" in result["menu_options"]
    assert "ev" not in result["menu_options"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "features"})
    assert result["menu_options"] == ["ev", "ev_preparation", "balancing", "demand", "arbitrage", "notifications", "init"]
    hass.config_entries.options.async_abort(result["flow_id"])
    assert not entry.options


async def test_final_save_rechecks_ev_after_concurrent_switch_change(hass):
    from types import SimpleNamespace
    from custom_components.opti_akku.config_flow import DEFINITIONS
    sources = {"ev1_mode": "select.ev_mode", "ev1_charging": "binary_sensor.ev_charging"}
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": sources})
    entry.add_to_hass(hass)
    settings = {key: definition["default"] for key, definition in DEFINITIONS.items()}
    entry.runtime_data = SimpleNamespace(settings=settings, _settings_revision=None)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "features"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "ev"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"opti_ev_akku_pause": False})
    settings["input_boolean.opti_ev_akku_pause"] = True
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "init"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert result["errors"]["base"] == "ev_pair_required"
    assert entry.options["sources"] == sources


@pytest.mark.parametrize("section", ["sources", "battery", "tariff", "forecast", "notifications", "advanced", "finish"])
async def test_options_sections_open_through_http(hass, hass_client, section):
    """Exercise the frontend boundary, including selectors without a unit."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, unique_id="synthetic-http",
                            options={"single_inverter": True})
    entry.add_to_hass(hass)
    client = await hass_client()
    response = await client.post("/api/config/config_entries/options/flow",
                                 json={"handler": entry.entry_id})
    assert response.status == 200
    flow = await response.json()
    parent = {"notifications": "features", "advanced": "maintenance"}.get(section)
    if parent:
        response = await client.post(f"/api/config/config_entries/options/flow/{flow['flow_id']}",
                                     json={"next_step_id": parent})
        assert response.status == 200, await response.text()
    response = await client.post(f"/api/config/config_entries/options/flow/{flow['flow_id']}",
                                 json={"next_step_id": section})
    assert response.status == 200, await response.text()
    form = await response.json()
    assert form["step_id"] == section
    assert form["type"] == "form"
    if section == "advanced":
        scarcity = next(field for field in form["data_schema"]
                        if field["name"] == "akkusteuerung_ueberschuss_veto_knappheit_faktor")
        assert "unit_of_measurement" not in scarcity["selector"]["number"]


async def test_tibber_options_confirm_home_and_currency_before_save(hass):
    """Selecting a provider never arms writes or commits an unfinished draft."""
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, unique_id="tibber-flow",
                            options={"sources": {"price_current": "sensor.old_price", "price_series": "sensor.old_series"}})
    entry.add_to_hass(hass)
    original = dict(entry.options)
    from types import SimpleNamespace
    with patch("custom_components.opti_akku.tibber_prices.async_fetch_prices", AsyncMock(return_value={"Test home": SimpleNamespace(entry_id="tibber-account")})):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "tariff"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"price_provider": "tibber"}))
        assert result["step_id"] == "tibber"
        assert dict(entry.options) == original
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
        assert result["errors"] == {"tibber_eur_confirmed": "tibber_eur_required"}
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"tibber_eur_confirmed": True}))
        assert result["type"] == FlowResultType.MENU
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
        assert "Tibber: Test home" in result["description_placeholders"]["price"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["price_provider"] == "tibber"
    assert entry.options["price_unit"] == "EUR/kWh"
    assert entry.options["tibber_home"] == "Test home"
    assert entry.options["tibber_entry_id"] == "tibber-account"
    assert entry.options["tibber_eur_confirmed"] is True
    assert not entry.options["single_writer_confirmed"]
    assert not entry.options["sources"]


async def test_tibber_flow_fetch_failure_preserves_entry(hass):
    from custom_components.opti_akku.tibber_prices import TibberPriceError

    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, unique_id="tibber-flow-failed", options={})
    entry.add_to_hass(hass)
    with patch("custom_components.opti_akku.tibber_prices.async_fetch_prices", AsyncMock(side_effect=TibberPriceError("tibber_timeout"))):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "tariff"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"price_provider": "tibber"}))
    assert result["step_id"] == "tibber"
    assert result["errors"] == {"base": "tibber_unavailable"}
    assert not entry.options


async def test_notification_selection_stays_draft_and_validates_service(hass):
    async def push(call):
        pass
    hass.services.async_register("notify", "test_phone", push)
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"single_inverter": True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass, result["flow_id"], {"next_step_id": "notifications"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"notification_service": "notify.test_phone"})
    assert result["type"] == FlowResultType.MENU
    assert "notification_service" not in entry.options
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["notification_service"] == "notify.test_phone"


@pytest.mark.parametrize("mode,additional,excluded,error_field", [
    ("balance", [], ["sensor.negative_load"], "excluded_load_sources"),
    ("external", ["sensor.extra_ac"], [], "additional_ac_sources"),
])
async def test_plant_wizard_rejects_invalid_lists(hass, mode, additional, excluded, error_field):
    hass.states.async_set("sensor.house", "1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.extra_ac", "500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.negative_load", "-100", {"unit_of_measurement": "W"})
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result, {
            "plant_mode": mode, "plant_meter_confirmed": True, "forecast_min_load_w": 0,
            "house_consumption": "sensor.house", "additional_ac_sources": additional,
            "excluded_load_sources": excluded}))
        assert result["step_id"] == "sources"
        assert error_field in result["errors"]


async def test_two_inverter_wizard_without_house_helper(hass):
    hass.states.async_set("sensor.extra_ac", "500", {"unit_of_measurement": "W"})
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)), patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await finish_wizard(hass, await begin(hass), {"sources": {
            "plant_mode": "balance", "plant_meter_confirmed": True, "forecast_min_load_w": 150,
            "additional_ac_sources": ["sensor.extra_ac"]}})
    assert result["options"]["plant_mode"] == "balance"
    assert not result["options"]["sources"].get("house_consumption")


async def test_event_based_idle_exclusion_can_be_configured_with_old_zero(hass):
    hass.states.async_set("sensor.wallbox", "0", {"unit_of_measurement": "W"})
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        values = form_values(result, {
            "plant_mode": "balance", "plant_meter_confirmed": True,
            "excluded_load_sources": ["sensor.wallbox"],
        })
        with patch("custom_components.opti_akku.config_flow.dt_util.utcnow",
                   return_value=dt_util.utcnow() + timedelta(days=7)):
            rejected = await hass.config_entries.flow.async_configure(result["flow_id"], values)
            assert rejected["errors"]["excluded_load_sources"] == "missing_or_stale"
            values["event_based_excluded_sources"] = ["sensor.wallbox"]
            accepted = await hass.config_entries.flow.async_configure(result["flow_id"], values)
        assert accepted["step_id"] == "battery"
        with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
            saved = await finish_wizard(hass, accepted)
        assert saved["options"]["event_based_excluded_sources"] == ["sensor.wallbox"]


@pytest.mark.parametrize("mode", ["legacy", "balance"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_idle_exclusion_wizard_rejects_source_not_excluded(hass, mode, enabled):
    hass.states.async_set("sensor.wallbox", "0", {"unit_of_measurement": "W"})
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result, {
            "plant_mode": mode, "plant_meter_confirmed": True, "strategy_enabled": enabled,
            "event_based_excluded_sources": ["sensor.wallbox"],
        }))
    assert result["errors"]["event_based_excluded_sources"] == "plant_sources_invalid"


async def test_initial_form_selects_backend_before_connection(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    assert {marker.schema for marker in result["data_schema"].schema} == {"backend"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"backend": "sma_modbus"}
    )
    assert result["step_id"] == "sma_connection"


async def test_fresh_sma_defaults_to_confirmed_plant_balance(hass):
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        defaults = form_values(result)
        assert defaults["plant_mode"] == "balance"
        assert defaults["plant_meter_confirmed"] is False
        result = await hass.config_entries.flow.async_configure(result["flow_id"], defaults)
    assert result["step_id"] == "sources"
    assert result["errors"]["plant_meter_confirmed"] == "plant_confirmation_required"


async def test_legacy_import_and_existing_entry_keep_legacy_plant_mode(hass):
    connection = {**CONNECTION, "migrate_legacy": True}
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass, connection)
        assert result["step_id"] == "migration_review"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"accept": True, "adjust_mapping": False}
        )
    assert result["step_id"] == "sources"
    assert form_values(result)["plant_mode"] == "legacy"
    hass.config_entries.flow.async_abort(result["flow_id"])

    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": {}})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sources"}
    )
    assert form_values(result)["plant_mode"] == "legacy"


async def test_huawei_wizard_binds_registry_device_and_forces_shadow(hass):
    from tests.test_huawei import SOURCES, make_backend

    _, entry, inverter, _ = make_backend(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"backend": "huawei_solar"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"huawei_device_id": inverter.id}
    )
    assert result["step_id"] == "huawei_sources"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SOURCES, "grid_positive": "export"}
    )
    assert result["step_id"] == "sources"
    with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await finish_wizard(
            hass, result, {"sources": {"strategy_enabled": False}}
        )
    assert result["data"] == {
        "backend": "huawei_solar",
        "huawei_entry_id": entry.entry_id,
        "huawei_device_id": inverter.id,
        "huawei_sources": SOURCES,
        "grid_positive": "export",
        "profile": "huawei_solar",
        "shadow_mode": True,
        "serial_number": "INV123",
    }
    assert result["result"].unique_id == f"shadow:huawei_solar:{inverter.id}"
    assert result["options"]["strategy_enabled"] is False


async def test_disabled_strategy_skips_strategy_source_requirements(hass):
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], form_values(result, {
                "strategy_enabled": False, "single_inverter": False
            })
        )
    assert result["step_id"] == "battery"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result))
    assert result["step_id"] == "notifications"


@pytest.mark.parametrize("initial_strategy", [True, False])
async def test_active_writer_blocks_strategy_change_at_final_save(hass, initial_strategy):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True, "strategy_enabled": initial_strategy},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(settings={}, _settings_revision=None, write_enabled=True)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sources"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"strategy_enabled": not initial_strategy})
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result)
    )
    assert result["errors"] == {"base": "stop_writes_before_strategy_change"}
    assert entry.options["strategy_enabled"] is initial_strategy


async def test_huawei_options_reconnect_keeps_shadow_immutable(hass):
    from tests.test_huawei import SOURCES, make_backend

    _, huawei_entry, inverter, _ = make_backend(hass)
    data = {
        "backend": "huawei_solar", "huawei_entry_id": huawei_entry.entry_id,
        "huawei_device_id": inverter.id, "huawei_sources": SOURCES,
        "grid_positive": "export", "profile": "huawei_solar",
        "shadow_mode": True, "serial_number": "INV123",
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data,
        unique_id=f"shadow:huawei_solar:{inverter.id}", options={"sources": {}})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "connection"}
    )
    assert "shadow_mode" not in {marker.schema for marker in result["data_schema"].schema}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"grid_power_w": SOURCES["pv_power_w"]})
    )
    assert result["errors"] == {"base": "huawei_device_unavailable"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result)
    )
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form_values(result)
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data["shadow_mode"] is True
    assert "host" not in entry.data


async def test_huawei_duplicate_role_entity_returns_form_error(hass):
    from tests.test_huawei import SOURCES, make_backend

    _, _, inverter, _ = make_backend(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"backend": "huawei_solar"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"huawei_device_id": inverter.id}
    )
    duplicate = {**SOURCES, "grid_power_w": SOURCES["pv_power_w"]}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**duplicate, "grid_positive": "export"}
    )
    assert result["step_id"] == "huawei_sources"
    assert result["errors"] == {"base": "huawei_device_unavailable"}


async def test_final_save_revalidates_sources_that_disappeared(hass):
    hass.states.async_set("sensor.house", 500, {"unit_of_measurement": "W"})
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION,
        options={"sources": {"house_consumption": "sensor.house"},
                 "single_inverter": False, "strategy_enabled": True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sources"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result)
    )
    hass.states.async_remove("sensor.house")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result)
    )
    assert result["errors"] == {"base": "sources_changed"}
    assert entry.options["sources"] == {"house_consumption": "sensor.house"}


async def test_options_change_during_final_validation_is_not_overwritten(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION,
        options={"sources": {}, "single_inverter": True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )

    async def concurrent_change(_settings):
        hass.config_entries.async_update_entry(entry,
            options={"sources": {}, "single_inverter": True, "concurrent": True})
        return True

    with patch(
        "custom_components.opti_akku.config_flow.WizardSections._validate_final_sources",
        AsyncMock(side_effect=concurrent_change),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form_values(result)
        )
    assert result["errors"] == {"base": "configuration_changed"}
    assert entry.options["concurrent"] is True


@pytest.mark.parametrize("initial_strategy", [True, False])
async def test_writer_enabled_during_final_validation_blocks_strategy_change(hass, initial_strategy):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION,
        options={"sources": {}, "single_inverter": True, "strategy_enabled": initial_strategy})
    entry.add_to_hass(hass)
    runtime = SimpleNamespace(settings={}, _settings_revision=None, write_enabled=False)
    entry.runtime_data = runtime
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sources"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"strategy_enabled": not initial_strategy})
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )

    async def enable_writer(_settings):
        runtime.write_enabled = True
        return True

    with patch(
        "custom_components.opti_akku.config_flow.WizardSections._validate_final_sources",
        AsyncMock(side_effect=enable_writer),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form_values(result)
        )
    assert result["errors"] == {"base": "stop_writes_before_strategy_change"}
    assert entry.options["strategy_enabled"] is initial_strategy


async def test_source_removed_during_tibber_fetch_fails_final_validation(hass):
    hass.states.async_set("sensor.house", 500, {"unit_of_measurement": "W"})
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={
        "sources": {"house_consumption": "sensor.house"}, "single_inverter": False,
        "strategy_enabled": True, "price_provider": "tibber", "tibber_home": "Home",
        "tibber_entry_id": "account", "tibber_eur_confirmed": True,
    })
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )

    async def fetch_then_remove(_hass):
        hass.states.async_remove("sensor.house")
        return {"Home": SimpleNamespace(entry_id="account")}

    with patch("custom_components.opti_akku.tibber_prices.async_fetch_prices",
               AsyncMock(side_effect=fetch_then_remove)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form_values(result)
        )
    assert result["errors"] == {"base": "sources_changed"}
    assert entry.options["sources"] == {"house_consumption": "sensor.house"}


async def test_fresh_balance_drops_legacy_suggestions_and_rejects_ac_override(hass):
    from custom_components.opti_akku.const import SOURCE_DEFINITIONS
    for role in ("house_consumption", "pv_power"):
        hass.states.async_set(SOURCE_DEFINITIONS[role][0], 100, {"unit_of_measurement": "W"})
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        values = form_values(result)
        assert not values.get("house_consumption")
        assert not values.get("pv_power")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], form_values(result, {
                "plant_meter_confirmed": True,
                "pv_power": SOURCE_DEFINITIONS["pv_power"][0],
            })
        )
    assert result["errors"]["pv_power"] == "plant_sources_invalid"


async def test_huawei_standard_wizard_maps_controls_without_writes(hass):
    from tests.test_huawei_control import CONTROLS, make_controller
    from tests.test_huawei import SOURCES
    controller, calls, _ = make_controller(hass)
    device = controller.device
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"backend": "huawei_solar"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"huawei_device_id": device.device_id, "shadow_mode": False})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**SOURCES, "grid_positive": "export"})
    assert result["step_id"] == "huawei_controls"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**CONTROLS, "huawei_grid_surplus": True})
    assert result["step_id"] == "sources"
    with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await finish_wizard(hass, result, {"sources": {"strategy_enabled": False}})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["huawei_controls"] == CONTROLS
    assert result["data"]["huawei_grid_surplus"] is True
    assert result["data"]["shadow_mode"] is False
    assert result["result"].unique_id == f"huawei_solar:{device.device_id}"
    assert calls == []


async def test_huawei_control_mapping_rejects_duplicate_actuator(hass):
    from tests.test_huawei_control import CONTROLS, make_controller
    from tests.test_huawei import SOURCES
    c, calls, _ = make_controller(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"backend": "huawei_solar"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"huawei_device_id": c.device.device_id, "shadow_mode": False})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**SOURCES, "grid_positive": "export"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**CONTROLS, "mode": CONTROLS["excess_pv"]})
    assert result["errors"] == {"base": "huawei_write_access_missing"}
    assert calls == []


async def test_demand_options_are_separate_and_default_off(hass):
    entry = MockConfigEntry(domain='opti_akku', title='Test',
                            data={'host': '127.0.0.1', 'port': 502, 'unit_id': 3},
                            options={'single_inverter': True, 'sources': {}})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert 'features' in result['menu_options']
    assert 'demand' not in result['menu_options']
    result = await configure_options(hass, result['flow_id'], {'next_step_id': 'demand'})
    schema = result['data_schema'].schema
    enabled = next(k for k in schema if k.schema == 'enabled')
    assert enabled.default() is False
    active_profile = next(k for k in schema if k.schema == 'use_for_peak_reserve')
    assert active_profile.default() is False
    before = dict(entry.options)
    result = await hass.config_entries.options.async_configure(result['flow_id'],
                                                              {'enabled': True, 'dhw_cycle_kwh': 0})
    # Merely visiting/editing the draft cannot change strategy or stored options.
    assert result['type'] == 'menu'
    assert dict(entry.options) == before
    result = await configure_options(hass, result['flow_id'], {'next_step_id': 'demand'})
    result = await hass.config_entries.options.async_configure(result['flow_id'],
        {'enabled': True, 'dhw_cycle_kwh': 0, 'water_temperature': 'sensor.missing'})
    assert result['errors']['water_temperature'] == 'missing_or_stale'
    assert result['errors']['base'] == 'demand_water_pair'
    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)
    heat = registry.async_get_or_create('sensor', 'powercalc', 'synthetic_heat')
    hass.states.async_set(heat.entity_id, '20', {'unit_of_measurement': 'W'})
    result = await hass.config_entries.options.async_configure(result['flow_id'],
        {'enabled': True, 'dhw_cycle_kwh': 0, 'heat_power': heat.entity_id})
    assert result['errors']['heat_power'] == 'demand_heat_meter'
    result = await hass.config_entries.options.async_configure(result['flow_id'],
        {'enabled': True, 'dhw_cycle_kwh': 0, 'use_for_peak_reserve': True})
    assert dict(entry.options) == before
    result = await hass.config_entries.options.async_configure(result['flow_id'], {'next_step_id': 'finish'})
    result = await hass.config_entries.options.async_configure(result['flow_id'], {})
    assert result['type'] == 'create_entry'
    assert entry.options['demand_forecast']['enabled'] is True
    assert entry.options['demand_forecast']['use_for_peak_reserve'] is True
    assert entry.options['single_inverter'] is True
    assert entry.options['sources'] == {}


async def test_arbitrage_estimate_requires_explicit_assumptions_and_stays_optional(hass):
    entry = MockConfigEntry(
        domain="opti_akku",
        title="Test",
        data={"host": "127.0.0.1", "port": 502, "unit_id": 3},
        options={"single_inverter": True, "sources": {}},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "arbitrage" not in result["menu_options"]
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "arbitrage"}
    )
    enabled = next(k for k in result["data_schema"].schema if k.schema == "enabled")
    assert enabled.default() is False
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": True}
    )
    assert set(result["errors"]) == {
        "battery_price_eur",
        "degradation_percent",
        "cycles",
        "usable_capacity_kwh",
        "charge_efficiency_percent",
        "discharge_efficiency_percent",
        "margin_ct",
    }
    shown = result["data_schema"].schema
    enabled = next(k for k in shown if k.schema == "enabled")
    assert enabled.default() is True
    configured = {
        "enabled": True,
        "hold_enabled": False,
        "battery_price_eur": 5000,
        "degradation_percent": 20,
        "cycles": 5000,
        "usable_capacity_kwh": 10,
        "charge_efficiency_percent": 90,
        "discharge_efficiency_percent": 90,
        "margin_ct": 2,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], configured)
    assert result["type"] == "menu"
    assert "arbitrage_estimate" not in entry.options
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert entry.options["arbitrage_estimate"] == configured

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "arbitrage"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": False}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert "arbitrage_estimate" not in entry.options


async def test_active_arbitrage_hold_requires_demand_forecast_and_is_saved(hass):
    assumptions = {
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
    entry = MockConfigEntry(
        domain="opti_akku",
        title="Test",
        data={"host": "127.0.0.1", "port": 502, "unit_id": 3},
        options={"single_inverter": True, "sources": {}},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "arbitrage"}
    )
    blocked = await hass.config_entries.options.async_configure(
        result["flow_id"], assumptions
    )
    assert blocked["errors"]["base"] == "arbitrage_hold_requires_demand"

    entry = MockConfigEntry(
        domain="opti_akku",
        title="Test with demand",
        data={"host": "127.0.0.2", "port": 502, "unit_id": 3},
        options={
            "single_inverter": True,
            "sources": {},
            "demand_forecast": {"enabled": True, "sources": {}},
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "arbitrage"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], assumptions
    )
    assert result["type"] == "menu"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert entry.options["arbitrage_estimate"] == assumptions


async def test_observation_options_never_replace_controller_sources(hass):
    entry = MockConfigEntry(
        domain="opti_akku",
        title="Test",
        data={"host": "127.0.0.1", "port": 502, "unit_id": 3},
        options={"single_inverter": True, "sources": {}},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.gross", "800", {"unit_of_measurement": "W"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "observation"}
    )
    enabled = next(k for k in result["data_schema"].schema if k.schema == "enabled")
    assert enabled.default() is False
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "enabled": True,
            "meter_confirmed": True,
            "legacy_floor_w": 150,
            "gross_house": "sensor.gross",
            "watch_sources": ["sensor.missing"],
        },
    )
    assert result["errors"]["base"] == "observation_sources"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "enabled": True,
            "meter_confirmed": True,
            "legacy_floor_w": 150,
            "gross_house": "sensor.gross",
        },
    )
    assert not entry.options.get("source_observation")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert entry.options["source_observation"]["enabled"] is True
    assert entry.options["sources"] == {}
    assert entry.options["single_inverter"] is True


async def test_ev_preparation_is_opt_in_and_needs_soc_and_charging(hass):
    entry=MockConfigEntry(domain='opti_akku',title='Test',data={'host':'127.0.0.1','port':502,'unit_id':3},options={'sources':{}})
    entry.add_to_hass(hass)
    result=await hass.config_entries.options.async_init(entry.entry_id)
    result=await configure_options(hass, result['flow_id'],{'next_step_id':'ev_preparation'})
    enabled=next(k for k in result['data_schema'].schema if k.schema=='enabled')
    assert enabled.default() is False
    result=await hass.config_entries.options.async_configure(result['flow_id'],{'enabled':True,'vehicle_threshold':40,'house_target':80})
    assert result['errors']=={'vehicle_soc':'missing_or_stale','charging':'missing_or_stale'}
    hass.states.async_set('sensor.car','20',{'unit_of_measurement':'%'})
    hass.states.async_set('binary_sensor.charging','off')
    result=await hass.config_entries.options.async_configure(result['flow_id'],{'enabled':True,'vehicle_soc':'sensor.car','charging':'binary_sensor.charging','vehicle_threshold':40,'house_target':80})
    assert not entry.options.get('ev_preparation')
    result=await hass.config_entries.options.async_configure(result['flow_id'],{'next_step_id':'finish'})
    result=await hass.config_entries.options.async_configure(result['flow_id'],{})
    assert result['type']=='create_entry'
    assert entry.options['ev_preparation']['enabled'] is True
    assert entry.options['sources']=={}


@pytest.mark.parametrize(
    ("value", "attributes"),
    [
        ("not-a-time", {"has_time": True}),
        ("2026-09-19", {"has_time": False}),
    ],
)
async def test_ev_departure_must_be_a_usable_time_source(hass, value, attributes):
    entry = MockConfigEntry(
        domain="opti_akku",
        title="Test",
        data={"host": "127.0.0.1", "port": 502, "unit_id": 3},
        options={"sources": {}},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.car", "20", {"unit_of_measurement": "%"})
    hass.states.async_set("binary_sensor.charging", "off")
    hass.states.async_set("input_datetime.departure", value, attributes)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "ev_preparation"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "enabled": True,
            "vehicle_soc": "sensor.car",
            "charging": "binary_sensor.charging",
            "departure": "input_datetime.departure",
            "vehicle_threshold": 40,
            "house_target": 80,
            "vehicle_target_soc": 80,
            "vehicle_capacity_kwh": 60,
            "charge_power_kw": 11,
            "vehicle_charge_efficiency_percent": 90,
        },
    )
    assert result["errors"]["departure"] == "ev_deadline_value"


async def test_ev_departure_requires_explicit_capacity_power_and_efficiency(hass):
    entry = MockConfigEntry(
        domain="opti_akku",
        title="Test",
        data={"host": "127.0.0.1", "port": 502, "unit_id": 3},
        options={"sources": {}},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.car", "20", {"unit_of_measurement": "%"})
    hass.states.async_set("binary_sensor.charging", "off")
    hass.states.async_set(
        "input_datetime.departure",
        (dt_util.utcnow() + timedelta(hours=4)).isoformat(),
        {"has_time": True},
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "ev_preparation"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "enabled": True,
            "vehicle_soc": "sensor.car",
            "charging": "binary_sensor.charging",
            "departure": "input_datetime.departure",
            "vehicle_threshold": 40,
            "house_target": 80,
            "vehicle_target_soc": 80,
        },
    )
    assert result["errors"] == {
        "vehicle_capacity_kwh": "ev_deadline_value",
        "charge_power_kw": "ev_deadline_value",
        "vehicle_charge_efficiency_percent": "ev_deadline_value",
    }


@pytest.mark.parametrize(
    ("missing_source", "expected_error"),
    [("departure", "ev_deadline_value"), ("vehicle_soc", "missing_or_stale")],
)
@pytest.mark.parametrize("enabled", [False, True])
async def test_ev_preparation_can_be_disabled_with_unavailable_sources(
    hass, missing_source, expected_error, enabled
):
    ev_options = {
        "enabled": True,
        "vehicle_soc": "sensor.car",
        "charging": "binary_sensor.charging",
        "departure": "input_datetime.departure",
        "vehicle_threshold": 40,
        "house_target": 80,
        "vehicle_target_soc": 80,
        "vehicle_capacity_kwh": 60,
        "charge_power_kw": 11,
        "vehicle_charge_efficiency_percent": 90,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={
            "sources": {},
            "single_inverter": True,
            "ev_preparation": ev_options,
        },
    )
    entry.add_to_hass(hass)
    if missing_source != "vehicle_soc":
        hass.states.async_set("sensor.car", 20, {"unit_of_measurement": "%"})
    hass.states.async_set("binary_sensor.charging", "off")
    if missing_source == "departure":
        hass.states.async_set("input_datetime.departure", "unavailable", {"has_time": True})
    else:
        hass.states.async_set(
            "input_datetime.departure",
            (dt_util.utcnow() + timedelta(hours=4)).isoformat(),
            {"has_time": True},
        )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "ev_preparation"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"enabled": enabled})
    )

    if enabled:
        assert result["errors"][missing_source] == expected_error
        return
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["ev_preparation"] == {**ev_options, "enabled": False}


async def huawei_source_form(hass, *, shadow=True, temperature=True):
    from tests.test_huawei import SOURCES, make_backend
    _, _, device, _ = make_backend(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"backend": "huawei_solar"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"huawei_device_id": device.id, "shadow_mode": shadow})
    sources = dict(SOURCES)
    if not temperature:
        sources.pop("battery_temp")
    return await hass.config_entries.flow.async_configure(result["flow_id"], {**sources, "grid_positive": "export"})


async def test_huawei_standard_requires_temperature_before_control_mapping(hass):
    from homeassistant.data_entry_flow import InvalidData
    with pytest.raises(InvalidData):
        await huawei_source_form(hass, shadow=False, temperature=False)


async def test_huawei_shadow_without_temperature_requires_telemetry_only(hass):
    result = await huawei_source_form(hass, temperature=False)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result))
    assert result["errors"] == {"base": "huawei_temperature_required"}
    with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result, {"strategy_enabled": False}))
        result = await finish_wizard(hass, result)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"]["strategy_enabled"] is False


@pytest.mark.parametrize("max_age", [900, 0])
async def test_huawei_strategy_requires_external_house_and_completes_shadow_wizard(hass, max_age):
    result = await huawei_source_form(hass)
    assert "single_inverter" not in {m.schema for m in result["data_schema"].schema}
    from homeassistant.data_entry_flow import InvalidData
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result, {"single_inverter": True}))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result))
    assert result["errors"].get("house_consumption") == "house_required"
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result, {"plant_mode": "balance", "plant_meter_confirmed": True}))
    hass.states.async_set("sensor.actual_house", "600", {"unit_of_measurement": "W"})
    with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result, {"plant_mode": "external", "house_consumption": "sensor.actual_house", "source_max_age": max_age}))
        result = await finish_wizard(hass, result)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"]["strategy_enabled"] is True
    assert result["options"]["single_inverter"] is False
    assert result["options"]["sources"]["house_consumption"] == "sensor.actual_house"
    assert result["options"]["source_max_age"] == max_age


async def test_huawei_temperature_loss_before_finish_rejected(hass):
    from tests.test_huawei import SOURCES
    result = await huawei_source_form(hass)
    hass.states.async_set("sensor.actual_house", "600", {"unit_of_measurement": "W"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result, {"house_consumption": "sensor.actual_house"}))
    for _ in range(10):
        if result["step_id"] == "finish":
            break
        assert not result.get("errors")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result))
    assert result["step_id"] == "finish"
    hass.states.async_set(SOURCES["battery_temp"], "unavailable")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result))
    assert result["errors"] == {"base": "huawei_temperature_required"}


async def test_huawei_standard_strategy_wizard_completes_without_writes(hass):
    from tests.test_huawei_control import CONTROLS, make_controller
    from tests.test_huawei import SOURCES
    controller, calls, _ = make_controller(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"backend": "huawei_solar"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"huawei_device_id": controller.device.device_id, "shadow_mode": False})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**SOURCES, "grid_positive": "export"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CONTROLS)
    hass.states.async_set("sensor.actual_house", "600", {"unit_of_measurement": "W"})
    with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await finish_wizard(hass, result, {"sources": {"house_consumption": "sensor.actual_house", "plant_mode": "external"}})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"]["strategy_enabled"] is True
    assert result["data"]["shadow_mode"] is False
    assert calls == []


async def test_huawei_existing_single_inverter_needs_explicit_source_correction(hass):
    from tests.test_huawei import SOURCES, make_backend
    _, provider, inverter, _ = make_backend(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"backend": "huawei_solar", "huawei_entry_id": provider.entry_id,
        "huawei_device_id": inverter.id, "huawei_sources": SOURCES, "grid_positive": "export",
        "profile": "huawei_solar", "shadow_mode": True, "serial_number": "INV123"},
        options={"sources": {}, "single_inverter": True, "strategy_enabled": True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert result["errors"] == {"base": "huawei_house_source_required"}
    assert entry.options["single_inverter"] is True
    # Cancel invalid draft; reopen the relevant section and correct explicitly.
    hass.config_entries.options.async_abort(result["flow_id"])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "sources"})
    hass.states.async_set("sensor.actual_house", "600", {"unit_of_measurement": "W"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result, {"house_consumption": "sensor.actual_house"}))
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["single_inverter"] is False


async def test_huawei_shadow_temperature_can_be_added_without_restarting_wizard(hass):
    from tests.test_huawei import SOURCES
    result = await huawei_source_form(hass, temperature=False)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result))
    assert result["errors"] == {"base": "huawei_temperature_required"}
    assert "huawei_battery_temp" in {m.schema for m in result["data_schema"].schema}
    hass.states.async_set("sensor.actual_house", "600", {"unit_of_measurement": "W"})
    with patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result, {"huawei_battery_temp": SOURCES["battery_temp"], "house_consumption": "sensor.actual_house"}))
        result = await finish_wizard(hass, result)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["huawei_sources"]["battery_temp"] == SOURCES["battery_temp"]
    assert result["options"]["strategy_enabled"] is True


async def test_unchanged_sma_connection_preserves_single_writer_confirmation(hass):
    data = {**CONNECTION, "backend": "sma_modbus", "serial_number": PROBE["serial_number"]}
    entry = MockConfigEntry(domain=DOMAIN, unique_id="sma_stp_se:1234567890", data=data,
        options={"single_writer_confirmed": True, "single_inverter": True, "sources": {}})
    entry.add_to_hass(hass)
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await configure_options(hass, result["flow_id"], {"next_step_id": "connection"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "finish"})
        result = await hass.config_entries.options.async_configure(result["flow_id"], form_values(result))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["single_writer_confirmed"] is True
    assert entry.data == data


async def test_failed_sma_reconnect_keeps_entry_and_retry_values(hass):
    data = {**CONNECTION, "backend": "sma_modbus", "serial_number": PROBE["serial_number"]}
    options = {"single_writer_confirmed": True, "single_inverter": True, "sources": {}}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="sma_stp_se:1234567890",
        data=data,
        options=options,
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "connection"}
    )
    submitted = {
        "host": " replacement.local ",
        "port": 2502,
        "unit_id": 4,
        "profile": "sma_stp_se",
    }
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value={})):
        result = await hass.config_entries.options.async_configure(result["flow_id"], submitted)

    assert result["step_id"] == "connection"
    assert result["errors"] == {"base": "unsupported_device"}
    assert form_values(result) == {**submitted, "host": "replacement.local"}
    assert dict(entry.data) == data
    assert dict(entry.options) == options

    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form_values(result)
        )
    assert result["type"] == FlowResultType.MENU
    assert dict(entry.data) == data
    assert dict(entry.options) == options


async def test_runtime_limit_change_during_options_flow_is_not_overwritten(hass):
    from custom_components.opti_akku.config_flow import DEFINITIONS

    options = {"single_inverter": True, "sources": {}}
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options=options)
    entry.add_to_hass(hass)
    runtime_settings = {key: definition["default"] for key, definition in DEFINITIONS.items()}
    entry.runtime_data = SimpleNamespace(settings=runtime_settings, write_enabled=False)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "battery"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"maxsoc": 90})
    )
    runtime_settings["input_number.minsoc"] = 96
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result)
    )

    assert result["step_id"] == "finish"
    assert result["errors"] == {"base": "invalid_limits"}
    assert dict(entry.options) == options


async def test_sma_without_serial_uses_stable_connection_identity(hass):
    probe = {key: value for key, value in PROBE.items() if key != "serial_number"}
    with patch(
        "custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=probe)
    ), patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await finish_wizard(
            hass,
            await begin(hass),
            {"sources": {"single_inverter": True, "plant_meter_confirmed": True}},
        )
    assert result["result"].unique_id == "192.0.2.10:502:3"


async def test_missing_huawei_device_stays_on_connection_step(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"backend": "huawei_solar"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"huawei_device_id": "missing-device"}
    )
    assert result["step_id"] == "huawei_device"
    assert result["errors"] == {"base": "huawei_device_unavailable"}


async def test_saved_tibber_provider_hides_manual_price_sources(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={
            "sources": {},
            "single_inverter": True,
            "price_provider": "tibber",
            "price_max_age": 7200,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tariff"}
    )
    assert not {
        "price_current",
        "price_series",
        "price_unit",
    } & {marker.schema for marker in result["data_schema"].schema}
    hass.config_entries.options.async_abort(result["flow_id"])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "advanced"}
    )
    assert form_values(result)["price_max_age"] == 7200


async def test_related_source_sets_and_limits_report_section_errors(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.price", "0.2", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.forecast", "10", {"unit_of_measurement": "kWh"})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tariff"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"price_current": "sensor.price"})
    )
    assert result["errors"]["base"] == "price_pair_required"
    hass.config_entries.options.async_abort(result["flow_id"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "forecast"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"forecast_today": "sensor.forecast"})
    )
    assert result["errors"]["base"] == "forecast_set_required"
    hass.config_entries.options.async_abort(result["flow_id"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "battery"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"minsoc": 96, "maxsoc": 95})
    )
    assert result["errors"]["base"] == "invalid_limits"


@pytest.mark.parametrize(
    ("value", "unit", "error"),
    [("unknown", "W", "invalid_value"), (-1, "W", "negative_value"), (1, "A", "unsupported_unit")],
)
async def test_ev_power_source_reports_precise_validation_error(hass, value, unit, error):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.ev_power", value, {"unit_of_measurement": unit})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "features"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ev"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"ev1_power": "sensor.ev_power"})
    )
    assert result["errors"]["ev1_power"] == error


async def test_failed_and_duplicate_sma_reconnect_preserve_entry(hass):
    data = {**CONNECTION, "backend": "sma_modbus", "serial_number": PROBE["serial_number"]}
    options = {"single_writer_confirmed": True, "single_inverter": True, "sources": {}}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="sma_stp_se:1234567890",
        data=data,
        options=options,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "connection"}
    )
    with patch(
        "custom_components.opti_akku.config_flow._probe", AsyncMock(side_effect=OSError)
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form_values(result)
        )
    assert result["errors"] == {"base": "cannot_connect"}
    assert dict(entry.data) == data and dict(entry.options) == options
    hass.config_entries.options.async_abort(result["flow_id"])

    MockConfigEntry(
        domain=DOMAIN,
        unique_id="sma_stp_se:other-device",
        data={**CONNECTION, "host": "other"},
    ).add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "connection"}
    )
    with patch(
        "custom_components.opti_akku.config_flow._probe",
        AsyncMock(return_value={**PROBE, "serial_number": "other-device"}),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form_values(result, {"host": "other"})
        )
    assert result["errors"] == {"base": "already_configured"}
    assert dict(entry.data) == data and dict(entry.options) == options


@pytest.mark.parametrize("failure", ["fetch", "binding"])
async def test_final_save_rechecks_tibber_account_binding(hass, failure):
    from custom_components.opti_akku.tibber_prices import TibberPriceError

    options = {
        "sources": {},
        "single_inverter": True,
        "price_provider": "tibber",
        "tibber_home": "Home",
        "tibber_entry_id": "account",
        "tibber_eur_confirmed": True,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options=options)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    fetch = (
        AsyncMock(side_effect=TibberPriceError("tibber_fetch_failed"))
        if failure == "fetch"
        else AsyncMock(return_value={"Home": SimpleNamespace(entry_id="replacement")})
    )
    with patch("custom_components.opti_akku.tibber_prices.async_fetch_prices", fetch):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], form_values(result)
        )
    assert result["errors"] == {"base": "sources_changed"}
    assert dict(entry.options) == options


async def test_unavailable_notification_target_is_visible_but_cannot_be_saved(hass):
    options = {
        "sources": {},
        "single_inverter": True,
        "notification_service": "notify.removed_phone",
    }
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options=options)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "notifications"}
    )
    assert form_values(result)["notification_service"] == "notify.removed_phone"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result)
    )
    assert result["errors"] == {"base": "notification_service_unavailable"}
    assert dict(entry.options) == options


async def test_observation_rejects_duplicate_and_missing_sources(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "observation"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        form_values(result, {"watch_sources": ["sensor.same", "sensor.same"]}),
    )
    assert result["errors"] == {"base": "observation_sources"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"watch_sources": ["sensor.missing"]})
    )
    assert result["errors"] == {"base": "observation_sources"}


@pytest.mark.parametrize(
    ("kind", "error"),
    [
        ("missing", "missing_or_stale"),
        ("unit", "ev_soc_unit"),
        ("self", "self_reference"),
    ],
)
async def test_ev_preparation_source_errors_are_field_specific(hass, kind, error):
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True},
    )
    entry.add_to_hass(hass)
    entity_id = "sensor.vehicle"
    if kind == "unit":
        hass.states.async_set(entity_id, 20, {"unit_of_measurement": "kWh"})
    elif kind == "self":
        entity = er.async_get(hass).async_get_or_create("sensor", DOMAIN, "vehicle")
        entity_id = entity.entity_id
        hass.states.async_set(entity_id, 20, {"unit_of_measurement": "%"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "ev_preparation"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        form_values(
            result,
            {
                "enabled": True,
                "vehicle_soc": entity_id,
                "charging": "binary_sensor.missing",
            },
        ),
    )
    assert result["errors"]["vehicle_soc"] == error


async def test_source_sections_reject_self_reference_and_overlapping_plant_roles(hass):
    from homeassistant.helpers import entity_registry as er

    own = er.async_get(hass).async_get_or_create("sensor", DOMAIN, "own-source")
    hass.states.async_set(own.entity_id, 500, {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.shared", 500, {"unit_of_measurement": "W"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sources"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        form_values(
            result,
            {
                "plant_mode": "balance",
                "plant_meter_confirmed": True,
                "additional_ac_sources": ["sensor.shared"],
                "excluded_load_sources": ["sensor.shared"],
            },
        ),
    )
    assert result["errors"]["base"] == "plant_duplicate_source"
    hass.config_entries.options.async_abort(result["flow_id"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "forecast"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        form_values(
            result,
            {
                "forecast_today": own.entity_id,
                "forecast_tomorrow": own.entity_id,
                "forecast_remaining": own.entity_id,
            },
        ),
    )
    assert result["errors"]["forecast_today"] == "self_reference"


async def test_ev_pause_setting_requires_a_complete_charger_pair(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "features"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ev"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"opti_ev_akku_pause": True})
    )
    assert result["errors"] == {"base": "ev_pair_required"}


async def test_huawei_maxsoc_must_fit_configured_cutoff_control(hass):
    data = {
        "backend": "huawei_solar",
        "huawei_entry_id": "provider",
        "huawei_device_id": "device",
        "huawei_sources": {},
        "huawei_controls": {"cutoff_soc": "number.cutoff"},
        "grid_positive": "export",
        "profile": "huawei_solar",
        "shadow_mode": False,
    }
    hass.states.async_set("number.cutoff", 50, {"min": 20, "max": 80})
    entry = MockConfigEntry(
        domain=DOMAIN, data=data, options={"sources": {}, "single_inverter": False}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "battery"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"maxsoc": 90})
    )
    assert result["errors"] == {"maxsoc": "huawei_cutoff_soc_unsupported"}


@pytest.mark.parametrize("self_reference", [False, True])
async def test_demand_heat_source_cannot_be_house_total_or_opti_entity(
    hass, self_reference
):
    from homeassistant.helpers import entity_registry as er

    if self_reference:
        heat = er.async_get(hass).async_get_or_create("sensor", DOMAIN, "heat")
        heat_entity = heat.entity_id
        house = "sensor.house"
    else:
        heat_entity = house = "sensor.house"
    hass.states.async_set(heat_entity, 500, {"unit_of_measurement": "W"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={
            "sources": {"house_consumption": house},
            "single_inverter": False,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await configure_options(hass,
        result["flow_id"], {"next_step_id": "demand"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], form_values(result, {"enabled": True, "heat_power": heat_entity})
    )
    assert result["errors"]["heat_power"] == (
        "self_reference" if self_reference else "demand_heat_meter"
    )


async def test_guided_features_can_open_ev_and_options_can_open_balancing(hass):
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        for expected in ("sources", "battery", "tariff", "forecast"):
            assert result["step_id"] == expected
            values = form_values(result)
            if expected == "sources":
                values.update(single_inverter=True, plant_meter_confirmed=True)
            result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
        assert result["step_id"] == "features"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"configure_ev": True, "configure_balancing": False}
        )
    assert result["step_id"] == "ev"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONNECTION,
        options={"sources": {}, "single_inverter": True},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "features"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "balancing"}
    )
    assert result["step_id"] == "balancing"


async def test_temporary_charge_override_only_offered_after_initial_setup(hass):
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values(result, {
            "plant_mode": "balance", "plant_meter_confirmed": True,
        }))
        assert result["step_id"] == "battery"
        assert "opti_manuelle_ladegrenze" not in {m.schema for m in result["data_schema"].schema}
        hass.config_entries.flow.async_abort(result["flow_id"])
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION, options={"sources": {}, "single_inverter": True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "battery"})
    assert "opti_manuelle_ladegrenze" in {m.schema for m in result["data_schema"].schema}
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_reporting_policy_is_reachable_before_validating_stale_sources(hass):
    hass.states.async_set('sensor.old_house','700',{'unit_of_measurement':'W'})
    with patch('custom_components.opti_akku.config_flow._probe',AsyncMock(return_value=PROBE)):
        result=await begin(hass)
        values=form_values(result,{'house_consumption':'sensor.old_house','source_max_age':0,'plant_mode':'external','forecast_min_load_w':0})
        with patch('custom_components.opti_akku.config_flow.dt_util.utcnow',return_value=dt_util.utcnow()+timedelta(hours=3)):
            result=await hass.config_entries.flow.async_configure(result['flow_id'], values)
        assert result['step_id']=='battery' and not result['errors'], result
        with patch('custom_components.opti_akku.async_setup_entry',AsyncMock(return_value=True)):
            saved=await finish_wizard(hass,result)
        assert saved['options']['source_max_age']==0


def test_huawei_temperature_keeps_age_gate_with_power_availability_policy(hass):
    from custom_components.opti_akku.config_flow import WizardSections
    wizard=WizardSections()
    wizard.hass=hass
    hass.states.async_set('sensor.temp','25',{'unit_of_measurement':'°C'})
    conn={'huawei_sources':{'battery_temp':'sensor.temp'}}
    assert wizard._huawei_temperature_valid(conn, {'source_max_age':0})
    with patch('custom_components.opti_akku.config_flow.dt_util.utcnow',return_value=dt_util.utcnow()+timedelta(seconds=901)):
        assert not wizard._huawei_temperature_valid(conn, {'source_max_age':0})


@pytest.mark.parametrize("value", [0.001, 0.4, 0.999])
def test_fractional_source_age_cannot_enable_availability_only(value):
    import voluptuous as vol
    from custom_components.opti_akku.config_flow import _sources_schema

    schema = _sources_schema({}, "sources")
    validator = next(v for k, v in schema.schema.items() if k.schema == "source_max_age")
    with pytest.raises(vol.Invalid):
        validator(value)
    assert validator(0) == 0
    assert validator(900) == 900


async def test_options_overview_and_maintenance_preserve_a_single_draft(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=CONNECTION)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["sources", "battery", "tariff", "forecast", "features", "maintenance", "finish"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "maintenance"})
    assert result["menu_options"] == ["connection", "observation", "advanced", "init"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "init"})
    assert result["step_id"] == "init"
    assert not entry.options


@pytest.mark.parametrize("value,expected", [(60.0, 60), (60.5, 60), (7200.0, 7200)])
def test_tibber_age_keeps_integer_conversion(value, expected):
    import voluptuous as vol
    from custom_components.opti_akku.config_flow import _sources_schema

    schema = _sources_schema({"price_provider": "tibber"}, "advanced")
    validator = next(v for k, v in schema.schema.items() if k.schema == "price_max_age")
    result = validator(value)
    assert type(result) is int and result == expected
    with pytest.raises(vol.Invalid):
        validator(59)
