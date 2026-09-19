"""Transparent battery arbitrage estimate without controller side effects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
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

HOLD_DECISIONS = frozenset({"above_target", "default", "peak_l1", "peak_l2"})
DISCHARGING_MODES = frozenset(
    {"Akku Automatisch", "Akku Dynamisch", "Akku nur Entladen", "Akku schnell Entladen"}
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _same_number(left: float | None, right: float | None, tolerance: float) -> bool:
    return left is not None and right is not None and abs(left - right) <= tolerance


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def _price_periods(series: Any, now: datetime, timezone: Any) -> list[tuple[datetime, datetime, float]]:
    """Rebuild the dated grid already validated by the source adapter."""
    if not isinstance(series, Mapping):
        return []
    local_now = now.astimezone(timezone)
    periods: list[tuple[datetime, datetime, float]] = []
    for offset, key in ((0, "today"), (1, "tomorrow")):
        values = series.get(key)
        if not isinstance(values, (list, tuple)) or not values:
            continue
        local_day = local_now.date() + timedelta(days=offset)
        start = datetime.combine(local_day, time.min, tzinfo=timezone).astimezone(UTC)
        end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=timezone).astimezone(UTC)
        slot_seconds = (end - start).total_seconds() / len(values)
        if slot_seconds not in (900, 3600):
            continue
        cursor = start
        for raw in values:
            price = _number(raw)
            if price is None:
                return []
            next_cursor = cursor + timedelta(seconds=slot_seconds)
            periods.append((cursor, next_cursor, price))
            cursor = next_cursor
    return periods


def build_terminal_value_proxy(
    *,
    forecast_slots: Any,
    price_series: Any,
    now: Any,
    timezone: Any,
    refill_from: Any,
    battery_capacity_kwh: Any,
    discharge_efficiency: float,
    throughput_cost_ct_kwh: float,
    current_soc: Any = None,
    minimum_soc: Any = None,
    maximum_soc: Any = None,
    profile_ready: bool = False,
) -> dict[str, Any]:
    """Value stored energy against forecast residual load, without controlling it.

    The curve mirrors EOS' useful terminal-value idea, but remains a bounded
    observation here: future residual-load slices are ordered by import price.
    The first available kWh therefore replaces the most expensive uncovered
    load, and additional energy becomes progressively less valuable.
    """
    base = {
        "terminal_value_status": "data_missing",
        "terminal_value_reason": "forecast_missing",
        "terminal_value_controls_battery": False,
        "terminal_value_method": "concave_residual_load_proxy",
    }
    issued = _instant(now)
    cutoff = _instant(refill_from)
    if issued is None or timezone is None or not isinstance(forecast_slots, list):
        return base
    if cutoff is None or cutoff <= issued:
        return {**base, "terminal_value_reason": "refill_boundary_missing"}
    periods = _price_periods(price_series, issued, timezone)
    if not periods:
        return {**base, "terminal_value_reason": "price_horizon_missing"}
    rated_capacity = _number(battery_capacity_kwh)
    soc, low, high = (_number(value) for value in (current_soc, minimum_soc, maximum_soc))
    if (
        rated_capacity is None or rated_capacity <= 0
        or low is None or high is None or not 0 <= low < high <= 100
    ):
        return {**base, "terminal_value_reason": "battery_capacity_invalid"}
    capacity_ac = rated_capacity * (high - low) / 100 * discharge_efficiency

    slices: list[tuple[float, float]] = []
    expected_hours = (cutoff - issued).total_seconds() / 3600
    forecast_hours = matched_hours = 0.0
    forecast_complete = len(forecast_slots) <= 100
    cursor = issued
    for row in forecast_slots[:100]:
        if not isinstance(row, Mapping):
            forecast_complete = False
            continue
        start, end = _instant(row.get("start")), _instant(row.get("end"))
        load_w, pv_w = _number(row.get("load_w")), _number(row.get("pv_p10_w"))
        if (
            start is None or end is None or end <= start or end <= issued
            or load_w is None or pv_w is None or load_w < 0 or pv_w < 0
        ):
            forecast_complete = False
            continue
        start, end = max(start, issued), min(end, cutoff)
        if end <= start:
            continue
        if abs((start - cursor).total_seconds()) > 1:
            forecast_complete = False
        cursor = max(cursor, end)
        forecast_hours += (end - start).total_seconds() / 3600
        residual_w = max(0.0, load_w - pv_w)
        for price_start, price_end, price in periods:
            overlap_start = max(start, price_start)
            overlap_end = min(end, price_end)
            if overlap_end <= overlap_start:
                continue
            hours = (overlap_end - overlap_start).total_seconds() / 3600
            matched_hours += hours
            residual_kwh = residual_w * hours / 1000
            if residual_kwh > 0:
                slices.append((price, residual_kwh))
        if end >= cutoff:
            break
    if forecast_hours <= 0:
        return base
    forecast_coverage = min(1.0, forecast_hours / expected_hours)
    price_coverage = min(1.0, matched_hours / expected_hours)
    forecast_complete = forecast_complete and cursor >= cutoff
    if matched_hours <= 0:
        return {**base, "terminal_value_reason": "price_horizon_missing"}

    cost_per_ac_kwh = throughput_cost_ct_kwh / discharge_efficiency
    valued = sorted(
        ((max(0.0, price - cost_per_ac_kwh), energy) for price, energy in slices),
        reverse=True,
    )

    def curve_value(energy_kwh: float) -> tuple[float, float]:
        remaining = max(0.0, min(capacity_ac, energy_kwh))
        value_ct = 0.0
        marginal = 0.0
        for price, energy in valued:
            if remaining <= 0:
                break
            used = min(remaining, energy)
            value_ct += used * price
            remaining -= used
            marginal = price
        if remaining > 1e-9:
            marginal = 0.0
        return value_ct / 100, marginal

    current_ac = (
        rated_capacity * min(high - low, max(0.0, soc - low)) / 100 * discharge_efficiency
        if soc is not None
        else None
    )
    full_value, full_marginal = curve_value(capacity_ac)
    current_value, current_marginal = (
        curve_value(current_ac) if current_ac is not None else (None, None)
    )
    positive = [(price, energy) for price, energy in valued if price > 0]
    complete = forecast_complete and forecast_coverage >= 0.999 and price_coverage >= 0.999
    status = "ready" if profile_ready and complete else "learning"
    reason = (
        "complete_profile_and_price_horizon"
        if status == "ready"
        else "profile_not_ready"
        if not profile_ready
        else "forecast_horizon_incomplete"
        if not forecast_complete or forecast_coverage < 0.999
        else "partial_price_horizon"
    )
    return {
        **base,
        "terminal_value_status": status,
        "terminal_value_reason": reason,
        "terminal_value_horizon_end": cutoff.isoformat(),
        "terminal_value_window_hours": round(expected_hours, 2),
        "terminal_value_forecast_coverage_percent": round(forecast_coverage * 100, 1),
        "terminal_value_price_coverage_percent": round(price_coverage * 100, 1),
        "terminal_value_residual_load_kwh": round(sum(energy for _, energy in slices), 3),
        "terminal_value_valued_load_kwh": round(sum(energy for _, energy in positive), 3),
        "terminal_value_ac_capacity_kwh": round(capacity_ac, 3),
        "terminal_value_curve_knee_kwh": round(
            min(capacity_ac, sum(energy for _, energy in positive)), 3
        ),
        "terminal_value_first_marginal_ct_kwh": round(positive[0][0], 3) if positive else 0.0,
        "terminal_value_full_marginal_ct_kwh": round(full_marginal, 3),
        "terminal_value_full_battery_eur": round(full_value, 3),
        "terminal_value_current_ac_kwh": round(current_ac, 3) if current_ac is not None else None,
        "terminal_value_current_battery_eur": (
            round(current_value, 3) if current_value is not None else None
        ),
        "terminal_value_current_marginal_ct_kwh": (
            round(current_marginal, 3) if current_marginal is not None else None
        ),
        "terminal_value_current_soc": round(soc, 3) if soc is not None else None,
        "terminal_value_battery_capacity_kwh": round(rated_capacity, 3),
        "terminal_value_minimum_soc": round(low, 3),
        "terminal_value_maximum_soc": round(high, 3),
        "terminal_value_issued_at": issued.isoformat(),
    }


def apply_discharge_hold(
    result: Any,
    report: Any,
    config: Any,
    current_price_ct_kwh: Any,
    now: Any,
) -> tuple[Any, dict[str, Any]]:
    """Hold stored energy when its forecast residual value is materially higher.

    The previous coordinator observation is intentionally used as an immutable
    input. It must be fresh and match the current assumptions and battery SOC;
    otherwise the existing strategy decision is retained.
    """
    out: dict[str, Any] = {
        "hold_enabled": isinstance(config, Mapping)
        and config.get("hold_enabled") is True,
        "hold_status": "disabled",
        "hold_controls_battery": False,
    }
    if not out["hold_enabled"]:
        return result, out
    if not isinstance(report, Mapping):
        return result, {**out, "hold_status": "waiting_for_fresh_report"}
    issued, observed_at = _instant(report.get("terminal_value_issued_at")), _instant(now)
    if (
        issued is None
        or observed_at is None
        or not 0 <= (observed_at - issued).total_seconds() <= 90
        or report.get("status") != "ready"
        or report.get("terminal_value_status") != "ready"
        or report.get("hold_enabled") is not True
    ):
        return result, {**out, "hold_status": "waiting_for_fresh_report"}
    if invalid_arbitrage_fields(config):
        return result, {**out, "hold_status": "invalid_assumptions"}
    expected = {
        "battery_price_eur": "battery_price_eur",
        "degradation_percent": "degradation_percent",
        "cycles": "cycles",
        "usable_capacity_kwh": "usable_capacity_kwh",
        "charge_efficiency_percent": "charge_efficiency",
        "discharge_efficiency_percent": "discharge_efficiency",
        "margin_ct": "margin_ct",
    }
    for config_key, report_key in expected.items():
        configured = _number(config.get(config_key))
        reported = _number(report.get(report_key))
        if config_key.endswith("_percent") and report_key.endswith("efficiency"):
            configured = configured / 100 if configured is not None else None
        if configured is None or reported is None or abs(configured - reported) > 1e-6:
            return result, {**out, "hold_status": "waiting_for_matching_report"}
    current_price = _number(current_price_ct_kwh)
    marginal = _number(report.get("terminal_value_current_marginal_ct_kwh"))
    throughput = _number(report.get("throughput_cost_ct_kwh"))
    efficiency = _number(report.get("discharge_efficiency"))
    reported_soc = _number(report.get("terminal_value_current_soc"))
    current_soc = _number(getattr(result, "states", {}).get("sensor.opti_soc"))
    current_capacity = _number(
        getattr(result, "states", {}).get("sensor.opti_battery_capacity_kwh")
    )
    current_minimum = _number(
        getattr(result, "states", {}).get("input_number.minsoc")
    )
    current_maximum = _number(
        getattr(result, "states", {}).get("input_number.maxsoc")
    )
    report_capacity = _number(report.get("terminal_value_battery_capacity_kwh"))
    report_minimum = _number(report.get("terminal_value_minimum_soc"))
    report_maximum = _number(report.get("terminal_value_maximum_soc"))
    margin = _number(config.get("margin_ct"))
    if (
        current_price is None
        or marginal is None
        or throughput is None
        or efficiency is None
        or efficiency <= 0
        or reported_soc is None
        or current_soc is None
        or abs(reported_soc - current_soc) > 2
        or not _same_number(current_capacity, report_capacity, 0.001)
        or not _same_number(current_minimum, report_minimum, 0.001)
        or not _same_number(current_maximum, report_maximum, 0.001)
        or margin is None
    ):
        return result, {**out, "hold_status": "data_missing"}
    current_net = max(0.0, current_price - throughput / efficiency)
    advantage = marginal - current_net
    previous_holding = report.get("hold_status") == "holding"
    threshold = max(0.0, margin + (-0.5 if previous_holding else 0.5))
    economical = marginal > 0 and advantage > threshold + 0.001
    out.update(
        hold_status="hold_candidate" if economical else "release",
        hold_current_net_ct_kwh=round(current_net, 3),
        hold_future_marginal_ct_kwh=round(marginal, 3),
        hold_advantage_ct_kwh=round(advantage, 3),
        hold_required_margin_ct_kwh=round(margin, 3),
        hold_switch_threshold_ct_kwh=round(threshold, 3),
    )
    if (
        not economical
        or result.decision_id not in HOLD_DECISIONS
        or result.mode not in DISCHARGING_MODES
    ):
        if economical:
            out["hold_status"] = "higher_priority"
        return result, out
    mode = "Akku nur Laden"
    reason = (
        "Restwert halten "
        f"({current_net:.1f} ct jetzt < {marginal:.1f} ct später, "
        f"Marge {margin:.1f} ct)"
    )
    values = dict(result.states)
    values["sensor.opti_strategie_vorschau"] = mode
    attrs = {
        **result.attributes,
        "sensor.opti_strategie_vorschau": {
            **result.attributes.get("sensor.opti_strategie_vorschau", {}),
            "grund": reason,
        },
    }
    return replace(
        result,
        mode=mode,
        reason=reason,
        states=values,
        attributes=attrs,
        decision_id="arbitrage_hold",
    ), {**out, "hold_status": "holding", "hold_controls_battery": True}


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
    terminal_context: Mapping[str, Any] | None = None,
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

    result = {
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
    if terminal_context is not None:
        result.update(
            build_terminal_value_proxy(
                discharge_efficiency=discharge_efficiency,
                throughput_cost_ct_kwh=throughput_cost_ct_kwh,
                **terminal_context,
            )
        )
    return result
