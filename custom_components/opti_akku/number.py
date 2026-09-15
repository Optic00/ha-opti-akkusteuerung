"""Number settings for Opti Akku."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.entity import EntityCategory

from .definitions import NUMBER_DEFINITIONS
from .entity import OptiAkkuEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(OptiAkkuNumber(entry, key, definition) for key, definition in NUMBER_DEFINITIONS.items())


class OptiAkkuNumber(OptiAkkuEntity, NumberEntity):
    """Editable strategy setting."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: ConfigEntry, key: str, definition: dict) -> None:
        super().__init__(entry, key)
        self._attr_translation_key = key.split(".", 1)[1]
        self._attr_icon = definition.get("icon")
        self._attr_native_min_value = definition["min"]
        self._attr_native_max_value = definition["max"]
        self._attr_native_step = definition["step"]
        self._attr_native_unit_of_measurement = definition.get("unit_of_measurement")
        self._attr_mode = NumberMode(definition.get("mode", "box"))
        self._default = definition["default"]

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float:
        return self.coordinator.settings.get(self._key, self._default)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_setting(self._key, value)
