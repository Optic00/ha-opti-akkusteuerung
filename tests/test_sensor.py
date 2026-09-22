"""Focused presentation checks for coordinator-backed sensors."""

from datetime import UTC, datetime
import logging
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.const import DOMAIN
from custom_components.opti_akku.sensor import OptiAkkuStateSensor
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


async def setup_sensor_platform(hass, disabled_by=None):
    sensor, coordinator = forecast_sensor()
    entry = sensor._entry
    entry.add_to_hass(hass)
    coordinator.device = Mock(supports_write_value_evidence=False)
    coordinator.async_add_listener.return_value = Mock()
    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_sensor.opti_forecast_tomorrow_kwh"
    existing = None
    if disabled_by is not None:
        existing = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            config_entry=entry,
            disabled_by=disabled_by,
        )
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    assert await component.async_setup_entry(entry)
    return entry, registry, unique_id, existing, component


async def test_fresh_forecast_tomorrow_is_enabled_by_entity_platform(hass):
    _entry, registry, unique_id, _existing, _component = await setup_sensor_platform(hass)

    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    entity = registry.async_get(entity_id)
    assert entity.disabled_by is None
    assert hass.states.get(entity_id).state == "12.5"


@pytest.mark.parametrize(
    "disabled_by",
    [er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER],
)
async def test_existing_forecast_disablement_and_id_survive_platform_setup(
    hass, disabled_by
):
    _entry, registry, unique_id, existing, _component = await setup_sensor_platform(
        hass, disabled_by
    )

    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) == existing.entity_id
    assert registry.async_get(existing.entity_id).disabled_by is disabled_by
    assert hass.states.get(existing.entity_id) is None
