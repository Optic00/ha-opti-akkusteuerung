"""Standby-Reserve vor geplantem Netzladen (issue #222). Pure Python, no HA.

An SMA Sunny Tripower Smart Energy goes into standby at night once its
battery is empty (#103). In standby it accepts no command, so a grid charge
window later in the same night is lost. Raising MinSOC permanently would
avoid the standby in every night, also in the nights without a planned grid
charge, and keep energy in the battery that the house could have used.

This module therefore holds a small reserve only while it matters:

- the backend is SMA (the standby is documented for the STP SE only),
- the sun is down,
- grid charging is allowed and a grid charge window opens before the next
  sunrise: a price below the feed-in tariff (independent of peaks) or the
  peak precharge conditions (``peak_rank._refill``, mirroring decisions.yaml),
- the SoC has reached the reserve.

Then a discharging mode becomes "Akku nur Laden" (a discharge lock; at night
there is no PV to charge from). Charging and non-discharging modes, and any
mode the strategy chose for a higher-priority reason, stay untouched. Missing
or invalid inputs release the reserve (fail-open): it only ever withholds
energy, it never adds a grid import.

``grid_charge_blocked`` detects the failure this reserve prevents: the
strategy requests "Akku Netzladen" while the inverter reports the documented
night standby. The coordinator surfaces that as a repair issue instead of
only the generic connection alert.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, tzinfo
from typing import Any

from .arbitrage import DISCHARGING_MODES, _instant, _price_periods
from .battery_model import BatteryModel
from .peak_rank import _classify, _hour_start, _number, _refill

STANDBY_RESERVE_DECISION = "standby_reserve"
HOLD_MODE = "Akku nur Laden"
GRID_CHARGE_MODE = "Akku Netzladen"
SMA_BACKENDS = frozenset({"sma", "sma_modbus"})
# A held reserve is released only this far above the setting, as the other
# SoC holds of the strategy (MinSOC, L3/L4) do; SoC readings of an idle
# battery may step by one percent.
RELEASE_BAND = 2.0
# Largest PV or battery power (W) still consistent with an idle inverter, as
# alerts.STANDBY_IDLE_W.
IDLE_W = 50.0


def next_grid_charge(
    *,
    now: datetime,
    timezone: tzinfo,
    price_series: Any,
    horizon_end: Any,
    sunrise: Any,
    grid_charging: bool,
    peak_reserve_active: bool,
    surcharge_ct: Any,
    spread_ct: Any,
    feed_in_ct: Any,
    maxsoc: Any,
    battery_model: BatteryModel,
) -> dict[str, Any]:
    """First possible grid charge window before ``sunrise``; never raises.

    Uses the refill search of the peak rank check, which mirrors the grid
    branches of decisions.yaml slot by slot. Future SoC and forecast score are
    unknown, so a possible window counts: near the reserve the SoC condition
    holds anyway, and a wrong guess only keeps a few percent in the battery.
    """
    if not grid_charging:
        return {"status": "grid_charging_off"}
    rising = _instant(sunrise)
    end = _instant(horizon_end)
    if rising is None or end is None:
        return {"status": "data_missing", "detail": "sunrise" if rising is None else "horizon"}
    periods = _price_periods(price_series, now, timezone)
    current = next((i for i, slot in enumerate(periods) if slot[0] <= now < slot[1]), None)
    if current is None:
        return {"status": "data_missing", "detail": "price_series"}
    feed_in = value if (value := _number(feed_in_ct)) is not None else 0
    # Grid charging below the feed-in tariff does not depend on a peak (first
    # branch of decisions.yaml); its window band only narrows the slot, so any
    # such slot before sunrise counts.
    below_feed_in = next((i for i in range(current, len(periods))
                          if periods[i][0] < rising and periods[i][2] < feed_in), None)
    if below_feed_in is not None:
        return {"status": "planned", "at": periods[below_feed_in][0].isoformat(),
                "kind": "grid_below_feed_in", "sunrise": rising.isoformat()}
    if not peak_reserve_active:
        # Without a peak reserve the precharge branch cannot fire.
        return {"status": "none", "sunrise": rising.isoformat()}
    surcharge = _number(surcharge_ct)
    if surcharge is None:
        return {"status": "data_missing", "detail": "surcharge"}
    spread = value if (value := _number(spread_ct)) is not None else 10
    window_start = _hour_start(now, timezone)
    window = [i for i, slot in enumerate(periods) if window_start <= slot[0] < end]
    window_min = min(periods[i][2] for i in window) if window else None
    peaks, _stored, percentile = _classify(
        periods, window, now, prices=[slot[2] for slot in periods], window_min=window_min,
        surcharge=surcharge, model=battery_model, spread=spread)
    refill = _refill(
        periods, peaks, current, end, timezone, grid=True, percentile=percentile, spread=spread,
        feed_in=feed_in,
        maxsoc=value if (value := _number(maxsoc)) is not None else 95,
        surcharge=surcharge, charging=False, model=battery_model,
    )
    if refill is None or periods[refill[0]][0] >= rising:
        return {"status": "none", "sunrise": rising.isoformat()}
    return {"status": "planned", "at": periods[refill[0]][0].isoformat(), "kind": refill[1],
            "sunrise": rising.isoformat()}


def apply_standby_reserve(
    result: Any,
    *,
    reserve_soc: Any,
    backend: Any,
    window: Mapping[str, Any],
    latched: bool,
    eligible: bool,
    timezone: tzinfo,
) -> tuple[Any, dict[str, Any], bool]:
    """Block a discharge at the reserve; return result, report and new latch."""
    reserve = _number(reserve_soc)
    states = getattr(result, "states", {})
    soc = _number(states.get("sensor.opti_soc"))
    minsoc = _number(states.get("input_number.minsoc"))
    report: dict[str, Any] = {"reserve_soc": reserve, "controls_battery": False,
                              "grid_charge": dict(window)}
    if reserve is None or reserve <= 0:
        return result, {**report, "status": "disabled"}, False
    if backend not in SMA_BACKENDS:
        return result, {**report, "status": "backend_not_supported"}, False
    if not eligible:
        return result, {**report, "status": "manual_or_strategy_disabled"}, False
    if states.get("sun.sun") != "below_horizon":
        return result, {**report, "status": "daylight"}, False
    if window.get("status") != "planned":
        return result, {**report, "status": "no_grid_charge_planned"}, False
    if soc is None or minsoc is None:
        return result, {**report, "status": "data_missing"}, False
    if minsoc >= reserve:
        return result, {**report, "status": "minsoc_covers"}, False
    limit = reserve + (RELEASE_BAND if latched else 0.0)
    if soc > limit:
        return result, {**report, "status": "armed"}, False
    if getattr(result, "mode", None) not in DISCHARGING_MODES:
        # Already not discharging (MinSOC, Pause, grid charging): keep the
        # latch so a later discharge decision inside the band stays blocked.
        return result, {**report, "status": "not_discharging"}, True
    at = _instant(window.get("at"))
    when = at.astimezone(timezone).strftime("%H:%M") if at is not None else "?"
    reason = f"Standby-Reserve ({reserve:g} %, Netzladen ab {when} geplant)"
    values = dict(states)
    values["sensor.opti_strategie_vorschau"] = HOLD_MODE
    attributes = {
        **result.attributes,
        "sensor.opti_strategie_vorschau": {
            **result.attributes.get("sensor.opti_strategie_vorschau", {}),
            "grund": reason,
        },
    }
    held = replace(result, mode=HOLD_MODE, reason=reason, states=values, attributes=attributes,
                   decision_id=STANDBY_RESERVE_DECISION)
    return held, {**report, "status": "holding", "controls_battery": True}, True


def grid_charge_blocked(data: Mapping[str, Any], standby_codes: frozenset[int]) -> bool:
    """Grid charging is the effective mode, but the inverter sleeps (#222).

    Same device evidence as ``alerts.night_standby``: reachable, documented
    standby code (which only means "no information"), sun down, an idle
    device (no PV, no battery flow) and an empty battery (SoC at MinSOC or the
    standby reserve, plus the release band). Missing telemetry fails closed.
    Only the mode actually commanded counts, not a strategy wish that a safety
    rule replaced.
    """
    connection = data.get("connection_status")
    if not isinstance(connection, Mapping) or connection.get("status") != "not_ready":
        return False
    if connection.get("inverter_status") not in standby_codes or not data.get("online", False):
        return False
    if data.get("mode") != GRID_CHARGE_MODE:
        return False
    states = data.get("states")
    if not isinstance(states, Mapping) or states.get("sun.sun") != "below_horizon":
        return False
    pv, battery = _number(states.get("sensor.opti_pv_generation_w")), _number(states.get("sensor.opti_battery_power_w"))
    if pv is None or battery is None or pv > IDLE_W or abs(battery) > IDLE_W:
        return False
    soc, minsoc = _number(states.get("sensor.opti_soc")), _number(states.get("input_number.minsoc"))
    report = data.get("standby_reserve")
    reserve = _number(report.get("reserve_soc")) if isinstance(report, Mapping) else None
    if soc is None or minsoc is None:
        return False
    return bool(soc <= max(minsoc, reserve or 0.0) + RELEASE_BAND)
