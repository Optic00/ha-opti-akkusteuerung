"""Config-entry lifecycle for Opti Akku."""

import asyncio
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.components.modbus import async_get_unit
from modbus_connection import ModbusTcpParams

from .const import DOMAIN, NO_RELOAD_OPTION_KEYS, PLATFORMS
from .coordinator import OptiCoordinator
from .engine import StrategyEngine
from .errors import translated
from .sunny_island import READ_ONLY_SMA_PROFILES
from .sma import SmaDevice
from .source_quality import measurement_max_age

RELOAD_HANDOVER_KEY = f"{DOMAIN}_reload_handover"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Acquire the HA-managed connection; no YAML hub or own socket."""
    # HA runs unload and setup of one reload in the same task. Only that setup
    # may use the handover: not a setup retry, and not a re-enable after the
    # entry was disabled while it was unloading.
    handover = hass.data.get(RELOAD_HANDOVER_KEY, {}).pop(entry.entry_id, None)
    if handover is not None and handover.get("task") is not asyncio.current_task():
        handover = None
    backend = entry.data.get("backend", "sma")
    if backend == "huawei_solar":
        from .huawei import HuaweiDevice
        if not entry.data.get("shadow_mode", True) and not entry.data.get("huawei_controls"):
            raise translated(ConfigEntryError, "huawei_bindings_required")
        from .device import DeviceError
        try:
            device = HuaweiDevice(hass, entry_id=entry.data["huawei_entry_id"],
                device_id=entry.data["huawei_device_id"], sources=entry.data["huawei_sources"],
                grid_positive=entry.data["grid_positive"], source_max_age=measurement_max_age(entry.options.get("source_max_age", 900)),
                controls=entry.data.get("huawei_controls"), read_only=entry.data.get("shadow_mode", True),
                grid_charge_for_surplus=entry.data.get("huawei_grid_surplus", False))
        except (ValueError, TypeError, KeyError, DeviceError) as err:
            raise translated(ConfigEntryError, "huawei_config_invalid", error=type(err).__name__) from err
    elif backend not in ("sma", "sma_modbus"):
        raise translated(ConfigEntryError, "backend_unsupported")
    read_only_device = READ_ONLY_SMA_PROFILES.get(entry.data.get("profile")) if backend in ("sma", "sma_modbus") else None
    if read_only_device and entry.data.get("shadow_mode") is not True:
        raise translated(ConfigEntryError, "profile_read_only")
    endpoint = (entry.data.get(CONF_HOST, "huawei").strip().lower(), entry.data.get(CONF_PORT, 0), entry.data.get("unit_id", 0))
    if backend == "huawei_solar":
        endpoint = (backend, entry.data["huawei_entry_id"], entry.data["huawei_device_id"])
    # Only the SMA backend acquires a shared HA Modbus unit.
    owners = hass.data.setdefault(f"{DOMAIN}_writers", {})
    shadow = entry.data.get("shadow_mode", backend == "huawei_solar") is True
    if not shadow and endpoint in owners and owners[endpoint] != entry.entry_id:
        raise translated(ConfigEntryError, "inverter_already_controlled")
    if not shadow:
        owners[endpoint] = entry.entry_id
    @callback
    def release_owner() -> None:
        if owners.get(endpoint) == entry.entry_id:
            owners.pop(endpoint, None)

    entry.async_on_unload(release_owner)
    if backend in ("sma", "sma_modbus"):
        unit = async_get_unit(hass, entry, ModbusTcpParams(host=endpoint[0], port=endpoint[1]), endpoint[2])
        device = read_only_device(unit) if read_only_device else SmaDevice(unit, read_only=shadow)
    engine = await hass.async_add_executor_job(StrategyEngine)
    coordinator = OptiCoordinator(hass, entry, device, engine)
    entry.runtime_data = coordinator
    await coordinator.async_restore(handover)

    @callback
    def entity_enabled(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        # HA reloads the whole entry 30 s after one of its entities is enabled
        # (#183). Remember that, so this reload can keep manual control. HA
        # re-arms its shared timer on every such change, so any later change
        # also extends an already noted enable.
        item = er.async_get(hass).async_get(event.data["entity_id"])
        if item is None or item.disabled_by is not None:
            return
        if item.config_entry_id == entry.entry_id or coordinator.entity_enable_noted:
            coordinator.note_entity_enabled()

    entry.async_on_unload(hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, entity_enabled, event_filter=_disabled_by_changed))
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    coordinator.async_listen_sources()
    coordinator.async_start_watchdog()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop writes before releasing entities and the shared Modbus unit."""
    coordinator = entry.runtime_data
    # Whether this unload follows an entity enable is decided before the pause,
    # which may take long. The pause itself always runs: if the following
    # setup fails, the inverter must not keep a forced command.
    after_enable = coordinator.entity_enable_noted
    await coordinator.async_stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # The control state is read only now, after the entities are gone, so no
    # later mode or write change is lost. Recheck the entry and HA as well.
    # Without a confirmed pause, no forced mode resumes.
    if (unloaded and after_enable and entry.disabled_by is None and hass.is_running
            and not coordinator.pause_unconfirmed and (handover := coordinator.reload_handover()) is not None):
        hass.data.setdefault(RELOAD_HANDOVER_KEY, {})[entry.entry_id] = {
            **handover, "at": time.monotonic(), "task": asyncio.current_task()}
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop a reload handover of a removed entry."""
    hass.data.get(RELOAD_HANDOVER_KEY, {}).pop(entry.entry_id, None)


@callback
def _disabled_by_changed(event_data: er.EventEntityRegistryUpdatedData) -> bool:
    return event_data["action"] == "update" and "disabled_by" in event_data["changes"]


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator = entry.runtime_data
    sources = {
        key: value
        for key, value in entry.options.items()
        if key not in NO_RELOAD_OPTION_KEYS
    }
    if coordinator.connection_config == dict(entry.data) and coordinator.source_options == sources:
        revision = entry.options.get("settings_revision")
        if revision and revision != coordinator._settings_revision:
            try:
                await coordinator.async_apply_settings(entry.options.get("settings", {}), revision)
            except HomeAssistantError:
                coordinator._last_error = "Einstellungen nicht übernommen: Grenzwerte oder benötigte Quellen sind nicht mehr gültig. Bitte erneut konfigurieren."
                # Discard the rejected patch so a restart cannot apply it later.
                hass.config_entries.async_update_entry(entry, options={**entry.options,
                    "settings": {}, "settings_revision": coordinator._settings_revision})
                coordinator.async_set_updated_data({**(coordinator.data or {}), "last_error": coordinator._last_error})
        return
    await hass.config_entries.async_reload(entry.entry_id)
