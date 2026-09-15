"""Manual operating mode selector."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import OptiAkkuEntity

STRATEGY = "Strategie"
OBSERVATION = "observation"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([OptiAkkuModeSelect(entry)])


class OptiAkkuModeSelect(OptiAkkuEntity, SelectEntity):
    _attr_translation_key = "operating_mode"
    _attr_icon = "mdi:battery-cog"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "operating_mode")

    @property
    def options(self) -> list[str]:
        return [STRATEGY if self.coordinator.strategy_enabled else OBSERVATION, *self.coordinator.supported_modes]

    @property
    def available(self) -> bool:
        return True

    @property
    def current_option(self) -> str:
        return self.coordinator.manual_mode or (STRATEGY if self.coordinator.strategy_enabled else OBSERVATION)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_mode("Beobachtung" if option == OBSERVATION else option)
