"""Dynamic virtual and connectivity binary sensors."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.entity import EntityCategory

from .entity import OptiAkkuEntity, coordinator_data, readable_name


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new_entities() -> None:
        states = (coordinator.data or {}).get("states", {})
        keys = [key for key in states if key.startswith("binary_sensor.") and key not in known]
        if keys:
            known.update(keys)
            async_add_entities(OptiAkkuStateBinarySensor(entry, key) for key in keys)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class OptiAkkuStateBinarySensor(OptiAkkuEntity, BinarySensorEntity):
    def __init__(self, entry: ConfigEntry, key: str) -> None:
        super().__init__(entry, key)
        meta = (entry.runtime_data.data or {}).get("metadata", {}).get(key, {})
        if key != "binary_sensor.opti_connection":
            self._attr_name = meta.get("name", readable_name(key))
        self._attr_icon = meta.get("icon")
        if key != "binary_sensor.opti_connection":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = key in {"binary_sensor.opti_block_violation", "binary_sensor.opti_write_stalled"}
        try:
            self._attr_device_class = BinarySensorDeviceClass(meta["device_class"])
        except (KeyError, ValueError):
            self._attr_device_class = None
        if key == "binary_sensor.opti_connection":
            self._attr_translation_key = "connection"
            self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        value = coordinator_data(self).get("states", {}).get(self._key)
        if value is None or (isinstance(value, str) and value.lower() in {"unknown", "unavailable"}):
            return None
        if isinstance(value, str):
            return value.lower() in {"on", "true", "yes", "1"}
        return bool(value)

    @property
    def extra_state_attributes(self):
        return coordinator_data(self).get("attributes", {}).get(self._key)
