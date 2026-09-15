"""Explicit start for the bounded read-only observation."""

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import OptiAkkuEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([OptiDemandHistoryImport(entry)])
    if not entry.runtime_data.shadow_mode:
        async_add_entities([OptiNotificationTest(entry)])
    if entry.runtime_data.shadow_mode:
        async_add_entities([OptiShadowStart(entry), OptiShadowStop(entry)])


class OptiShadowStart(OptiAkkuEntity, ButtonEntity):
    _attr_translation_key = "shadow_start"
    _attr_icon = "mdi:eye-clock"

    def __init__(self, entry):
        super().__init__(entry, "shadow_start")

    async def async_press(self):
        await self.coordinator.async_start_shadow()


class OptiShadowStop(OptiAkkuEntity, ButtonEntity):
    _attr_translation_key = "shadow_stop"
    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, entry):
        super().__init__(entry, "shadow_stop")

    async def async_press(self):
        await self.coordinator.async_stop_shadow()


class OptiNotificationTest(OptiAkkuEntity, ButtonEntity):
    _attr_translation_key = "notification_test"
    _attr_icon = "mdi:bell-check"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry):
        super().__init__(entry, "notification_test")

    async def async_press(self):
        await self.coordinator.alerts.async_test()


class OptiDemandHistoryImport(OptiAkkuEntity, ButtonEntity):
    _attr_translation_key = "demand_history_import"
    _attr_icon = "mdi:database-import"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry):
        super().__init__(entry, "demand_history_import")

    async def async_press(self):
        await self.coordinator.async_import_demand_history()
