"""Transparent battery arbitrage estimate without controller side effects."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any


FIELDS = (
    "battery_price_eur",
    "degradation_percent",
    "cycles",
    "usable_capacity_kwh",
    "charge_efficiency_percent",
    "discharge_efficiency_percent",
    "margin_ct",
)


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
    ranges = {
        "battery_price_eur": (0, 100_000),
        "degradation_percent": (0, 100),
        "cycles": (1, 100_000),
        "usable_capacity_kwh": (0.1, 1_000),
        "charge_efficiency_percent": (1, 100),
        "discharge_efficiency_percent": (1, 100),
        "margin_ct": (0, 100),
    }
    invalid.update(
        key
        for key, value in values.items()
        if value is not None and not ranges[key][0] <= value <= ranges[key][1]
    )
    return invalid


def build_arbitrage_estimate(
    config: Any,
    charge_price_ct_kwh: Any = None,
    *,
    strategy_enabled: bool = True,
) -> dict[str, Any]:
    """Calculate a display-only threshold from explicit user assumptions."""
    if not isinstance(config, Mapping) or config.get("enabled") is not True:
        return {
            "status": "not_configured",
            "informational_only": True,
            "controls_battery": False,
        }
    invalid = invalid_arbitrage_fields(config)
    if invalid:
        return {
            "status": "invalid",
            "invalid_fields": sorted(invalid),
            "informational_only": True,
            "controls_battery": False,
        }
    if not strategy_enabled:
        return {
            "status": "strategy_disabled",
            "informational_only": True,
            "controls_battery": False,
        }

    values = {key: float(config[key]) for key in FIELDS}
    charge_efficiency = values["charge_efficiency_percent"] / 100
    discharge_efficiency = values["discharge_efficiency_percent"] / 100
    degradation_value_eur = (
        values["battery_price_eur"] * values["degradation_percent"] / 100
    )
    throughput_cost_ct_kwh = (
        degradation_value_eur
        / (2 * values["cycles"] * values["usable_capacity_kwh"])
        * 100
    )
    cycle_cost_floor_ct_kwh = 2 * throughput_cost_ct_kwh + values["margin_ct"]
    charge_price = _number(charge_price_ct_kwh)
    minimum_high_price = None
    minimum_spread = None
    if charge_price is not None:
        minimum_high_price = (
            charge_price / charge_efficiency + cycle_cost_floor_ct_kwh
        ) / discharge_efficiency
        minimum_spread = minimum_high_price - charge_price

    return {
        "status": "ready" if charge_price is not None else "price_missing",
        "minimum_spread_ct_kwh": (
            round(minimum_spread, 3) if minimum_spread is not None else None
        ),
        "minimum_high_price_ct_kwh": (
            round(minimum_high_price, 3) if minimum_high_price is not None else None
        ),
        "charge_price_ct_kwh": charge_price,
        "throughput_cost_ct_kwh": round(throughput_cost_ct_kwh, 3),
        "cycle_cost_floor_ct_kwh": round(cycle_cost_floor_ct_kwh, 3),
        "degradation_value_eur": round(degradation_value_eur, 2),
        "battery_price_eur": values["battery_price_eur"],
        "degradation_percent": values["degradation_percent"],
        "cycles": values["cycles"],
        "usable_capacity_kwh": values["usable_capacity_kwh"],
        "charge_efficiency": charge_efficiency,
        "discharge_efficiency": discharge_efficiency,
        "margin_ct": values["margin_ct"],
        "formula": (
            "p_high_min = (p_low / eta_charge + "
            "2 * c_throughput + margin) / eta_discharge"
        ),
        "informational_only": True,
        "controls_battery": False,
    }
