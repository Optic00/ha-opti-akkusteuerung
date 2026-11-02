"""Default visibility checks for coordinator-backed binary sensors."""

from unittest.mock import Mock

from homeassistant.helpers.entity import EntityCategory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.binary_sensor import OptiAkkuStateBinarySensor


@pytest.mark.parametrize(
    ("key", "enabled", "category"),
    [
        ("binary_sensor.opti_balancing_haltefenster_aktiv", True, EntityCategory.DIAGNOSTIC),
        ("binary_sensor.opti_block_violation", True, EntityCategory.DIAGNOSTIC),
        ("binary_sensor.opti_write_stalled", True, EntityCategory.DIAGNOSTIC),
        ("binary_sensor.opti_other_diagnostic", False, EntityCategory.DIAGNOSTIC),
        ("binary_sensor.opti_connection", True, None),
    ],
)
def test_binary_sensor_default_visibility(key, enabled, category):
    coordinator = Mock()
    coordinator.data = {"states": {key: "off"}}
    coordinator.shadow_mode = False
    entry = MockConfigEntry(
        domain="opti_akku", title="Opti Test", data={"profile": "sma_stp_se"}
    )
    entry.runtime_data = coordinator

    sensor = OptiAkkuStateBinarySensor(entry, key)

    assert sensor.entity_registry_enabled_default is enabled
    assert sensor.entity_category is category
