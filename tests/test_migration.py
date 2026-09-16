"""Migration reads a validated, frozen proposal and never arms the controller."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.opti_akku.const import DOMAIN
from custom_components.opti_akku.migration import snapshot
from .test_config_flow import CONNECTION, PROBE, finish_wizard

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def seed(hass, low="7", high="91", unit="%"):
    hass.states.async_set("input_number.minsoc", low, {"unit_of_measurement": unit})
    hass.states.async_set("input_number.maxsoc", high, {"unit_of_measurement": "%"})
    hass.states.async_set("input_boolean.akku_opti_automatik", "on")
    hass.states.async_set("input_boolean.opti_ev_akku_pause", "on")


async def begin_migration(hass):
    return await hass.config_entries.flow.async_init(DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={**CONNECTION, "migrate_legacy": True, "shadow_mode": False})


async def test_migration_frozen_shadow_and_confirmation(hass):
    seed(hass)
    hass.states.async_set("input_select.akkusteuerung_modus", "Akku Netzladen")
    before = {s.entity_id: s.state for s in hass.states.async_all()}
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)) as probe, patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await begin_migration(hass)
        assert result["step_id"] == "migration_review"
        assert probe.call_args.args[1]["shadow_mode"] is True
        assert "91.0" in result["description_placeholders"]["report"]
        assert not hass.config_entries.async_entries(DOMAIN)
        rejected = await hass.config_entries.flow.async_configure(result["flow_id"], {"accept": False})
        assert rejected["errors"]["base"] == "migration_confirmation_required"
        assert before == {s.entity_id: s.state for s in hass.states.async_all()}
        hass.states.async_set("input_number.maxsoc", "80", {"unit_of_measurement": "%"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"accept": True})
        result = await finish_wizard(hass, result, {"sources": {"single_inverter": True}})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["shadow_mode"] is True
    assert result["result"].unique_id == "shadow:sma_stp_se:1234567890"
    options = result["options"]
    assert options["settings"]["input_number.maxsoc"] == 91
    assert options["settings"]["input_number.minsoc"] == 7
    assert options["settings"]["input_boolean.akku_opti_automatik"] is False
    assert options["settings"]["input_boolean.opti_ev_akku_pause"] is False
    assert options["single_writer_confirmed"] is False
    assert options["shadow_reference_mode"] == "input_select.akkusteuerung_modus"
    assert options["migration"]["settings"]["input_number.maxsoc"] == 91
    assert hass.states.get("input_number.maxsoc").state == "80"


@pytest.mark.parametrize("low,high,unit", [("95", "90", "%"), ("7", "unknown", "%"), ("7", "91", "kWh"), ("-1", "91", "%"), ("nan", "91", "%")])
async def test_invalid_pair_never_partially_imported(hass, low, high, unit):
    seed(hass, low, high, unit)
    values, report = snapshot(hass.states, {"input_number.minsoc": "input_number.minsoc", "input_number.maxsoc": "input_number.maxsoc"})
    assert "input_number.minsoc" not in values
    assert "input_number.maxsoc" not in values
    assert report["input_number.maxsoc"]["status"] != "accepted"
    if unit != "%":
        assert report["input_number.minsoc"]["status"] == "unit"


async def test_boolean_and_allowlist(hass):
    seed(hass)
    hass.states.async_set("input_boolean.opti_prognose_netzladen", "unavailable")
    hass.states.async_set("input_boolean.opti_pv_ueberschuss_ladung", "off")
    values, report = snapshot(hass.states, {k: k for k in ["input_boolean.akku_opti_automatik", "input_boolean.opti_ev_akku_pause", "input_boolean.opti_prognose_netzladen", "input_boolean.opti_pv_ueberschuss_ladung"]})
    assert values == {"input_boolean.opti_pv_ueberschuss_ladung": False}
    assert "input_boolean.akku_opti_automatik" not in report
    assert report["input_boolean.opti_prognose_netzladen"]["status"] == "invalid"
    assert report["input_boolean.opti_pv_ueberschuss_ladung"]["status"] == "accepted"


async def test_renamed_helpers_resnapshot_and_cancel(hass):
    seed(hass)
    hass.states.async_set("input_number.custom_low", "9", {"unit_of_measurement": "%"})
    hass.states.async_set("input_number.custom_high", "88", {"unit_of_measurement": "%"})
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)):
        result = await begin_migration(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"adjust_mapping": True})
        assert result["step_id"] == "migration_mapping"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"minsoc": "input_number.custom_low", "maxsoc": "input_number.custom_high"})
        assert "88.0" in result["description_placeholders"]["report"]
        assert "custom_high" in result["description_placeholders"]["report"]
        hass.config_entries.flow.async_abort(result["flow_id"])
    assert not hass.config_entries.async_entries(DOMAIN)
    assert hass.states.get("input_number.custom_high").state == "88"


async def test_empty_migration_and_balancing_defaults(hass):
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)), patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await begin_migration(hass)
        assert "[missing]" in result["description_placeholders"]["report"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"accept": True})
        result = await finish_wizard(hass, result, {"sources": {"single_inverter": True}})
    assert result["data"]["shadow_mode"] is True


@pytest.mark.parametrize("interval,expected", [(None, 0), (4, 4)])
async def test_balancing_only_enabled_by_imported_interval(hass, interval, expected):
    if interval is not None:
        from custom_components.opti_akku.definitions import NUMBER_DEFINITIONS
        key = "input_number.opti_balancing_intervall_tage"
        unit = NUMBER_DEFINITIONS[key].get("unit_of_measurement")
        hass.states.async_set(key, str(interval), {"unit_of_measurement": unit})
    with patch("custom_components.opti_akku.config_flow._probe", AsyncMock(return_value=PROBE)), patch("custom_components.opti_akku.async_setup_entry", AsyncMock(return_value=True)):
        result = await begin_migration(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"accept": True})
        result = await finish_wizard(hass, result, {"sources": {"single_inverter": True}})
    assert result["options"]["settings"]["input_number.opti_balancing_intervall_tage"] == expected
