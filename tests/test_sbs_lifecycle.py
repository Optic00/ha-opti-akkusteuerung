"""SBS entry lifecycle with the real driver: setup, entity activation, reload and unload never write (#170)."""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import RELOAD_AFTER_UPDATE_DELAY, ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.opti_akku.sbs import SbsDevice
from tests.test_sbs import FakeUnit


async def test_sbs_entry_survives_activation_reload_and_unload_without_writes(hass):
    entry = MockConfigEntry(domain="opti_akku", title="Opti SBS",
        data={"host": "127.0.0.1", "port": 15020, "unit_id": 3, "profile": "sma_sbs", "shadow_mode": True},
        options={"single_writer_confirmed": True, "single_inverter": True, "sources": {}})
    entry.add_to_hass(hass)
    unit = FakeUnit()
    with patch("custom_components.opti_akku.async_get_unit", return_value=unit):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        before = entry.runtime_data
        assert isinstance(before.device, SbsDevice)
        assert not hass.data.get("opti_akku_writers")

        registry = er.async_get(hass)
        disabled = [item for item in er.async_entries_for_config_entry(registry, entry.entry_id)
                    if item.disabled_by is er.RegistryEntryDisabler.INTEGRATION]
        assert disabled
        target = disabled[0].entity_id
        # The real HA handler reloads the entry 30 s after an entity is enabled.
        registry.async_update_entity(target, disabled_by=None)
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1))
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        after = entry.runtime_data
        assert after is not before
        assert after.shadow_mode is True
        assert hass.states.get(target) is not None
        assert not hass.data.get("opti_akku_writers")

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert unit.writes == []
    assert before.device.blocked_write_attempts == 0
    assert after.device.blocked_write_attempts == 0
