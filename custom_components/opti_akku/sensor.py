"""Dynamic virtual and diagnostic sensors."""

from __future__ import annotations

from math import isfinite
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import MATCH_ALL
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.entity import EntityCategory

from .entity import OptiAkkuEntity, coordinator_data, readable_name

SENSOR_PREFIXES = ("sensor.", "counter.", "input_datetime.")
DIAGNOSTICS = ("mode", "reason", "last_write", "last_error", "source_errors", "price_provider_error", "price_last_success", "notification_error", "load_profile", "control_release", "device_errors", "command_confirmation", "command_evidence", "pause_pending", "reserve_plan", "operating_report", "price_status", "demand_forecast", "source_observation", "connection_status", "ev_preparation", "arbitrage_estimate")
CORE_UNITS = {"soc": "%", "battery_temp": "°C", "battery_capacity_kwh": "kWh",
              "battery_power_w": "W", "pv_power_w": "W", "pv_generation_w": "W",
              "grid_import_w": "W", "grid_export_w": "W", "house_consumption_w": "W",
              "target_soc": "%", "charge_power_w": "W", "price_current_ct_kwh": "ct/kWh"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new_entities() -> None:
        states = (coordinator.data or {}).get("states", {})
        keys = [key for key in states if key.startswith(SENSOR_PREFIXES) and key not in known]
        if keys:
            known.update(keys)
            async_add_entities(OptiAkkuStateSensor(entry, key) for key in keys)

    add_new_entities()
    def diagnostic_entity(key: str) -> SensorEntity:
        if key == "arbitrage_estimate":
            return OptiAkkuArbitrageSensor(entry, key)
        if key in ("command_evidence", "reserve_plan", "operating_report", "demand_forecast", "source_observation", "connection_status", "ev_preparation"):
            return OptiAkkuReportSensor(entry, key)
        return OptiAkkuDiagnosticSensor(entry, key)

    async_add_entities(diagnostic_entity(key) for key in DIAGNOSTICS)
    if getattr(coordinator.device, "supports_write_value_evidence", False) is True:
        async_add_entities([OptiAkkuReportSensor(entry, "last_write_values")])
    if coordinator.shadow_mode:
        async_add_entities([OptiAkkuDiagnosticSensor(entry, "shadow_status")])
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class OptiAkkuStateSensor(OptiAkkuEntity, SensorEntity):
    def __init__(self, entry: ConfigEntry, key: str) -> None:
        super().__init__(entry, key)
        core_key = key.removeprefix("sensor.opti_")
        if core_key not in CORE_UNITS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False
        meta = (entry.runtime_data.data or {}).get("metadata", {}).get(key, {})
        if core_key not in CORE_UNITS:
            self._attr_name = meta.get("name", readable_name(key))
        self._attr_icon = meta.get("icon")
        self._attr_native_unit_of_measurement = meta.get("unit") or meta.get("unit_of_measurement")
        if core_key in CORE_UNITS:
            self._attr_translation_key = "value_" + core_key
            self._attr_native_unit_of_measurement = CORE_UNITS[core_key]
        try:
            self._attr_device_class = SensorDeviceClass(meta["device_class"])
        except (KeyError, ValueError):
            self._attr_device_class = None
        try:
            self._attr_state_class = SensorStateClass(meta["state_class"])
        except (KeyError, ValueError):
            self._attr_state_class = None
        self._numeric = bool(
            self._attr_native_unit_of_measurement
            or self._attr_device_class
            or self._attr_state_class
        )

    def _value(self) -> Any:
        value = coordinator_data(self).get("states", {}).get(self._key)
        if value is None or (isinstance(value, str) and value.lower() in {"unknown", "unavailable"}):
            return None
        if self._numeric:
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            return value if isfinite(value) else None
        return value

    @property
    def available(self) -> bool:
        return super().available and self._value() is not None

    @property
    def native_value(self) -> Any:
        return self._value()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return coordinator_data(self).get("attributes", {}).get(self._key)


class OptiAkkuDiagnosticSensor(OptiAkkuEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        super().__init__(entry, f"diagnostic_{key}")
        self._data_key = key
        self._attr_translation_key = key
        if key == "reserve_plan":
            self._attr_entity_category = None
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["planned", "disabled", "no_valid_plan", "observation", "manual", "no_peak", "unconfirmed", "hold_requested", "discharge_requested", "charge_requested"]
        if key == "operating_report":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["collecting", "data_missing", "error"]
        if key == "demand_forecast":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["disabled", "learning", "ready", "data_missing", "no_pv_timing", "error"]
        if key == "ev_preparation":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["disabled", "car_charging", "data_missing", "away", "target_reached", "no_demand", "waiting_surplus", "ready", "preparing", "higher_priority"]
        if key == "source_observation":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["disabled", "ready", "warming_up", "data_missing", "error"]
        if key == "connection_status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["offline", "recovering", "not_ready", "ready"]
        if key == "price_status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["loading", "ready", "error", "not_used"]
        if key == "command_confirmation":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["pending", "idle_or_confirmed", "waiting_ready"]
        if key == "command_evidence":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["observation", "not_attempted", "waiting_ready", "pending", "completed", "failed", "superseded"]
        if key == "pause_pending":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["pending", "clear"]
        if key == "control_release":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["not_supported", "not_confirmed"]
        if key == "load_profile":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["legacy", "warming_up", "ready", "missing"]
        if key in ("last_write", "price_last_success"):
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> Any:
        value = coordinator_data(self).get(self._data_key)
        if self._data_key == "last_write_values":
            return value.get("summary") if isinstance(value, dict) else None
        if self._data_key in ("command_evidence", "reserve_plan", "operating_report", "demand_forecast", "source_observation", "connection_status", "ev_preparation"):
            return value.get("status") if isinstance(value, dict) else None
        if self._data_key == "pause_pending":
            return "pending" if value else "clear"
        if self._data_key == "load_profile":
            if not value:
                return "legacy"
            if not value.get("valid", True):
                return "missing"
            return "warming_up" if value.get("warming_up") else "ready"
        if isinstance(value, (dict, list, tuple, set)):
            return len(value)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._data_key == "shadow_status":
            return coordinator_data(self).get("shadow_summary", {})
        value = coordinator_data(self).get(self._data_key)
        if self._data_key == "last_write_values" and isinstance(value, dict):
            return {key: item for key, item in value.items() if key != "summary"}
        if self._data_key in ("command_evidence", "reserve_plan", "operating_report", "demand_forecast", "source_observation", "connection_status", "ev_preparation") and isinstance(value, dict):
            return {key: item for key, item in value.items() if key != "status"}
        return {"details": value} if isinstance(value, (dict, list, tuple, set)) else None


class OptiAkkuReportSensor(OptiAkkuDiagnosticSensor):
    """Keep detailed live attributes out of Recorder."""

    _unrecorded_attributes = frozenset({MATCH_ALL})


class OptiAkkuArbitrageSensor(OptiAkkuReportSensor):
    """Price spread with optional residual-value hold details."""

    _attr_native_unit_of_measurement = "ct/kWh"

    @property
    def native_value(self) -> float | None:
        value = coordinator_data(self).get(self._data_key)
        if not isinstance(value, dict):
            return None
        candidate = value.get("minimum_spread_ct_kwh")
        return candidate if isinstance(candidate, int | float) and isfinite(candidate) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        value = coordinator_data(self).get(self._data_key)
        if not isinstance(value, dict):
            return None
        return {key: item for key, item in value.items() if key != "minimum_spread_ct_kwh"}
