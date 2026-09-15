"""Switch settings for Opti Akku."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.entity import EntityCategory

from .definitions import SWITCH_DEFINITIONS
from .entity import OptiAkkuEntity

WRITE_ENABLE_KEY = "write_enabled"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    entities = [OptiAkkuSettingSwitch(entry, key, definition) for key, definition in SWITCH_DEFINITIONS.items()]
    if not entry.runtime_data.shadow_mode:
        entities.append(OptiAkkuWriteEnableSwitch(entry))
    async_add_entities(entities)


class OptiAkkuSettingSwitch(OptiAkkuEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: ConfigEntry, key: str, definition: dict) -> None:
        super().__init__(entry, key)
        if key == "input_boolean.akku_opti_automatik":
            self._attr_entity_category = None
            self._attr_entity_registry_enabled_default = True
        self._attr_translation_key = key.split(".", 1)[1]
        self._attr_icon = definition.get("icon")
        self._default = definition["default"]

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.settings.get(self._key, self._default))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_setting(self._key, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_setting(self._key, False)


class OptiAkkuWriteEnableSwitch(OptiAkkuEntity, SwitchEntity):
    _attr_translation_key = "write_enabled"
    _attr_icon = "mdi:shield-key"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, WRITE_ENABLE_KEY)

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.write_enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_write_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_write_enabled(False)
