"""Config flow for Opti Akku."""

from __future__ import annotations

from typing import Any
from copy import deepcopy
from uuid import uuid4

from homeassistant.util import dt as dt_util
from .definitions import NUMBER_DEFINITIONS, SWITCH_DEFINITIONS
from .coordinator import validate_setting
from .migration import MIGRATABLE, snapshot, describe
from .sources import normalize_price_unit, build_inputs, finite

from modbus_connection import ModbusError, ModbusTcpParams
import voluptuous as vol

from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)

from .const import (
    DEFAULT_OPTIONS,
    DEFAULT_PORT,
    DEFAULT_UNIT_ID,
    DOMAIN,
    EV_SOURCE_KEYS,
    SOURCE_DEFINITIONS,
)
from .sma import SmaDevice, UnsupportedDeviceError
from .device import DeviceError

CONF_UNIT_ID = "unit_id"
CONF_PROFILE = "profile"
PROFILE_SMA_STP_SE = "sma_stp_se"
BACKEND_SMA = "sma_modbus"
BACKEND_HUAWEI = "huawei_solar"
HUAWEI_REQUIRED_ROLES = (
    "soc", "battery_capacity_kwh", "battery_power_w", "pv_power_w", "grid_power_w"
)
HUAWEI_OPTIONAL_ROLES = ("battery_temp", "pv_generation_w")


def _device_unique_id(connection: dict[str, Any], probe: dict[str, Any]) -> str:
    if connection.get("backend") == BACKEND_HUAWEI:
        return f"{'shadow:' if connection.get('shadow_mode', True) else ''}{BACKEND_HUAWEI}:{connection['huawei_device_id']}"
    prefix = "shadow:" if connection.get("shadow_mode") is True else ""
    if serial_number := probe.get("serial_number"):
        return f"{prefix}{PROFILE_SMA_STP_SE}:{serial_number}"
    return (
        f"{prefix}{connection[CONF_HOST].lower()}:{connection[CONF_PORT]}:"
        f"{connection[CONF_UNIT_ID]}"
    )


def _connection_schema(defaults: dict[str, Any], *, initial: bool = False) -> vol.Schema:
    schema = vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): vol.All(
                TextSelector(), vol.Strip, vol.Length(min=1)
            ),
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                NumberSelector(
                    NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=1, max=65535)
                ),
                vol.Coerce(int),
            ),
            vol.Required(
                CONF_UNIT_ID, default=defaults.get(CONF_UNIT_ID, DEFAULT_UNIT_ID)
            ): vol.All(
                NumberSelector(
                    NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=1, max=247)
                ),
                vol.Coerce(int),
            ),
            vol.Required(
                CONF_PROFILE, default=defaults.get(CONF_PROFILE, PROFILE_SMA_STP_SE)
            ): SelectSelector(SelectSelectorConfig(options=[PROFILE_SMA_STP_SE])),
        }
    )
    if initial:
        return schema.extend({vol.Optional("shadow_mode", default=True): BooleanSelector(),
                              vol.Optional("migrate_legacy", default=False): BooleanSelector()})
    return schema


def _sources_schema(defaults: dict[str, Any], *, require_confirmation: bool, shadow_mode: bool = False) -> vol.Schema:
    sources = defaults.get("sources", {})
    schema: dict[vol.Marker, Any] = {}
    for key in (*SOURCE_DEFINITIONS, *EV_SOURCE_KEYS):
        marker = vol.Optional(key, description={"suggested_value": sources[key]}) if key in sources else vol.Optional(key)
        domains = ["select", "input_select"] if key.endswith("_mode") else ["binary_sensor"] if key.endswith("_charging") else ["sensor"]
        schema[marker] = EntitySelector(EntitySelectorConfig(domain=domains))
    schema.update(
        {
            vol.Required(
                "price_unit", default=defaults.get("price_unit", "EUR/kWh")
            ): SelectSelector(SelectSelectorConfig(options=["EUR/kWh", "ct/kWh"])),
            vol.Required(
                "source_max_age", default=defaults.get("source_max_age", 900)
            ): vol.All(
                NumberSelector(NumberSelectorConfig(min=1, max=3600, mode=NumberSelectorMode.BOX)),
                vol.Coerce(int),
            ),
            vol.Required(
                "forecast_max_age", default=defaults.get("forecast_max_age", 21600)
            ): vol.All(
                NumberSelector(NumberSelectorConfig(min=1, max=86400, mode=NumberSelectorMode.BOX)),
                vol.Coerce(int),
            ),
            vol.Required(
                "price_max_age", default=defaults.get("price_max_age", 7200)
            ): vol.All(
                NumberSelector(NumberSelectorConfig(min=1, max=86400, mode=NumberSelectorMode.BOX)),
                vol.Coerce(int),
            ),
            vol.Required(
                "single_inverter", default=defaults.get("single_inverter", False)
            ): BooleanSelector(),
            vol.Required(
                "single_writer_confirmed",
                default=False if require_confirmation else defaults.get("single_writer_confirmed", False),
            ): BooleanSelector(),
            (vol.Optional("shadow_reference_mode", description={"suggested_value": defaults["shadow_reference_mode"]})
             if defaults.get("shadow_reference_mode") else vol.Optional("shadow_reference_mode")):
                EntitySelector(EntitySelectorConfig(domain=["input_select", "select", "sensor"])),
        }
    )
    if shadow_mode:
        schema = {key: value for key, value in schema.items() if key.schema != "single_writer_confirmed"}
    return vol.Schema(schema)


async def _probe(hass: Any, data: dict[str, Any]) -> dict[str, Any]:
    params = ModbusTcpParams(host=data[CONF_HOST].strip(), port=data[CONF_PORT])
    async with async_get_temporary_unit(hass, params, data[CONF_UNIT_ID]) as unit:
        return await SmaDevice(unit, read_only=True).async_probe()


def _huawei_entry_for_device(hass: Any, device_id: str) -> str | None:
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    matches = [
        entry.entry_id
        for entry in hass.config_entries.async_entries(BACKEND_HUAWEI)
        if entry.state is ConfigEntryState.LOADED and entry.entry_id in device.config_entries
    ]
    return matches[0] if len(matches) == 1 else None


def _huawei_entities(hass: Any, device_id: str, entry_id: str, domain: str = "sensor", role: str | None = None) -> list[str]:
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    devices = dr.async_get(hass)
    selected = devices.async_get(device_id)
    allowed = {
        device.id
        for device in devices.devices.values()
        if device.id == device_id or device.via_device_id == device_id
        or (role == "grid_power_w" and selected is not None and selected.via_device_id is not None
            and device.via_device_id == selected.via_device_id)
    }
    return sorted(
        entity.entity_id
        for entity in er.async_get(hass).entities.values()
        if entity.platform == BACKEND_HUAWEI
        and entity.config_entry_id == entry_id
        and entity.device_id in allowed
        and entity.domain == domain
    )


# The same sections power first-time setup and the editable options menu.
SETTING_GROUPS = {
    "battery": ["minsoc", "maxsoc", "akkusteuerung_max_ladestaerke", "akkusteuerung_max_entladestaerke", "opti_pv_ueberschuss_ladung"],
    "tariff": ["ladepreis", "mindestpreisdifferenz_lade_entladepreis", "opti_einspeiseverguetung_ct", "opti_netzlade_spread_ct", "opti_peak_min_aufschlag_ct", "opti_halte_spread_ct", "hausakku_aus_netz_laden"],
    "forecast": ["opti_forecast_optimismus", "opti_peak_verbrauch_kw", "opti_prognose_netzladen"],
    "ev": ["opti_ev_akku_pause"],
    "balancing": ["opti_balancing_intervall_tage", "opti_balancing_karenz_tage", "opti_balancing_max_ct", "opti_balancing_done_soc", "opti_balancing_spreizungs_schwelle", "opti_balancing_bedarf_cooldown_tage", "opti_balancing_netzladen"],
    "finish": ["akku_opti_automatik"],
}
_GROUPED = {key for keys in SETTING_GROUPS.values() for key in keys}
SETTING_GROUPS["advanced"] = [key.split(".", 1)[1] for key in (*NUMBER_DEFINITIONS, *SWITCH_DEFINITIONS) if key.split(".", 1)[1] not in _GROUPED]
SOURCE_GROUPS = {"sources": ["house_consumption", "pv_generation", "pv_power"],
                 "tariff": ["price_current", "price_series"],
                 "forecast": ["forecast_today", "forecast_tomorrow", "forecast_remaining"],
                 "ev": list(EV_SOURCE_KEYS), "balancing": ["cell_spread"]}
OPTION_GROUPS = {"sources": ["strategy_enabled", "single_inverter", "plant_mode", "additional_ac_sources", "excluded_load_sources", "plant_meter_confirmed", "forecast_min_load_w"], "tariff": ["price_unit", "price_provider"],
                 "advanced": ["source_max_age", "forecast_max_age", "price_max_age"],
                 "finish": ["shadow_reference_mode", "single_writer_confirmed"]}
DEFINITIONS = {**NUMBER_DEFINITIONS, **SWITCH_DEFINITIONS}


def _setting_key(name: str) -> str:
    return ("input_number." if "input_number." + name in NUMBER_DEFINITIONS else "input_boolean.") + name


class WizardSections:
    """Draft-only changes until the explicit final save."""

    def _init_draft(self, options: dict, settings: dict | None = None) -> None:
        self._draft = deepcopy({**DEFAULT_OPTIONS, **options})
        self._settings = {key: definition["default"] for key, definition in DEFINITIONS.items()}
        self._settings.update({k: v for k, v in (settings or options.get("settings", {})).items() if k in DEFINITIONS})
        self._initial_settings = dict(self._settings)
        self._settings_dirty = False
        self._edited_settings = set()
        self._guided = True

    def _schema(self, section: str) -> vol.Schema:
        keys = set(SOURCE_GROUPS.get(section, []) + OPTION_GROUPS.get(section, []))
        source_schema = _sources_schema(self._draft, require_confirmation=False,
                                       shadow_mode=self._connection.get("shadow_mode", False))
        schema = {marker: validator for marker, validator in source_schema.schema.items() if marker.schema in keys}
        if section == "sources":
            huawei = self._connection.get("backend") == BACKEND_HUAWEI
            if huawei:
                schema = {key: val for key, val in schema.items() if key.schema != "single_inverter"}
            if huawei:
                entity_id = self._connection.get("huawei_sources", {}).get("battery_temp")
                marker = vol.Optional("huawei_battery_temp", description={"suggested_value": entity_id}) if entity_id else vol.Optional("huawei_battery_temp")
                schema[marker] = EntitySelector(EntitySelectorConfig(include_entities=_huawei_entities(
                    self.hass, self._connection["huawei_device_id"], self._connection["huawei_entry_id"], role="battery_temp")))
            schema[vol.Required("strategy_enabled", default=self._draft.get("strategy_enabled", True))] = BooleanSelector()
            schema[vol.Required("plant_mode", default=self._draft.get("plant_mode", "legacy"))] = SelectSelector(
                SelectSelectorConfig(options=["legacy", "external"] if huawei else ["legacy", "balance", "external"], translation_key="plant_mode"))
            for key in ("additional_ac_sources", "excluded_load_sources"):
                schema[vol.Optional(key, default=self._draft.get(key, []))] = EntitySelector(
                    EntitySelectorConfig(domain=["sensor"], multiple=True))
            schema[vol.Required("plant_meter_confirmed", default=self._draft.get("plant_meter_confirmed", False))] = BooleanSelector()
            schema[vol.Required("forecast_min_load_w", default=self._draft.get("forecast_min_load_w", 0))] = NumberSelector(
                NumberSelectorConfig(min=0, max=5000, step=1, unit_of_measurement="W", mode=NumberSelectorMode.BOX))
        if section == "advanced" and self._draft.get("price_provider") == "tibber":
            for marker in list(schema):
                if marker.schema == "price_max_age":
                    schema[marker] = vol.All(NumberSelector(NumberSelectorConfig(
                        min=60, max=86400, mode=NumberSelectorMode.BOX)), vol.Coerce(int))
        if section == "tariff":
            schema[vol.Required("price_provider", default=self._draft.get("price_provider", "entities"))] = SelectSelector(
                SelectSelectorConfig(options=["entities", "tibber"], translation_key="price_provider"))
            if self._draft.get("price_provider") == "tibber":
                schema = {marker: value for marker, value in schema.items()
                          if marker.schema not in ("price_current", "price_series", "price_unit")}
        for name in SETTING_GROUPS.get(section, []):
            key = _setting_key(name)
            definition = DEFINITIONS[key]
            if key in SWITCH_DEFINITIONS:
                selector = BooleanSelector()
            else:
                config = NumberSelectorConfig(min=definition["min"], max=definition["max"],
                                              step=definition["step"], mode=NumberSelectorMode.BOX)
                if definition.get("unit_of_measurement") is not None:
                    config["unit_of_measurement"] = definition["unit_of_measurement"]
                selector = NumberSelector(config)
            schema[vol.Required(name, default=self._settings[key])] = selector
        return vol.Schema(schema)

    def _huawei_temperature_valid(self, connection=None, draft=None) -> bool:
        connection = self._connection if connection is None else connection
        draft = self._draft if draft is None else draft
        state = self.hass.states.get(connection.get("huawei_sources", {}).get("battery_temp", ""))
        if state is None or state.attributes.get("unit_of_measurement") != "°C":
            return False
        value = finite(state.state)
        age = (dt_util.utcnow() - (state.last_reported or state.last_updated)).total_seconds()
        return value is not None and -60 <= value <= 100 and -60 <= age <= draft.get("source_max_age", 900)

    def _huawei_source_error(self, draft=None, connection=None):
        draft = self._draft if draft is None else draft
        connection = self._connection if connection is None else connection
        if connection.get("backend") != BACKEND_HUAWEI:
            return None
        if (not connection.get("shadow_mode", True) or draft.get("strategy_enabled", True)) and not self._huawei_temperature_valid(connection, draft):
            return "huawei_temperature_required"
        if draft.get("single_inverter") or draft.get("plant_mode") == "balance":
            return "huawei_house_source_required"
        return None

    def _validate_sources(self, section: str, draft: dict, settings: dict, connection=None) -> dict:
        if section == "sources" and (error := self._huawei_source_error(draft, connection)):
            return {"base": error}
        if draft.get("strategy_enabled", True) is False and section in ("sources", "tariff", "forecast", "balancing"):
            return {}
        keys = SOURCE_GROUPS.get(section, [])
        if section == "tariff" and draft.get("price_provider") == "tibber":
            return {}
        sources = draft["sources"]
        errors = {}
        if section == "sources" and draft.get("strategy_enabled") is True and draft.get("plant_mode", "legacy") != "balance" and not draft.get("single_inverter") and not sources.get("house_consumption"):
            errors["house_consumption"] = "house_required"
        if section == "sources" and draft.get("plant_mode", "legacy") != "legacy":
            from .plant import plant_entity_ids, validate_plant_sources
            from homeassistant.helpers import entity_registry as er
            if draft.get("plant_mode") == "balance" and draft.get("plant_meter_confirmed") is not True:
                errors["plant_meter_confirmed"] = "plant_confirmation_required"
            if draft.get("plant_mode") == "external" and not sources.get("house_consumption"):
                errors["house_consumption"] = "house_required"
            ac = draft.get("additional_ac_sources", [])
            excluded = draft.get("excluded_load_sources", [])
            combined = ac + excluded
            if len(set(combined)) != len(combined) or any(x in combined for x in (sources.get("pv_power"), sources.get("house_consumption")) if x):
                errors["base"] = "plant_duplicate_source"
            for key, error in validate_plant_sources(draft, self.hass.states, dt_util.utcnow()).items():
                field = ("additional_ac_sources" if key in ac else "excluded_load_sources" if key in excluded else key)
                if field not in ("additional_ac_sources", "excluded_load_sources", "house_consumption", "pv_power", "plant_meter_confirmed", "forecast_min_load_w"):
                    field = "base"
                errors.setdefault(field, error if error in ("missing_or_stale", "unsupported_unit", "negative_value", "invalid_value") else "plant_sources_invalid")
            registry = er.async_get(self.hass)
            for entity_id in plant_entity_ids(draft):
                registered = registry.async_get(entity_id)
                state = self.hass.states.get(entity_id)
                if registered and registered.platform == DOMAIN:
                    errors["base"] = "self_reference"
                elif state is None or finite(state.state) is None:
                    errors["base"] = "missing_or_stale"
                elif state.attributes.get("unit_of_measurement") not in ("W", "kW"):
                    errors["base"] = "unsupported_unit"
        for key in keys:
            entity_id = sources.get(key)
            if entity_id and entity_id not in self.hass.states.async_entity_ids():
                errors[key] = "missing_or_stale"
            if entity_id:
                from homeassistant.helpers import entity_registry as er
                registered = er.async_get(self.hass).async_get(entity_id)
                if registered and registered.platform == DOMAIN:
                    errors[key] = "self_reference"
        _, _, source_errors = build_inputs({}, draft, self.hass.states, dt_util.utcnow())
        for key in keys:
            if key in source_errors:
                errors.setdefault(key, source_errors[key])
        if section == "tariff" and bool(sources.get("price_current")) != bool(sources.get("price_series")):
            errors["base"] = "price_pair_required"
        if section == "tariff":
            for key in keys:
                source = self.hass.states.get(sources.get(key, ""))
                if source:
                    unit = normalize_price_unit(source.attributes.get("unit_of_measurement"))
                    if unit is None:
                        errors[key] = "unsupported_unit"
                    elif unit != draft["price_unit"]:
                        errors[key] = "price_unit_mismatch"
        if section == "forecast" and any(sources.get(k) for k in keys) and not all(sources.get(k) for k in keys):
            errors["base"] = "forecast_set_required"
        if section == "ev":
            for key in ("ev1_power", "ev2_power"):
                source = self.hass.states.get(sources.get(key, ""))
                if source:
                    if finite(source.state) is None:
                        errors[key] = "invalid_value"
                    elif finite(source.state) < 0:
                        errors[key] = "negative_value"
                    elif source.attributes.get("unit_of_measurement") not in ("W", "kW"):
                        errors[key] = "unsupported_unit"
            for n in (1, 2):
                fields = [f"ev{n}_{field}" for field in ("mode", "charging", "power")]
                if any(sources.get(k) for k in fields) and not all(sources.get(k) for k in fields[:2]):
                    errors["base"] = "ev_pair_required"
            if settings["input_boolean.opti_ev_akku_pause"] and not any(sources.get(f"ev{n}_mode") and sources.get(f"ev{n}_charging") for n in (1, 2)):
                errors["base"] = "ev_pair_required"
        return errors

    async def _section(self, section: str, user_input: dict | None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            draft = deepcopy(self._draft)
            connection = deepcopy(self._connection)
            settings = dict(self._settings)
            for key in SOURCE_GROUPS.get(section, []):
                if user_input.get(key):
                    draft["sources"][key] = user_input[key]
                else:
                    draft["sources"].pop(key, None)
            for key in OPTION_GROUPS.get(section, []):
                if key in user_input:
                    draft[key] = user_input[key]
            if section == "sources" and self._connection.get("backend") == BACKEND_HUAWEI:
                draft["single_inverter"] = False
                if user_input.get("huawei_battery_temp"):
                    connection["huawei_sources"]["battery_temp"] = user_input["huawei_battery_temp"]
                else:
                    connection["huawei_sources"].pop("battery_temp", None)
            if section == "finish":
                draft["shadow_reference_mode"] = user_input.get("shadow_reference_mode") or ""
            for name in SETTING_GROUPS.get(section, []):
                if name in user_input:
                    settings[_setting_key(name)] = user_input[name]
            try:
                settings = {key: validate_setting(key, value, settings) for key, value in settings.items()}
            except (HomeAssistantError, ValueError, TypeError, KeyError):
                errors["base"] = "invalid_limits"
            for key, error in self._validate_sources(section, draft, settings, connection).items():
                errors.setdefault(key, error)
            if (section == "battery" and self._connection.get("backend") == BACKEND_HUAWEI
                    and not self._connection.get("shadow_mode", True)):
                cutoff = self.hass.states.get(self._connection.get("huawei_controls", {}).get("cutoff_soc", ""))
                low = finite(cutoff.attributes.get("min")) if cutoff else None
                high = finite(cutoff.attributes.get("max")) if cutoff else None
                if low is None or high is None or not low <= settings["input_number.maxsoc"] <= high:
                    errors["maxsoc"] = "huawei_cutoff_soc_unsupported"
            if self._connection.get("shadow_mode"):
                draft["single_writer_confirmed"] = False
            if not errors:
                self._edited_settings = {key for key in settings if settings[key] != self._initial_settings[key]}
                self._settings_dirty = bool(self._edited_settings)
                self._draft, self._settings, self._connection = draft, settings, connection
                if section == "finish":
                    return await self._finish()
                if section == "tariff" and draft.get("price_provider") == "tibber":
                    return await self.async_step_tibber()
                if not self._guided:
                    return await (self.async_step_features() if section in ("ev", "balancing") else self.async_step_init())
                following = {"sources": "battery", "battery": "tariff", "tariff": "forecast",
                             "forecast": "features", "ev": "balancing" if self._use_balancing else "notifications",
                             "balancing": "notifications"}
                next_section = following[section]
                if section == "battery" and not draft.get("strategy_enabled", True):
                    next_section = "notifications"
                return await getattr(self, "async_step_" + next_section)()
        placeholders = self._summary() if section == "finish" else None
        return self.async_show_form(step_id=section, data_schema=self.add_suggested_values_to_schema(self._schema(section), user_input or {}), errors=errors,
                                    description_placeholders=placeholders)

    def _summary(self) -> dict[str, str]:
        sources = self._draft["sources"]
        connection = (f"Huawei Solar / {self._connection.get('huawei_device_id', '—')}"
                      if self._connection.get("backend") == BACKEND_HUAWEI
                      else f"{self._connection.get(CONF_HOST, '—')}:{self._connection.get(CONF_PORT, '—')} / {self._connection.get(CONF_UNIT_ID, '—')}")
        return {"connection": connection,
                "mode": "Shadow" if self._connection.get("shadow_mode") else "Standard",
                "limits": f'{self._settings["input_number.minsoc"]:g}–{self._settings["input_number.maxsoc"]:g} %',
                "sources": str(len(sources)),
                "price": (f"Tibber: {self._draft.get('tibber_home', '—')} (EUR/kWh)"
                          if self._draft.get("price_provider") == "tibber"
                          else str(sources.get("price_current", "—"))),
                "forecast": str(sources.get("forecast_today", "—"))}

    async def _validate_final_sources(self, settings: dict) -> bool:
        sections = ["ev"]
        if self._draft.get("strategy_enabled", True):
            sections[:0] = ["sources", "tariff", "forecast", "balancing"]
        if self._draft.get("strategy_enabled", True) and self._draft.get("price_provider") == "tibber":
            from .tibber_prices import TibberPriceError, async_fetch_prices

            try:
                homes = await async_fetch_prices(self.hass)
            except TibberPriceError:
                return False
            home = self._draft.get("tibber_home")
            if home not in homes or homes[home].entry_id != self._draft.get("tibber_entry_id"):
                return False
        if self._connection.get("backend") == BACKEND_HUAWEI:
            from .huawei import HuaweiConfigurationError, HuaweiDevice

            try:
                device = HuaweiDevice(self.hass,
                    entry_id=self._connection["huawei_entry_id"],
                    device_id=self._connection["huawei_device_id"],
                    sources=self._connection["huawei_sources"],
                    source_max_age=self._draft.get("source_max_age", 900),
                    grid_positive=self._connection["grid_positive"],
                    controls=self._connection.get("huawei_controls"))
                await device.async_probe()
                await device.async_read()
            except (HuaweiConfigurationError, KeyError, TypeError, DeviceError):
                return False
            if device.last_read_errors.keys() & set(HUAWEI_REQUIRED_ROLES):
                return False
            if (not self._connection.get("shadow_mode", True) or self._draft.get("strategy_enabled", True)) and not self._huawei_temperature_valid():
                return False
            if not self._connection.get("shadow_mode", True):
                if device.controller is None:
                    return False
                try:
                    device.controller._target("cutoff_soc", float(settings["input_number.maxsoc"]))
                except (DeviceError, KeyError, ValueError):
                    return False
        return not any(
            self._validate_sources(section, self._draft, settings) for section in sections
        )

    async def async_step_huawei_controls(self, user_input=None):
        from .huawei import HuaweiDevice, HuaweiConfigurationError
        from .huawei_control import CONTROL_DOMAINS
        errors = {}
        defaults = self._connection.get("huawei_controls", {})
        if user_input is not None:
            controls = {role: user_input[role] for role in CONTROL_DOMAINS}
            try:
                device = HuaweiDevice(self.hass,
                    entry_id=self._connection["huawei_entry_id"], device_id=self._connection["huawei_device_id"],
                    sources=self._connection["huawei_sources"], controls=controls,
                    grid_positive=self._connection["grid_positive"])
                await device.async_probe()
            except (DeviceError, HuaweiConfigurationError):
                errors["base"] = "huawei_write_access_missing"
            else:
                self._connection["huawei_controls"] = controls
                self._connection["huawei_grid_surplus"] = user_input.get("huawei_grid_surplus", False)
                self._draft["single_writer_confirmed"] = False
                return await (self.async_step_init() if hasattr(self, "_entry") else self.async_step_sources())
        schema = {}
        for role, domain in CONTROL_DOMAINS.items():
            entities = _huawei_entities(self.hass, self._connection["huawei_device_id"],
                                       self._connection["huawei_entry_id"], domain)
            suggestion = defaults.get(role)
            if not suggestion:
                from homeassistant.helpers import entity_registry as er
                keys = {"charge_limit": "storage_maximum_charging_power",
                        "discharge_limit": "storage_maximum_discharging_power",
                        "grid_limit": "storage_power_of_charge_from_grid",
                        "cutoff_soc": "storage_grid_charge_cutoff_state_of_charge"}
                matches = []
                for entity_id in entities:
                    entity = er.async_get(self.hass).async_get(entity_id)
                    state = self.hass.states.get(entity_id)
                    if entity is None or state is None or entity.disabled_by is not None:
                        continue
                    options = set(state.attributes.get("options", []))
                    if ((role in keys and entity.translation_key == keys[role])
                            or (role == "mode" and {"maximise_self_consumption", "time_of_use_luna2000"} <= options)
                            or (role == "excess_pv" and {"charge", "fed_to_grid"} <= options)):
                        matches.append(entity_id)
                if len(matches) == 1:
                    suggestion = matches[0]
            marker = vol.Required(role, description={"suggested_value": suggestion}) if suggestion else vol.Required(role)
            schema[marker] = EntitySelector(EntitySelectorConfig(domain=domain, include_entities=entities))
        schema[vol.Required("huawei_grid_surplus", default=self._connection.get("huawei_grid_surplus", False))] = BooleanSelector()
        return self.async_show_form(step_id="huawei_controls", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_notifications(self, user_input=None):
        choices = [""] + [f"notify.{name}" for name in sorted(self.hass.services.async_services().get("notify", {}))
                           if name not in ("send_message", "persistent_notification")]
        selected = self._draft.get("notification_service", "")
        errors = {}
        if user_input is not None:
            selected = user_input.get("notification_service", "")
            if selected not in choices:
                errors["base"] = "notification_service_unavailable"
            else:
                self._draft["notification_service"] = selected
                return await (self.async_step_finish() if self._guided else self.async_step_init())
        if selected not in choices:
            choices.append(selected)
        return self.async_show_form(step_id="notifications", errors=errors, data_schema=vol.Schema({
            vol.Required("notification_service", default=selected): SelectSelector(SelectSelectorConfig(
                options=[{"value": value, "label": value or "Home Assistant"} for value in choices])),
        }))

    async def async_step_observation(self, user_input=None):
        """Configure comparison only. Never replace active source mappings."""
        cfg = self._draft.get("source_observation", {})
        errors = {}
        if user_input is not None:
            for key in ("watch_sources", "additional_ac_sources", "excluded_load_sources"):
                values = user_input.get(key, [])
                if len(values) > 8 or len(set(values)) != len(values):
                    errors["base"] = "observation_sources"
            selected = [user_input.get("instant_house"), user_input.get("gross_house"), *user_input.get("watch_sources", []),
                        *user_input.get("additional_ac_sources", []), *user_input.get("excluded_load_sources", [])]
            if len(set(filter(None, selected))) > 16 or any(value and self.hass.states.get(value) is None for value in selected):
                errors["base"] = "observation_sources"
            if not errors:
                self._draft["source_observation"] = dict(user_input)
                return await self.async_step_init()
        schema = {vol.Required("enabled", default=cfg.get("enabled", False)): BooleanSelector(),
                  vol.Required("meter_confirmed", default=cfg.get("meter_confirmed", False)): BooleanSelector(),
                  vol.Required("legacy_floor_w", default=cfg.get("legacy_floor_w", 0)): NumberSelector(NumberSelectorConfig(min=0,max=5000,unit_of_measurement="W",mode=NumberSelectorMode.BOX))}
        for key in ("instant_house", "gross_house", "watch_sources", "additional_ac_sources", "excluded_load_sources"):
            marker = vol.Optional(key, description={"suggested_value":cfg[key]}) if cfg.get(key) else vol.Optional(key)
            schema[marker] = EntitySelector(EntitySelectorConfig(domain=["sensor"],multiple=key not in ("instant_house", "gross_house")))
        return self.async_show_form(step_id="observation",data_schema=vol.Schema(schema),errors=errors)

    async def async_step_ev_preparation(self, user_input=None):
        from homeassistant.helpers import entity_registry as er
        cfg = self._draft.get("ev_preparation", {})
        errors = {}
        if user_input is not None:
            for key in ("vehicle_soc", "charging", "away"):
                entity_id = user_input.get(key)
                if user_input.get("enabled") and key != "away" and not entity_id:
                    errors[key] = "missing_or_stale"
                if entity_id:
                    entity = er.async_get(self.hass).async_get(entity_id)
                    state = self.hass.states.get(entity_id)
                    if entity and entity.platform == DOMAIN:
                        errors[key] = "self_reference"
                    elif state is None:
                        errors[key] = "missing_or_stale"
                    elif key == "vehicle_soc" and state.attributes.get("unit_of_measurement") != "%":
                        errors[key] = "ev_soc_unit"
            if not errors:
                self._draft["ev_preparation"] = dict(user_input)
                return await self.async_step_init()
        schema = {vol.Required("enabled", default=cfg.get("enabled", False)): BooleanSelector()}
        for key in ("vehicle_soc", "charging", "away"):
            marker = vol.Optional(key, description={"suggested_value": cfg[key]}) if cfg.get(key) else vol.Optional(key)
            schema[marker] = EntitySelector(EntitySelectorConfig(domain=["sensor"] if key == "vehicle_soc" else ["binary_sensor", "input_boolean"]))
        for key, default, low, high in (("vehicle_threshold", 40, 0, 95), ("house_target", 80, 50, 95)):
            schema[vol.Required(key, default=cfg.get(key, default))] = NumberSelector(NumberSelectorConfig(min=low, max=high, step=1, unit_of_measurement="%", mode=NumberSelectorMode.BOX))
        return self.async_show_form(step_id="ev_preparation", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_demand(self, user_input=None):
        """Configure demand observation and optional profile-based peak reserve."""
        from .demand import SOURCE_KEYS
        from homeassistant.helpers import entity_registry as er
        cfg = self._draft.get("demand_forecast", {})
        errors = {}
        if user_input is not None:
            sources = {key: user_input[key] for key in SOURCE_KEYS if user_input.get(key)}
            for key, entity_id in sources.items():
                registered = er.async_get(self.hass).async_get(entity_id)
                if key == "heat_power" and registered and registered.platform == "powercalc":
                    errors[key] = "demand_heat_meter"
                elif registered and registered.platform == DOMAIN:
                    errors[key] = "self_reference"
                elif self.hass.states.get(entity_id) is None:
                    errors[key] = "missing_or_stale"
            if bool(sources.get("water_temperature")) != bool(sources.get("water_target")):
                errors["base"] = "demand_water_pair"
            if sources.get("heat_power") == self._draft.get("sources", {}).get("house_consumption") and sources.get("heat_power"):
                errors["heat_power"] = "demand_heat_meter"
            if not errors:
                self._draft["demand_forecast"] = {"enabled": user_input["enabled"],
                    "sources": sources, "dhw_cycle_kwh": user_input["dhw_cycle_kwh"],
                    **({"temperature_matching": True} if user_input.get("temperature_matching") else {}),
                    **({"use_for_peak_reserve": True} if user_input.get("use_for_peak_reserve") else {}),
                    **({"history_house": user_input["history_house"]} if user_input.get("history_house") else {})}
                return await self.async_step_init()
        schema = {vol.Required("enabled", default=cfg.get("enabled", False)): BooleanSelector()}
        for key in SOURCE_KEYS:
            domain = ["binary_sensor", "input_boolean", "schedule"] if key in (
                "summer_mode", "heating_active", "dhw_active", "dhw_due") else ["sensor", "number", "input_number"] if key == "water_target" else ["sensor"]
            current = cfg.get("sources", {}).get(key)
            marker = vol.Optional(key, description={"suggested_value": current}) if current else vol.Optional(key)
            schema[marker] = EntitySelector(EntitySelectorConfig(domain=domain))
        schema[vol.Required("use_for_peak_reserve", default=cfg.get("use_for_peak_reserve", False))] = BooleanSelector()
        schema[vol.Required("temperature_matching", default=cfg.get("temperature_matching", False))] = BooleanSelector()
        marker = vol.Optional("history_house", description={"suggested_value": cfg["history_house"]}) if cfg.get("history_house") else vol.Optional("history_house")
        schema[marker] = EntitySelector(EntitySelectorConfig(domain=["sensor"]))
        schema[vol.Required("dhw_cycle_kwh", default=cfg.get("dhw_cycle_kwh", 0))] = NumberSelector(
            NumberSelectorConfig(min=0, max=30, step=0.1, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX))
        return self.async_show_form(step_id="demand", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_sources(self, user_input=None):
        return await self._section("sources", user_input)

    async def async_step_battery(self, user_input=None):
        return await self._section("battery", user_input)

    async def async_step_tariff(self, user_input=None):
        return await self._section("tariff", user_input)

    async def async_step_tibber(self, user_input=None):
        """Use HA's existing Tibber account; never ask for another token."""
        from .tibber_prices import TibberPriceError, async_fetch_prices

        errors = {}
        try:
            homes = await async_fetch_prices(self.hass)
        except TibberPriceError:
            homes = {}
            errors["base"] = "tibber_unavailable"
        selected = self._draft.get("tibber_home")
        if homes and selected is None:
            selected = next(iter(homes))
        if user_input is not None:
            selected = user_input.get("tibber_home")
            if homes and selected not in homes:
                errors["base"] = "tibber_home_changed"
            if user_input.get("tibber_eur_confirmed") is not True:
                errors["tibber_eur_confirmed"] = "tibber_eur_required"
            if self._draft.get("price_max_age", 7200) < 60:
                errors["base"] = "tibber_price_age"
            if not errors:
                self._draft.update(price_provider="tibber", tibber_home=selected,
                                   tibber_eur_confirmed=True, price_unit="EUR/kWh",
                                   tibber_entry_id=homes[selected].entry_id)
                for key in ("price_current", "price_series"):
                    self._draft["sources"].pop(key, None)
                return await (self.async_step_forecast() if self._guided else self.async_step_init())
        choices = list(homes)
        if selected and selected not in choices:
            choices.append(selected)
        marker = (vol.Required("tibber_home", default=selected) if selected
                  else vol.Required("tibber_home"))
        return self.async_show_form(step_id="tibber", errors=errors, data_schema=vol.Schema({
            marker: SelectSelector(SelectSelectorConfig(options=choices)),
            vol.Required("tibber_eur_confirmed", default=self._draft.get("tibber_eur_confirmed", False)): BooleanSelector(),
        }))

    async def async_step_forecast(self, user_input=None):
        return await self._section("forecast", user_input)

    async def async_step_ev(self, user_input=None):
        return await self._section("ev", user_input)

    async def async_step_balancing(self, user_input=None):
        return await self._section("balancing", user_input)

    async def async_step_advanced(self, user_input=None):
        return await self._section("advanced", user_input)

    async def async_step_finish(self, user_input=None):
        return await self._section("finish", user_input)

    async def async_step_features(self, user_input=None):
        if not self._guided:
            return self.async_show_menu(step_id="features", menu_options=["ev", "balancing", "init"])
        if user_input is not None:
            self._use_balancing = user_input.get("configure_balancing", False)
            if not self._use_balancing:
                self._settings["input_number.opti_balancing_intervall_tage"] = 0
                self._settings["input_boolean.opti_balancing_netzladen"] = False
                self._draft["sources"].pop("cell_spread", None)
            if user_input.get("configure_ev", False):
                return await self.async_step_ev()
            return await (self.async_step_balancing() if self._use_balancing else self.async_step_notifications())
        return self.async_show_form(step_id="features", data_schema=vol.Schema({
            vol.Required("configure_ev", default=False): BooleanSelector(),
            vol.Required("configure_balancing", default=self._use_balancing): BooleanSelector(),
        }))


class OptiAkkuConfigFlow(WizardSections, ConfigFlow, domain=DOMAIN):
    """Guided, read-only-first onboarding."""

    VERSION = 1

    def __init__(self) -> None:
        self._connection = {}
        self._init_draft({})
        self._use_balancing = False
        self._migration_mapping = {}
        self._migration_values = {}
        self._migration_report = {}

    async def async_step_user(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({
                vol.Required("backend", default=BACKEND_SMA): SelectSelector(
                    SelectSelectorConfig(
                        options=[BACKEND_SMA, BACKEND_HUAWEI], translation_key="backend"
                    )
                ),
            }))
        if CONF_HOST not in user_input:
            if user_input.get("backend") == BACKEND_HUAWEI:
                return await self.async_step_huawei_device()
            return await self.async_step_sma_connection()
        return await self._async_sma_connection(user_input, "user")

    async def async_step_sma_connection(self, user_input=None):
        if user_input is None:
            return self.async_show_form(
                step_id="sma_connection",
                data_schema=_connection_schema({}, initial=True),
            )
        return await self._async_sma_connection(user_input, "sma_connection")

    async def _async_sma_connection(self, user_input, step_id):
        errors = {}
        connection = {CONF_HOST: user_input[CONF_HOST].strip(), CONF_PORT: user_input[CONF_PORT],
                          CONF_UNIT_ID: user_input[CONF_UNIT_ID], CONF_PROFILE: user_input.get(CONF_PROFILE, PROFILE_SMA_STP_SE),
                          "backend": BACKEND_SMA,
                          "shadow_mode": bool(user_input.get("migrate_legacy")) or user_input.get("shadow_mode", True)}
        try:
            probe = await _probe(self.hass, connection)
            if not probe or not probe.get("inverter_status"):
                raise UnsupportedDeviceError("Unsupported device")
        except (ModbusError, HomeAssistantError, OSError, TimeoutError):
            errors["base"] = "cannot_connect"
        except (UnsupportedDeviceError, ValueError):
            errors["base"] = "unsupported_device"
        else:
            connection["serial_number"] = probe.get("serial_number")
            await self.async_set_unique_id(_device_unique_id(connection, probe))
            self._abort_if_unique_id_configured()
            self._connection = connection
            self._draft["sources"] = {key: definition[0] for key, definition in SOURCE_DEFINITIONS.items()
                                      if self.hass.states.get(definition[0]) is not None}
            price = self.hass.states.get(self._draft["sources"].get("price_current", ""))
            if price and (unit := normalize_price_unit(price.attributes.get("unit_of_measurement"))):
                self._draft["price_unit"] = unit
            if user_input.get("migrate_legacy"):
                self._draft["plant_mode"] = "legacy"
                self._migration_mapping = {key: key for key in MIGRATABLE if self.hass.states.get(key)}
                self._capture_migration()
                return await self.async_step_migration_review()
            self._draft["plant_mode"] = "balance"
            self._draft["sources"].pop("house_consumption", None)
            self._draft["sources"].pop("pv_power", None)
            return await self.async_step_sources()
        return self.async_show_form(step_id=step_id,
            data_schema=_connection_schema(user_input, initial=True), errors=errors)

    async def async_step_huawei_device(self, user_input=None):
        errors = {}
        if user_input is not None:
            device_id = user_input["huawei_device_id"]
            entry_id = _huawei_entry_for_device(self.hass, device_id)
            if entry_id is None:
                errors["base"] = "huawei_device_unavailable"
            else:
                self._huawei_shadow = user_input.get("shadow_mode", True)
                self._huawei_device_id = device_id
                self._huawei_entry_id = entry_id
                return await self.async_step_huawei_sources()
        return self.async_show_form(step_id="huawei_device", errors=errors, data_schema=vol.Schema({
            vol.Required("huawei_device_id"): DeviceSelector(
                DeviceSelectorConfig(integration=BACKEND_HUAWEI)
            ),
            vol.Required("shadow_mode", default=True): BooleanSelector(),
        }))

    async def async_step_huawei_sources(self, user_input=None):
        from .huawei import HuaweiConfigurationError, HuaweiDevice

        errors = {}
        entities = _huawei_entities(self.hass, self._huawei_device_id, self._huawei_entry_id)
        if user_input is not None:
            sources = {role: user_input[role] for role in HUAWEI_REQUIRED_ROLES}
            sources.update({role: user_input[role] for role in HUAWEI_OPTIONAL_ROLES if user_input.get(role)})
            connection = {
                "backend": BACKEND_HUAWEI,
                "huawei_entry_id": self._huawei_entry_id,
                "huawei_device_id": self._huawei_device_id,
                "huawei_sources": sources,
                "grid_positive": user_input["grid_positive"],
                CONF_PROFILE: BACKEND_HUAWEI,
                "shadow_mode": getattr(self, "_huawei_shadow", True),
            }
            try:
                device = HuaweiDevice(self.hass, entry_id=self._huawei_entry_id,
                    device_id=self._huawei_device_id, sources=sources,
                    grid_positive=user_input["grid_positive"])
                probe = await device.async_probe()
                await device.async_read()
            except HuaweiConfigurationError:
                errors["base"] = "huawei_device_unavailable"
            else:
                if device.last_read_errors.keys() & set(HUAWEI_REQUIRED_ROLES):
                    errors["base"] = "huawei_required_data"
                elif not connection["shadow_mode"] and not self._huawei_temperature_valid(connection):
                    errors["base"] = "huawei_temperature_required"
                else:
                    if probe.get("serial_number"):
                        connection["serial_number"] = probe["serial_number"]
                    await self.async_set_unique_id(_device_unique_id(connection, probe))
                    self._abort_if_unique_id_configured()
                    self._connection = connection
                    if not connection["shadow_mode"]:
                        return await self.async_step_huawei_controls()
                    return await self.async_step_sources()
        schema = {}
        for role in HUAWEI_REQUIRED_ROLES:
            choices = _huawei_entities(self.hass, self._huawei_device_id, self._huawei_entry_id, role=role)
            schema[vol.Required(role)] = EntitySelector(EntitySelectorConfig(include_entities=choices))
        for role in HUAWEI_OPTIONAL_ROLES:
            marker = vol.Required(role) if role == "battery_temp" and not getattr(self, "_huawei_shadow", True) else vol.Optional(role)
            schema[marker] = EntitySelector(EntitySelectorConfig(include_entities=entities))
        schema[vol.Required("grid_positive", default="export")] = SelectSelector(
            SelectSelectorConfig(options=["export", "import"], translation_key="grid_positive")
        )
        return self.async_show_form(step_id="huawei_sources", errors=errors, data_schema=vol.Schema(schema))

    def _capture_migration(self):
        self._migration_values, self._migration_report = snapshot(self.hass.states, self._migration_mapping)
        self._migration_at = dt_util.utcnow().isoformat()

    async def async_step_migration_mapping(self, user_input=None):
        if user_input is not None:
            self._migration_mapping = {key: user_input[key.split(".", 1)[1]] for key in MIGRATABLE
                                       if user_input.get(key.split(".", 1)[1])}
            self._capture_migration()
            return await self.async_step_migration_review()
        schema = {}
        for key in MIGRATABLE:
            name = key.split(".", 1)[1]
            marker = (vol.Optional(name, description={"suggested_value": self._migration_mapping[key]})
                      if key in self._migration_mapping else vol.Optional(name))
            schema[marker] = EntitySelector(EntitySelectorConfig(domain=key.split(".", 1)[0]))
        return self.async_show_form(step_id="migration_mapping", data_schema=vol.Schema(schema))

    async def async_step_migration_review(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("adjust_mapping"):
                return await self.async_step_migration_mapping()
            if user_input.get("accept") is True:
                candidate = {**self._settings, **self._migration_values}
                try:
                    for key, value in candidate.items():
                        validate_setting(key, value, candidate)
                except (HomeAssistantError, ValueError, TypeError, KeyError):
                    errors["base"] = "invalid_limits"
                else:
                    self._settings = candidate
                    self._use_balancing = self._migration_values.get("input_number.opti_balancing_intervall_tage", 0) > 0
                    self._draft["migration"] = {"captured_at": self._migration_at,
                        "settings": dict(self._migration_values), "report": deepcopy(self._migration_report)}
                    if self.hass.states.get("input_select.akkusteuerung_modus"):
                        self._draft["shadow_reference_mode"] = "input_select.akkusteuerung_modus"
                    return await self.async_step_sources()
            else:
                errors["base"] = "migration_confirmation_required"
        return self.async_show_form(step_id="migration_review", data_schema=vol.Schema({
            vol.Required("accept", default=False): BooleanSelector(),
            vol.Required("adjust_mapping", default=False): BooleanSelector(),
        }), errors=errors, description_placeholders={"captured_at": self._migration_at,
            "report": describe(self._migration_values, self._migration_report)})

    async def _finish(self):
        if not await self._validate_final_sources(self._settings):
            return self.async_show_form(step_id="finish", data_schema=self._schema("finish"),
                errors={"base": self._huawei_source_error() or "sources_changed"}, description_placeholders=self._summary())
        self._draft["settings"] = dict(self._settings)
        self._draft["settings_revision"] = uuid4().hex
        return self.async_create_entry(title="Opti Akku Shadow" if self._connection.get("shadow_mode") else "Opti Akku",
                                       data=self._connection, options=self._draft)

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptiAkkuOptionsFlow(config_entry)


class OptiAkkuOptionsFlow(WizardSections, OptionsFlow):
    """Section menu; preserve current settings and entry identity until Save."""

    def __init__(self, config_entry):
        self._entry = config_entry
        self._original_options = deepcopy(dict(config_entry.options))
        self._original_data = dict(config_entry.data)
        self._connection = dict(config_entry.data)
        self._unique_id = config_entry.unique_id
        runtime = getattr(config_entry, "runtime_data", None)
        self._init_draft(dict(config_entry.options), getattr(runtime, "settings", None))
        self._guided = False
        self._use_balancing = False
        self._settings_loaded = False
        self._had_pending_settings = False
        if runtime is not None:
            self._merge_pending_settings(getattr(runtime, "_settings_revision", None))

    def _merge_pending_settings(self, applied_revision):
        self._initial_settings = dict(self._settings)
        self._had_pending_settings = bool(self._draft.get("settings_revision")
            and self._draft["settings_revision"] != applied_revision)
        if self._had_pending_settings:
            self._settings.update({k: v for k, v in self._draft.get("settings", {}).items() if k in DEFINITIONS})
        self._edited_settings = {key for key in self._settings if self._settings[key] != self._initial_settings[key]}
        self._settings_dirty = bool(self._edited_settings)

    async def async_step_init(self, user_input=None):
        if not self._settings_loaded:
            self._settings_loaded = True
            if getattr(self._entry, "runtime_data", None) is None:
                from homeassistant.helpers.storage import Store
                stored = await Store(self.hass, 1, f"{DOMAIN}.{self._entry.entry_id}", private=True).async_load() or {}
                self._settings = {key: definition["default"] for key, definition in DEFINITIONS.items()}
                self._settings.update({k: v for k, v in stored.get("settings", {}).items() if k in DEFINITIONS})
                self._merge_pending_settings(stored.get("settings_revision"))
        return self.async_show_menu(step_id="init", menu_options=["connection", "sources", "battery", "tariff", "forecast", "features", "notifications", "demand", "ev_preparation", "observation", "advanced", "finish"])

    async def async_step_connection(self, user_input=None):
        if self._connection.get("backend") == BACKEND_HUAWEI:
            return await self.async_step_huawei_connection(user_input)
        errors = {}
        if user_input is not None:
            connection = {"backend": BACKEND_SMA, CONF_HOST: user_input[CONF_HOST].strip(), CONF_PORT: user_input[CONF_PORT],
                          CONF_UNIT_ID: user_input[CONF_UNIT_ID], CONF_PROFILE: user_input.get(CONF_PROFILE, PROFILE_SMA_STP_SE),
                          "shadow_mode": self._entry.data.get("shadow_mode", False)}
            try:
                probe = await _probe(self.hass, connection)
                if not probe or not probe.get("inverter_status"):
                    raise UnsupportedDeviceError("Unsupported device")
            except (ModbusError, HomeAssistantError, OSError, TimeoutError):
                errors["base"] = "cannot_connect"
            except (UnsupportedDeviceError, ValueError):
                errors["base"] = "unsupported_device"
            else:
                connection["serial_number"] = probe.get("serial_number")
                unique_id = _device_unique_id(connection, probe)
                if any(e.entry_id != self._entry.entry_id and e.unique_id == unique_id for e in self.hass.config_entries.async_entries(DOMAIN)):
                    errors["base"] = "already_configured"
                else:
                    if connection != self._connection:
                        self._draft["single_writer_confirmed"] = False
                    self._connection, self._unique_id = connection, unique_id
                    return await self.async_step_init()
        return self.async_show_form(step_id="connection", data_schema=_connection_schema(user_input or self._connection), errors=errors)

    async def async_step_huawei_connection(self, user_input=None):
        from .huawei import HuaweiConfigurationError, HuaweiDevice

        errors = {}
        defaults = self._connection.get("huawei_sources", {})
        if user_input is not None:
            device_id = user_input["huawei_device_id"]
            entry_id = _huawei_entry_for_device(self.hass, device_id)
            sources = {role: user_input[role] for role in HUAWEI_REQUIRED_ROLES}
            sources.update({role: user_input[role] for role in HUAWEI_OPTIONAL_ROLES if user_input.get(role)})
            if entry_id is None:
                errors["base"] = "huawei_device_unavailable"
            else:
                try:
                    device = HuaweiDevice(self.hass, entry_id=entry_id, device_id=device_id,
                        sources=sources, grid_positive=user_input["grid_positive"])
                    probe = await device.async_probe()
                    await device.async_read()
                except HuaweiConfigurationError:
                    errors["base"] = "huawei_device_unavailable"
                else:
                    if device.last_read_errors.keys() & set(HUAWEI_REQUIRED_ROLES):
                        errors["base"] = "huawei_required_data"
                    elif (not self._entry.data.get("shadow_mode", True) or self._draft.get("strategy_enabled", True)) and not self._huawei_temperature_valid({"huawei_sources": sources}):
                        errors["base"] = "huawei_temperature_required"
                    else:
                        connection = {"backend": BACKEND_HUAWEI,
                            "huawei_entry_id": entry_id, "huawei_device_id": device_id,
                            "huawei_sources": sources, "grid_positive": user_input["grid_positive"],
                            CONF_PROFILE: BACKEND_HUAWEI, "shadow_mode": self._entry.data.get("shadow_mode", True)}
                        if device_id == self._connection.get("huawei_device_id"):
                            for key in ("huawei_controls", "huawei_grid_surplus"):
                                if key in self._connection:
                                    connection[key] = self._connection[key]
                        if probe.get("serial_number"):
                            connection["serial_number"] = probe["serial_number"]
                        unique_id = _device_unique_id(connection, probe)
                        if any(e.entry_id != self._entry.entry_id and e.unique_id == unique_id
                               for e in self.hass.config_entries.async_entries(DOMAIN)):
                            errors["base"] = "already_configured"
                        else:
                            self._connection, self._unique_id = connection, unique_id
                            self._draft["single_writer_confirmed"] = False
                            if not connection["shadow_mode"]:
                                return await self.async_step_huawei_controls()
                            return await self.async_step_init()
        schema = {
            vol.Required("huawei_device_id", default=self._connection["huawei_device_id"]):
                DeviceSelector(DeviceSelectorConfig(integration=BACKEND_HUAWEI)),
        }
        for role in HUAWEI_REQUIRED_ROLES:
            schema[vol.Required(role, default=defaults.get(role, ""))] = EntitySelector(
                EntitySelectorConfig(integration=BACKEND_HUAWEI, domain="sensor"))
        for role in HUAWEI_OPTIONAL_ROLES:
            marker = vol.Optional(role, description={"suggested_value": defaults[role]}) if role in defaults else vol.Optional(role)
            schema[marker] = EntitySelector(EntitySelectorConfig(integration=BACKEND_HUAWEI, domain="sensor"))
        schema[vol.Required("grid_positive", default=self._connection.get("grid_positive", "export"))] = SelectSelector(
            SelectSelectorConfig(options=["export", "import"], translation_key="grid_positive"))
        return self.async_show_form(step_id="connection", errors=errors, data_schema=vol.Schema(schema))

    async def _finish(self):
        if dict(self._entry.options) != self._original_options or dict(self._entry.data) != self._original_data:
            return self.async_show_form(step_id="finish", data_schema=self._schema("finish"),
                errors={"base": "configuration_changed"}, description_placeholders=self._summary())
        runtime = getattr(self._entry, "runtime_data", None)
        if (self._original_options.get("strategy_enabled", True)
                != self._draft.get("strategy_enabled", True)
                and getattr(runtime, "write_enabled", False)):
            return self.async_show_form(step_id="finish", data_schema=self._schema("finish"),
                errors={"base": "stop_writes_before_strategy_change"},
                description_placeholders=self._summary())
        final_settings = dict(getattr(runtime, "settings", self._settings))
        final_settings.update({key: self._settings[key] for key in self._edited_settings})
        sources = self._draft["sources"]
        if final_settings.get("input_boolean.opti_ev_akku_pause") and not any(
            sources.get(f"ev{n}_mode") and sources.get(f"ev{n}_charging") for n in (1, 2)
        ):
            return self.async_show_form(step_id="finish", data_schema=self._schema("finish"),
                errors={"base": "ev_pair_required"}, description_placeholders=self._summary())
        if not await self._validate_final_sources(final_settings):
            return self.async_show_form(step_id="finish", data_schema=self._schema("finish"),
                errors={"base": self._huawei_source_error() or "sources_changed"}, description_placeholders=self._summary())
        if dict(self._entry.options) != self._original_options or dict(self._entry.data) != self._original_data:
            return self.async_show_form(step_id="finish", data_schema=self._schema("finish"),
                errors={"base": "configuration_changed"}, description_placeholders=self._summary())
        runtime = getattr(self._entry, "runtime_data", None)
        if (self._original_options.get("strategy_enabled", True)
                != self._draft.get("strategy_enabled", True)
                and getattr(runtime, "write_enabled", False)):
            return self.async_show_form(step_id="finish", data_schema=self._schema("finish"),
                errors={"base": "stop_writes_before_strategy_change"},
                description_placeholders=self._summary())
        if self._settings_dirty or self._had_pending_settings:
            runtime = getattr(self._entry, "runtime_data", None)
            current = dict(getattr(runtime, "settings", self._settings))
            current.update({key: self._settings[key] for key in self._edited_settings})
            try:
                current = {key: validate_setting(key, value, current) for key, value in current.items()}
            except (HomeAssistantError, ValueError, TypeError, KeyError):
                return self.async_show_form(step_id="finish", data_schema=self._schema("finish"),
                    errors={"base": "invalid_limits"}, description_placeholders=self._summary())
            self._draft["settings"] = {key: current[key] for key in self._edited_settings}
            self._draft["settings_revision"] = uuid4().hex
        # Single update callback observes both the final data and options.
        if self._connection != dict(self._entry.data):
            self._draft["single_writer_confirmed"] = False
        self.hass.config_entries.async_update_entry(self._entry, data=self._connection,
            unique_id=self._unique_id, options=self._draft)
        return self.async_create_entry(title="", data=self._draft)
