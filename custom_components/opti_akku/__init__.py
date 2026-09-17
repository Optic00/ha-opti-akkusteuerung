"""Config-entry lifecycle for Opti Akku."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.components.modbus import async_get_unit
from modbus_connection import ModbusTcpParams

from .const import DOMAIN, NO_RELOAD_OPTION_KEYS, PLATFORMS
from .coordinator import OptiCoordinator
from .engine import StrategyEngine
from .sma import SmaDevice


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Acquire the HA-managed connection; no YAML hub or own socket."""
    backend = entry.data.get("backend", "sma")
    if backend == "huawei_solar":
        from .huawei import HuaweiDevice
        if not entry.data.get("shadow_mode", True) and not entry.data.get("huawei_controls"):
            raise ConfigEntryError("An active Huawei entry requires verified actuator bindings")
        from .device import DeviceError
        try:
            device = HuaweiDevice(hass, entry_id=entry.data["huawei_entry_id"],
                device_id=entry.data["huawei_device_id"], sources=entry.data["huawei_sources"],
                grid_positive=entry.data["grid_positive"], source_max_age=entry.options.get("source_max_age", 900),
                controls=entry.data.get("huawei_controls"), read_only=entry.data.get("shadow_mode", True),
                grid_charge_for_surplus=entry.data.get("huawei_grid_surplus", False))
        except (ValueError, TypeError, KeyError, DeviceError) as err:
            raise ConfigEntryError(f"Invalid Huawei configuration: {type(err).__name__}") from err
    elif backend not in ("sma", "sma_modbus"):
        raise ConfigEntryError("Unsupported device backend")
    endpoint = (entry.data.get(CONF_HOST, "huawei").strip().lower(), entry.data.get(CONF_PORT, 0), entry.data.get("unit_id", 0))
    if backend == "huawei_solar":
        endpoint = (backend, entry.data["huawei_entry_id"], entry.data["huawei_device_id"])
    # Only the SMA backend acquires a shared HA Modbus unit.
    owners = hass.data.setdefault(f"{DOMAIN}_writers", {})
    shadow = entry.data.get("shadow_mode", backend == "huawei_solar") is True
    if not shadow and endpoint in owners and owners[endpoint] != entry.entry_id:
        raise ConfigEntryError("This inverter already has an Opti Akku controller")
    if not shadow:
        owners[endpoint] = entry.entry_id
    @callback
    def release_owner() -> None:
        if owners.get(endpoint) == entry.entry_id:
            owners.pop(endpoint, None)

    entry.async_on_unload(release_owner)
    if backend in ("sma", "sma_modbus"):
        unit = async_get_unit(hass, entry, ModbusTcpParams(host=endpoint[0], port=endpoint[1]), endpoint[2])
        device = SmaDevice(unit, read_only=shadow)
    engine = await hass.async_add_executor_job(StrategyEngine)
    coordinator = OptiCoordinator(hass, entry, device, engine)
    entry.runtime_data = coordinator
    await coordinator.async_restore()
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    coordinator.async_listen_sources()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop writes before releasing entities and the shared Modbus unit."""
    coordinator = entry.runtime_data
    await coordinator.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


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
