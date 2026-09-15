"""Bridge to entities owned by the wlcrs Huawei Solar integration."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower, UnitOfTemperature
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util


DOMAIN = "huawei_solar"
REQUIRED_ROLES = frozenset(
    {"soc", "battery_capacity_kwh", "battery_power_w", "pv_power_w", "grid_power_w"}
)
OPTIONAL_ROLES = frozenset({"battery_temp", "pv_generation_w", "inverter_status"})
ROLE_KEYS = {
    "soc": "sensor.opti_soc",
    "battery_capacity_kwh": "sensor.opti_battery_capacity_kwh",
    "battery_power_w": "sensor.opti_battery_power_w",
    "battery_temp": "sensor.opti_battery_temp",
    "pv_power_w": "sensor.opti_pv_power_w",
    "pv_generation_w": "sensor.opti_pv_generation_w",
}


class HuaweiConfigurationError(ValueError):
    """The selected Huawei entry, device, or entity topology is unsafe."""


class HuaweiDevice:
    """Read Huawei Solar states without opening another Modbus connection."""

    PHASED = True
    supported_modes: tuple[str, ...] = ()
    supports_control_release = False

    def __init__(
        self,
        hass: Any,
        *,
        entry_id: str,
        device_id: str,
        sources: dict[str, str],
        source_max_age: float = 900,
        grid_positive: str = "export",
        controls: dict[str, str] | None = None,
        read_only: bool = True,
        grid_charge_for_surplus: bool = False,
    ) -> None:
        if not isinstance(entry_id, str) or not entry_id:
            raise HuaweiConfigurationError("Huawei entry_id is required")
        if not isinstance(device_id, str) or not device_id:
            raise HuaweiConfigurationError("Huawei device_id is required")
        if not isinstance(sources, dict):
            raise HuaweiConfigurationError("Huawei sources must be a mapping")
        if grid_positive not in ("export", "import"):
            raise HuaweiConfigurationError("grid_positive must be export or import")
        if isinstance(source_max_age, bool):
            raise HuaweiConfigurationError("source_max_age must be finite and non-negative")
        try:
            max_age = float(source_max_age)
        except (TypeError, ValueError, OverflowError) as err:
            raise HuaweiConfigurationError(
                "source_max_age must be finite and non-negative"
            ) from err
        if not math.isfinite(max_age) or max_age < 0:
            raise HuaweiConfigurationError("source_max_age must be finite and non-negative")
        unknown = set(sources) - REQUIRED_ROLES - OPTIONAL_ROLES
        missing = REQUIRED_ROLES - set(sources)
        if unknown or missing:
            raise HuaweiConfigurationError(
                f"Huawei source roles invalid; missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        if any(not isinstance(value, str) or not value for value in sources.values()):
            raise HuaweiConfigurationError("Every Huawei source must be an entity ID")
        if len(set(sources.values())) != len(sources):
            raise HuaweiConfigurationError("Huawei source entities cannot serve multiple roles")
        self.hass = hass
        self.entry_id = entry_id
        self.device_id = device_id
        self.sources = dict(sources)
        self.source_max_age = max_age
        self.grid_positive = grid_positive
        self.last_read_errors: dict[str, str] = {}
        if not isinstance(read_only, bool) or not isinstance(grid_charge_for_surplus, bool):
            raise HuaweiConfigurationError("Huawei safety flags must be booleans")
        self.read_only = read_only
        self.grid_charge_for_surplus = grid_charge_for_surplus
        self.controller = None
        if controls:
            from .huawei_control import HuaweiController
            self.controller = HuaweiController(self, controls)
        if not read_only and self.controller is None:
            raise HuaweiConfigurationError("An active Huawei entry needs actuator bindings")
        if not read_only:
            from .sma import MODES
            self.supported_modes = MODES

    def _validated_device(self) -> Any:
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise HuaweiConfigurationError("Huawei Solar config entry is not loaded")
        registry = dr.async_get(self.hass)
        selected = registry.async_get(self.device_id)
        if selected is None or self.entry_id not in selected.config_entries:
            raise HuaweiConfigurationError("Selected Huawei device does not belong to the entry")
        entity_registry = er.async_get(self.hass)
        for role, entity_id in self.sources.items():
            entity = entity_registry.async_get(entity_id)
            if (
                entity is None
                or entity.platform != DOMAIN
                or entity.config_entry_id != self.entry_id
                or entity.device_id is None
            ):
                raise HuaweiConfigurationError(f"Invalid Huawei source for {role}")
            source_device = registry.async_get(entity.device_id)
            if source_device is None or not (
                source_device.id == selected.id or source_device.via_device_id == selected.id
                or (role == "grid_power_w" and selected.via_device_id is not None
                    and source_device.via_device_id == selected.via_device_id)
            ):
                raise HuaweiConfigurationError(f"Huawei source for {role} is outside the device")
        return selected

    async def async_probe(self) -> dict[str, Any]:
        """Validate HA registry identity without touching Huawei transport or services."""
        device = self._validated_device()
        if self.controller:
            self.controller.validate()
        return {
            "manufacturer": "Huawei",
            "model": device.model,
            "serial_number": device.serial_number,
            "backend": DOMAIN,
            "backend_health": True,
            # Compatibility health only; this is not a Huawei operating-state code.
            "inverter_status": True,
        }

    @staticmethod
    def _finite(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    def _value(
        self,
        role: str,
        now: datetime,
        units: dict[str, float],
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float | None:
        entity_id = self.sources.get(role)
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        reported = None if state is None else state.last_reported or state.last_updated
        if state is None or not isinstance(reported, datetime):
            self.last_read_errors[role] = "missing_or_stale"
            return None
        try:
            age = (now - reported).total_seconds()
        except (TypeError, ValueError):
            self.last_read_errors[role] = "missing_or_stale"
            return None
        if not -60 <= age <= self.source_max_age:
            self.last_read_errors[role] = "missing_or_stale"
            return None
        value = self._finite(state.state)
        if value is None:
            self.last_read_errors[role] = "invalid_value"
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit not in units:
            self.last_read_errors[role] = "unsupported_unit"
            return None
        value *= units[unit]
        if not math.isfinite(value) or minimum is not None and value < minimum or maximum is not None and value > maximum:
            self.last_read_errors[role] = "invalid_value"
            return None
        return value

    async def async_read(self) -> dict[str, Any]:
        """Read a fresh, fail-closed snapshot from selected Huawei entities."""
        self._validated_device()
        self.last_read_errors = {}
        now = dt_util.utcnow()
        values: dict[str, Any] = {
            "sensor.opti_inverter_status": "unavailable",
            **{key: None for key in ROLE_KEYS.values()},
            "sensor.opti_grid_import_w": None,
            "sensor.opti_grid_export_w": None,
        }
        values[ROLE_KEYS["soc"]] = self._value(
            "soc", now, {PERCENTAGE: 1}, minimum=0, maximum=100
        )
        values[ROLE_KEYS["battery_capacity_kwh"]] = self._value(
            "battery_capacity_kwh",
            now,
            {UnitOfEnergy.KILO_WATT_HOUR: 1, UnitOfEnergy.WATT_HOUR: 0.001},
            minimum=0.000001,
        )
        power_units = {UnitOfPower.WATT: 1, UnitOfPower.KILO_WATT: 1000}
        for role in ("battery_power_w", "pv_power_w"):
            values[ROLE_KEYS[role]] = self._value(role, now, power_units)
        if "battery_temp" in self.sources:
            values[ROLE_KEYS["battery_temp"]] = self._value(
                "battery_temp",
                now,
                {UnitOfTemperature.CELSIUS: 1},
                minimum=-60,
                maximum=100,
            )
        if "pv_generation_w" in self.sources:
            values[ROLE_KEYS["pv_generation_w"]] = self._value(
                "pv_generation_w", now, power_units, minimum=0
            )
        grid = self._value("grid_power_w", now, power_units)
        if grid is not None:
            if self.grid_positive == "export":
                values["sensor.opti_grid_export_w"] = max(0.0, grid)
                values["sensor.opti_grid_import_w"] = max(0.0, -grid)
            else:
                values["sensor.opti_grid_import_w"] = max(0.0, grid)
                values["sensor.opti_grid_export_w"] = max(0.0, -grid)
        return values

    async def async_apply(self, *_args: Any, **_kwargs: Any) -> None:
        """Apply through the bound provider; shadow has an independent hard gate."""
        if self.read_only or self.controller is None:
            raise PermissionError("Huawei Solar entity backend is read-only")
        await self.controller.apply(*_args, **_kwargs)

    async def async_shutdown_control(self) -> None:
        """Finish a bounded pause even though the coordinator will no longer tick."""
        if self.read_only or self.controller is None:
            return
        async with self.controller._lock:
            clean = await self.controller._cleanup()
            self.controller._steps = []
            self.controller._pending = None
            self.controller.last_mode = None
            if not clean:
                from .huawei_control import HuaweiControlError
                raise HuaweiControlError("Shutdown pause not confirmed; device settings may persist")

    @property
    def last_write(self):
        return self.controller.last_write if self.controller else None
