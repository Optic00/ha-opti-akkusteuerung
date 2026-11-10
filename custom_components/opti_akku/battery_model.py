"""One battery model: efficiencies and cycle costs for every calculation.

Pure functions and an immutable value, no Home Assistant imports. Without an
enabled arbitrage configuration the documented defaults apply; an enabled but
invalid configuration also falls back to the defaults and marks the economics
as invalid.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any, Literal

FIELDS = (
    "battery_price_eur",
    "degradation_percent",
    "cycles",
    "usable_capacity_kwh",
    "charge_efficiency_percent",
    "discharge_efficiency_percent",
    "margin_ct",
)

_RANGES = {
    "battery_price_eur": (0, 100_000),
    "degradation_percent": (0, 100),
    "cycles": (1, 100_000),
    "usable_capacity_kwh": (0.1, 1_000),
    "charge_efficiency_percent": (1, 100),
    "discharge_efficiency_percent": (1, 100),
    "margin_ct": (0, 100),
}

DEFAULT_CHARGE_EFFICIENCY = 0.9
DEFAULT_DISCHARGE_EFFICIENCY = 0.9
DEFAULT_THROUGHPUT_COST_CT_KWH = 1.5
DEFAULT_MARGIN_CT = 0.0
ENGINE_ENTITY = "sensor.opti_battery_model"

Source = Literal["configured", "default", "invalid"]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def invalid_arbitrage_fields(config: Any) -> set[str]:
    """Return missing or invalid assumption names without filling defaults."""
    if not isinstance(config, Mapping) or config.get("enabled") is not True:
        return set(FIELDS)
    values = {key: _number(config.get(key)) for key in FIELDS}
    invalid = {key for key, value in values.items() if value is None}
    invalid.update(
        key
        for key, value in values.items()
        if value is not None and not _RANGES[key][0] <= value <= _RANGES[key][1]
    )
    return invalid


def throughput_cost_ct_kwh(
    battery_price_eur: float, degradation_percent: float, cycles: float, usable_capacity_kwh: float
) -> float:
    """Value loss per kWh of battery-side throughput (charging or discharging), in ct."""
    degradation_value_eur = battery_price_eur * degradation_percent / 100
    return degradation_value_eur / (2 * cycles * usable_capacity_kwh) * 100


@dataclass(frozen=True)
class BatteryModel:
    charge_efficiency: float  # AC -> stored, (0, 1]
    discharge_efficiency: float  # stored -> AC, (0, 1]
    throughput_cost_ct_kwh: float  # per kWh battery-side throughput, >= 0
    margin_ct: float  # battery-side margin, >= 0
    source: Source

    @classmethod
    def default(cls, source: Source = "default") -> BatteryModel:
        return cls(
            DEFAULT_CHARGE_EFFICIENCY,
            DEFAULT_DISCHARGE_EFFICIENCY,
            DEFAULT_THROUGHPUT_COST_CT_KWH,
            DEFAULT_MARGIN_CT,
            source,
        )

    @classmethod
    def from_options(cls, arbitrage_cfg: Any) -> BatteryModel:
        """Build the model from the arbitrage options; never raises."""
        if not isinstance(arbitrage_cfg, Mapping) or arbitrage_cfg.get("enabled") is not True:
            return cls.default()
        if invalid_arbitrage_fields(arbitrage_cfg):
            return cls.default("invalid")
        values = {key: float(arbitrage_cfg[key]) for key in FIELDS}
        return cls(
            charge_efficiency=values["charge_efficiency_percent"] / 100,
            discharge_efficiency=values["discharge_efficiency_percent"] / 100,
            throughput_cost_ct_kwh=throughput_cost_ct_kwh(
                values["battery_price_eur"],
                values["degradation_percent"],
                values["cycles"],
                values["usable_capacity_kwh"],
            ),
            margin_ct=values["margin_ct"],
            source="configured",
        )

    @property
    def cycle_cost_floor_ct_kwh(self) -> float:
        """Cost per stored and again withdrawn kWh."""
        return 2 * self.throughput_cost_ct_kwh + self.margin_ct

    @property
    def roundtrip_efficiency(self) -> float:
        return self.charge_efficiency * self.discharge_efficiency

    @property
    def wear_ct_per_discharged_ac_kwh(self) -> float:
        """Wear in the planner's unit: ct per discharged AC kWh."""
        return 2 * self.throughput_cost_ct_kwh / self.discharge_efficiency

    @property
    def economics_valid(self) -> bool:
        return self.source != "invalid"

    def minimum_high_price(self, low_price_ct: float) -> float:
        """Lowest discharge price that covers charging at ``low_price_ct``."""
        return (
            low_price_ct / self.charge_efficiency + self.cycle_cost_floor_ct_kwh
        ) / self.discharge_efficiency

    @property
    def precharge_price_factor(self) -> float:
        """Factor on the charge price in ``minimum_high_price`` (>= 1)."""
        return 1 / (self.charge_efficiency * self.discharge_efficiency)

    @property
    def precharge_price_offset_ct(self) -> float:
        """Offset in ``minimum_high_price``: cycle cost floor after discharge losses."""
        return self.cycle_cost_floor_ct_kwh / self.discharge_efficiency

    def engine_state(self) -> tuple[str, dict[str, Any]]:
        """State and attributes of ``sensor.opti_battery_model``."""
        return self.source, {
            "charge_efficiency": self.charge_efficiency,
            "discharge_efficiency": self.discharge_efficiency,
            "roundtrip_efficiency": self.roundtrip_efficiency,
            "throughput_cost_ct_kwh": round(self.throughput_cost_ct_kwh, 3),
            "cycle_cost_floor_ct_kwh": round(self.cycle_cost_floor_ct_kwh, 3),
            "margin_ct": round(self.margin_ct, 3),
            "source": self.source,
            # Unrounded: the precharge condition computes cur * factor + offset.
            "precharge_price_factor": self.precharge_price_factor,
            "precharge_price_offset_ct": self.precharge_price_offset_ct,
        }


def template_discharge_efficiency(value: Any) -> float:
    """The discharge efficiency the reserve template uses for ``value``.

    Same rule as the template fallback: a number in (0, 1], otherwise the
    default. The reserve never disappears because the model is missing.
    """
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_DISCHARGE_EFFICIENCY
    return result if isfinite(result) and 0 < result <= 1 else DEFAULT_DISCHARGE_EFFICIENCY
