"""Repair issues follow coordinator state and need a user action (P1.4)."""

import json
from pathlib import Path

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.coordinator import OptiCoordinator
from custom_components.opti_akku.engine import StrategyEngine
from custom_components.opti_akku.repairs import ISSUES, async_clear_issues, issue_id
from tests.test_coordinator import device

COMPONENT = Path(__file__).parents[1] / "custom_components" / "opti_akku"


@pytest.fixture
def entry(hass):
    result = MockConfigEntry(domain="opti_akku", title="Opti Test",
        data={"host": "127.0.0.1", "port": 15020, "unit_id": 3, "profile": "sma_stp_se",
              "serial_number": "123456789"},
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


def _issue(hass, key, entry):
    return ir.async_get(hass).async_get_issue("opti_akku", issue_id(key, entry.entry_id))


async def test_changed_identity_opens_issue_until_the_device_matches(hass, entry, coordinator):
    coordinator.device.async_probe.return_value["serial_number"] = "987654321"
    data = await coordinator._async_update_data()
    assert data["identity_mismatch"] is True
    assert data["online"] is False
    issue = _issue(hass, "identity_changed", entry)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"entry": "Opti Test"}
    assert not issue.is_fixable
    coordinator.device.async_probe.return_value["serial_number"] = "123456789"
    data = await coordinator._async_update_data()
    assert data["identity_mismatch"] is False
    assert _issue(hass, "identity_changed", entry) is None


@pytest.mark.parametrize("code", ["tibber_home_mismatch", "tibber_entry_mismatch",
                                  "tibber_home_count", "tibber_config_entries"])
async def test_tibber_selection_error_opens_issue(hass, entry, coordinator, code):
    coordinator._price_provider_error = code
    await coordinator._async_update_data()
    assert _issue(hass, "tibber_config", entry) is not None
    coordinator._price_provider_error = None
    await coordinator._async_update_data()
    assert _issue(hass, "tibber_config", entry) is None


@pytest.mark.parametrize("code", ["tibber_not_ready", "tibber_timeout", "tibber_fetch_failed"])
async def test_transient_tibber_errors_stay_out_of_repairs(hass, entry, coordinator, code):
    coordinator._price_provider_error = code
    await coordinator._async_update_data()
    assert _issue(hass, "tibber_config", entry) is None


async def test_rejected_settings_issue_clears_when_settings_apply(hass, entry, coordinator):
    coordinator.settings_rejected = True
    await coordinator._async_update_data()
    assert _issue(hass, "settings_rejected", entry).severity == ir.IssueSeverity.WARNING
    await coordinator.async_apply_settings({"input_number.minsoc": 15}, "rev-2")
    assert coordinator.settings_rejected is False
    await coordinator._async_update_data()
    assert _issue(hass, "settings_rejected", entry) is None


async def test_rejected_options_patch_marks_settings_rejected(hass, entry, coordinator):
    from custom_components.opti_akku import _async_options_updated

    entry.runtime_data = coordinator
    coordinator.connection_config = dict(entry.data)
    coordinator.source_options = {key: value for key, value in entry.options.items()}
    hass.config_entries.async_update_entry(entry, options={
        **entry.options, "settings": {"input_number.minsoc": 90, "input_number.maxsoc": 10},
        "settings_revision": "bad"})
    await _async_options_updated(hass, entry)
    assert coordinator.settings_rejected is True


async def test_clear_removes_every_issue_of_the_entry(hass, entry, coordinator):
    coordinator._price_provider_error = "tibber_home_mismatch"
    coordinator.settings_rejected = True
    await coordinator._async_update_data()
    assert _issue(hass, "tibber_config", entry) and _issue(hass, "settings_rejected", entry)
    async_clear_issues(hass, entry.entry_id)
    assert all(_issue(hass, key, entry) is None for key in ISSUES)


def test_every_issue_has_title_and_description_in_all_languages():
    for path in (COMPONENT / "strings.json", *(COMPONENT / "translations").glob("*.json")):
        issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
        for key in ISSUES:
            assert issues[key]["title"] and issues[key]["description"], (path.name, key)
            assert "{entry}" in issues[key]["description"], (path.name, key)
