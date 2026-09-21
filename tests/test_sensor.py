"""Focused presentation checks for coordinator-backed sensors."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.const import DOMAIN
from custom_components.opti_akku.sensor import (
    OptiAkkuStateSensor,
    _enable_tomorrow_forecast,
)
from custom_components.opti_akku.sources import build_inputs


def forecast_sensor(value=12.5):
    key = "sensor.opti_forecast_tomorrow_kwh"
    coordinator = Mock()
    coordinator.data = {
        "states": {key: value},
        "metadata": {key: {"unit_of_measurement": "kWh"}},
    }
    coordinator.last_update_success = True
    coordinator.shadow_mode = False
    entry = MockConfigEntry(
        domain="opti_akku",
        title="Opti Test",
        data={"profile": "sma_stp_se"},
    )
    entry.runtime_data = coordinator
    return OptiAkkuStateSensor(entry, key), coordinator


def test_forecast_tomorrow_is_a_normal_energy_sensor():
    sensor, _coordinator = forecast_sensor()

    assert sensor.native_value == 12.5
    assert sensor.available is True
    assert sensor.native_unit_of_measurement == "kWh"
    assert sensor.entity_category is None
    assert sensor.entity_registry_enabled_default is True
    assert sensor.translation_key == "value_forecast_tomorrow_kwh"


def test_forecast_tomorrow_rejects_missing_or_unavailable_values():
    sensor, coordinator = forecast_sensor("unavailable")

    assert sensor.native_value is None
    assert sensor.available is False

    coordinator.data["states"].pop("sensor.opti_forecast_tomorrow_kwh")
    assert sensor.native_value is None
    assert sensor.available is False


def test_forecast_tomorrow_publishes_normalized_source_and_p10():
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    source = SimpleNamespace(
        state="12500",
        last_reported=now,
        attributes={"unit_of_measurement": "Wh", "estimate10": 7000},
    )
    states, attributes, errors = build_inputs(
        {},
        {"sources": {"forecast_tomorrow": "sensor.forecast"}},
        {"sensor.forecast": source},
        now,
    )
    key = "sensor.opti_forecast_tomorrow_kwh"
    sensor, coordinator = forecast_sensor()
    coordinator.data = {
        "states": states,
        "attributes": attributes,
        "metadata": {key: {"unit_of_measurement": "kWh"}},
    }

    assert not errors
    assert sensor.native_value == 12.5
    assert sensor.extra_state_attributes == {"estimate10": 7.0}

    unconfigured, _attributes, errors = build_inputs({}, {}, {}, now)
    assert unconfigured[key] == "unavailable"
    assert "forecast_tomorrow" not in errors


def test_upgrade_enables_only_integration_disabled_forecast(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Opti Test")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_sensor.opti_forecast_tomorrow_kwh"
    entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )

    _enable_tomorrow_forecast(hass, entry)
    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) == entity.entity_id
    assert registry.async_get(entity.entity_id).disabled_by is None

    registry.async_update_entity(
        entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    _enable_tomorrow_forecast(hass, entry)
    assert (
        registry.async_get(entity.entity_id).disabled_by
        is er.RegistryEntryDisabler.USER
    )
