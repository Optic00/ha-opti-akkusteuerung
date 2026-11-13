"""Price-rank allocation for peak energy: shadow check and optional hold.

Without the option ``peak_rank_control`` this is observation only; neither the
strategy nor any write reads the result. With the option the coordinator
calls ``apply_peak_rank_hold`` before the write: a clear ``hold`` for an L1/L2
discharge blocks the discharge (see there).

Peak ladder L1 discharges in every VERY_EXPENSIVE slot, which is only a
percentile over today and tomorrow. With little usable energy it can spend the
battery on an evening peak although a more expensive morning peak follows
before the next refill. This check ranks the current slot and the coming peak
slots up to the next dependable refill by price, assigns each slot its expected
house load and fills them from the most expensive one with the usable energy.
The current slot is worth discharging only if it is among the served slots.

Peak slots use the same classification as ``sensor.opti_peak_reserve_soc``
(midrank percentile >= 0.6 and the minimum surcharge over the window low, or
economic against the battery model, issue #231; see ``_classify``).
Only peak slots and the current slot draw on the battery; consumption between
peaks is not modelled. The confirmed boundary is the PV horizon of the peak
reserve. Future SoC, reserve and forecast score are not predicted: a possible
price-driven grid refill makes the check uncertain instead of shortening it.
Prices must cover the whole reserve horizon, also for the refill's peak average.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, tzinfo
from math import isfinite
from typing import Any

from .arbitrage import DISCHARGING_MODES, _instant, _price_periods
from .battery_model import (
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_MARGIN_CT,
    DEFAULT_THROUGHPUT_COST_CT_KWH,
    BatteryModel,
)

PEAK_PERCENTILE = 0.60
WEAK_SCORE = 3
# Same entry band as the precharge window in decisions.yaml.
WINDOW_BAND_CT = 0.5
HOLD_WINDOW_BAND_CT = 1.5
# Same hold band as the economic precharge condition in decisions.yaml.
ECONOMIC_HOLD_BAND_CT = 1.0
PEAK_DECISIONS = frozenset({"peak_l1", "peak_l2"})
HOLD_DECISION = "peak_rank_hold"
MAX_INTERVAL = 90
# Model assumptions (model_assumptions) plus six settings, as the coordinator
# passes them.
SETTING_COUNT = 6
ASSUMPTION_COUNT = 5 + SETTING_COUNT
# Snapshots before the battery model had only the six settings; the template
# then always calculated with 0.9.
LEGACY_DISCHARGE_EFFICIENCY = 0.9
LEGACY_ECONOMICS = (DEFAULT_CHARGE_EFFICIENCY, DEFAULT_THROUGHPUT_COST_CT_KWH,
                    DEFAULT_MARGIN_CT, True)
_COUNTERS = (
    "ready_seconds",
    "peak_seconds",
    "agree_seconds",
    "hold_seconds",
    "discharged_while_agree_kwh",
    "discharged_while_hold_kwh",
)
# Time in which the optional hold actually blocked a discharge. Not part of
# the shadow counters above; snapshots before the option count as zero.
_CONTROL_COUNTERS = ("blocked_seconds",)

Slot = tuple[datetime, datetime, float]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    return result if isfinite(result) else None


def _percentile(prices: Sequence[float], price: float) -> float:
    lower = sum(1 for item in prices if item < price)
    equal = sum(1 for item in prices if item == price)
    return (lower + 0.5 * equal) / len(prices)


def _peak_percentile(prices: Sequence[float], price: float) -> float:
    """Midrank with the template's floor: the top price tier counts as EXPENSIVE."""
    result = _percentile(prices, price)
    if price >= max(prices) and max(prices) > min(prices):
        result = max(result, PEAK_PERCENTILE)
    return result


def _economic_floor(low: float, margin: float, model: BatteryModel) -> float:
    """Model minimum, at least ``margin`` (spread or surcharge) over the reference."""
    return max(model.minimum_high_price(low), low + margin)


def _classify(periods: Sequence[Slot], window: Sequence[int], now: datetime, *,
              prices: Sequence[float], window_min: float | None, surcharge: float,
              model: BatteryModel, spread: float = 10.0) -> tuple[set[int], set[int], set[int]]:
    """Peak and stored-economic slots exactly as ``sensor.opti_peak_reserve_soc``.

    Peak: percentile >= 0.6 above the surcharge, or forward economic (beats the
    cheapest not yet ended window slot before it by the model's minimum and the
    grid-charge spread, as precharging itself). Stored: not a peak, but beats
    the cheapest slot before it since the start of the price series by the
    model's minimum and the surcharge (issue #231). The template caps stored slots at the
    current battery content; the rank allocation does that through
    ``usable`` already. The third set holds the percentile peaks alone, the
    template's reference for the precharge spread average.
    """
    economic = model.economics_valid
    in_window = set(window)
    peaks: set[int] = set()
    stored: set[int] = set()
    percentile: set[int] = set()
    reference: float | None = None
    forward: float | None = None
    for index, (_start, stop, price) in enumerate(periods):
        if index in in_window:
            percentile_peak = (_peak_percentile(prices, price) >= PEAK_PERCENTILE
                               and (window_min is None or price >= window_min + surcharge))
            ahead = economic and forward is not None and price >= _economic_floor(forward, spread, model)
            if percentile_peak:
                percentile.add(index)
            if percentile_peak or ahead:
                peaks.add(index)
            elif economic and reference is not None and price >= _economic_floor(reference, surcharge, model):
                stored.add(index)
            if stop > now:
                forward = price if forward is None else min(forward, price)
        reference = price if reference is None else min(reference, price)
    return peaks, stored, percentile


def _load_kw(hours: Any, start: datetime, fallback_kw: float) -> float:
    if isinstance(hours, Mapping):
        key = str(int(start.timestamp() // 3600 * 3600))
        value = _number(hours.get(key))
        if value is not None and 0 <= value <= 50000:
            return value / 1000
    return fallback_kw


def _hour_start(at: datetime, timezone: tzinfo) -> datetime:
    """Full local hour, as ``now().replace(minute=0, ...)`` in the template."""
    return at.astimezone(timezone).replace(minute=0, second=0, microsecond=0)


def _grid_charge_possible(*, price: float, minimum: float | None, average: float | None,
                          spread: float, feed_in: float, soc: float | None,
                          reserve_soc: float | None, maxsoc: float, score: float | None,
                          model: BatteryModel, charging: bool = False) -> str | None:
    """Ladebedingungen aus decisions.yaml; fehlende Zukunftswerte sind nur möglich.

    Einstieg: SoC < Ziel - 3, Fenster bis Tief + 0,5 ct. Bereits laufendes
    Netzladen: SoC < Ziel, Fenster bis Tief + 1,5 ct. Ein möglicher Zweig ist
    ohne verlässliche Zukunftswerte ausdrücklich kein Nachladebeleg.
    Peak-Vorladen zusätzlich nur wirtschaftlich (Akkumodell, Halteband 1 ct
    bei laufendem Netzladen). Näherung: ungewichteter Mittelwert der
    künftigen Peaks statt des lastgewichteten Template-Werts.
    """
    stopband = 0 if charging else 3
    band = HOLD_WINDOW_BAND_CT if charging else WINDOW_BAND_CT
    if minimum is not None and price > minimum + band:
        return None
    if (price < feed_in and (score is None or score < WEAK_SCORE)
            and (soc is None or soc < maxsoc - stopband)):
        return "grid_below_feed_in"
    economic_band = ECONOMIC_HOLD_BAND_CT if charging else 0.0
    if (average is not None and average - price >= spread
            and model.economics_valid
            and average + economic_band >= model.minimum_high_price(price)
            and (soc is None or reserve_soc is None or soc < reserve_soc - stopband)):
        return "grid_precharge"
    return None


def _refill(slots: Sequence[Slot], peaks: set[int], first: int, end: datetime, timezone: tzinfo, *,
            grid: bool, percentile: set[int] | None = None, spread: float, feed_in: float, maxsoc: float,
            surcharge: float, charging: bool, model: BatteryModel) -> tuple[int, str] | None:
    """Erstes mögliches Netzladefenster, kein belastbarer Refill.

    Mirrors the grid branches of decisions.yaml as seen from that slot. Like
    the peak reserve template, it looks from the start of that slot's full
    hour: the peak average includes earlier peaks of the same hour. The
    charging window uses only remaining slots before the next peak, with
    the lowest price plus the entry or hold band. A charge
    after the last peak protects nothing and does not count.
    """
    if not grid:
        return None
    limit = next((i for i in range(first, len(slots)) if slots[i][0] >= end), len(slots))
    for index in range(first, limit):
        if not any(index <= i < limit for i in peaks):
            break
        price = slots[index][2]
        hour = _hour_start(slots[index][0], timezone)
        view = [i for i in range(limit) if slots[i][0] >= hour]
        low = min(slots[i][2] for i in view)
        future_peaks = {i for i in view if i in peaks and slots[i][2] >= low + surcharge}
        before_peak: list[float] = []
        for i in view:
            # Expired slots still count for the peak average, not the price window.
            if slots[i][1] <= slots[index][0]:
                continue
            if i in future_peaks:
                break
            before_peak.append(slots[i][2])
        # Spread average over percentile peaks, as the template; economic
        # peaks only when there is none (issue #231).
        spread_peaks = [i for i in view if i in future_peaks and (percentile is None or i in percentile)]
        ahead = [slots[i][2] for i in (spread_peaks or [i for i in view if i in future_peaks])]
        kind = _grid_charge_possible(
            price=price, minimum=round(min(before_peak), 2) if before_peak else None,
            average=round(sum(ahead) / len(ahead), 2) if ahead else None,
            spread=spread, feed_in=feed_in, maxsoc=maxsoc,
            # Heutige Messwerte belegen weder SoC noch Reserve/Score dieses Slots.
            soc=None, reserve_soc=None, score=None, charging=charging, model=model,
        )
        if kind is not None:
            return index, kind
    return None


def model_assumptions(model: BatteryModel) -> list[Any]:
    """Model values the verdict depends on; a change restarts the totals."""
    return [model.discharge_efficiency, model.charge_efficiency,
            model.throughput_cost_ct_kwh, model.margin_ct, model.economics_valid]


def peak_rank_check(
    *,
    now: datetime,
    timezone: tzinfo,
    price_series: Any,
    horizon_end: Any,
    load_hours: Any,
    fallback_load_kw: Any,
    surcharge_ct: Any,
    soc: Any,
    capacity_kwh: Any,
    minsoc: Any,
    decision_id: Any,
    peak_reserve_active: bool,
    strategy_mode: Any = None,
    grid_charging: bool,
    spread_ct: Any,
    feed_in_ct: Any,
    score: Any,
    discharge_efficiency: float,
    battery_model: BatteryModel,
    maxsoc: Any = 95,
) -> dict[str, Any]:
    """Return the rank verdict for this cycle; never raises on bad input.

    ``discharge_efficiency`` kommt aus dem Akkumodell (stored -> AC),
    ``battery_model`` liefert die Wirtschaftlichkeit des Peak-Vorladens.

    ``score`` ist ein heutiger Messwert, keine Freigabe für künftiges Netzladen.
    """
    out: dict[str, Any] = {"observation_only": True, "controls_battery": False}
    if not peak_reserve_active:
        return {**out, "status": "not_applicable", "detail": "peak_reserve_inactive"}
    periods = _price_periods(price_series, now, timezone)
    if len(periods) < 4:
        return {**out, "status": "data_missing", "detail": "price_series"}
    end = _instant(horizon_end)
    if end is None:
        return {**out, "status": "data_missing", "detail": "horizon"}
    values = {"soc": _number(soc), "capacity": _number(capacity_kwh), "minsoc": _number(minsoc),
              "load": _number(fallback_load_kw), "surcharge": _number(surcharge_ct)}
    missing = [key for key, value in values.items() if value is None]
    if missing:
        return {**out, "status": "data_missing", "detail": missing[0]}
    if not values["capacity"] > 0 or not 0 <= values["soc"] <= 100 or values["load"] <= 0:
        return {**out, "status": "data_missing", "detail": "battery"}
    current = next((i for i, slot in enumerate(periods) if slot[0] <= now < slot[1]), None)
    if current is None or end <= now:
        return {**out, "status": "not_applicable", "detail": "no_horizon"}

    covered_until = now
    for start, stop, _price in periods[current:]:
        if start > covered_until:
            break
        covered_until = max(covered_until, stop)
        if covered_until >= end:
            break
    if covered_until < end:
        return {**out, "status": "incomplete", "verdict": "uncertain",
                "detail": "price_coverage", "horizon_end": end.isoformat(),
                "price_coverage_end": covered_until.isoformat(),
                "reason": "Unvollständig: Preise decken den Reservehorizont nicht lückenlos ab"}

    prices = [slot[2] for slot in periods]
    window_start = _hour_start(now, timezone)
    window = [i for i, slot in enumerate(periods) if window_start <= slot[0] < end]
    window_min = min(periods[i][2] for i in window) if window else None
    spread = value if (value := _number(spread_ct)) is not None else 10
    peaks, stored, percentile = _classify(periods, window, now, prices=prices, window_min=window_min,
                                          surcharge=values["surcharge"], model=battery_model,
                                          spread=spread)
    refill = _refill(
        periods, peaks, current + 1, end, timezone, grid=grid_charging, percentile=percentile,
        spread=value if (value := _number(spread_ct)) is not None else 10,
        feed_in=value if (value := _number(feed_in_ct)) is not None else 0,
        maxsoc=value if (value := _number(maxsoc)) is not None else 95,
        surcharge=values["surcharge"], charging=strategy_mode == "Akku Netzladen",
        model=battery_model,
    )
    if refill:
        return {**out, "status": "uncertain", "verdict": "uncertain",
                "detail": "future_charge_conditions", "horizon_end": end.isoformat(),
                "possible_refill_at": periods[refill[0]][0].isoformat(),
                "possible_refill_kind": refill[1],
                "reason": "Unsicher: künftiger SoC, Reserve-SoC, Prognose-Score und "
                          "Lademodus für das mögliche Netzladefenster sind nicht belegt"}
    boundary = end

    # Stored economic slots compete for the battery like peaks (as the
    # template's rank protection does); only the refill check uses peaks.
    candidates = sorted(
        {current, *(i for i in peaks | stored if current < i and periods[i][0] < boundary)},
        key=lambda i: (-periods[i][2], periods[i][0]),
    )
    usable = values["capacity"] * max(0.0, values["soc"] - values["minsoc"]) / 100 * discharge_efficiency
    remaining = usable
    demand = 0.0
    served: list[int] = []
    for index in candidates:
        start, stop, _price = periods[index]
        hours = (min(stop, boundary) - max(start, now)).total_seconds() / 3600
        energy = _load_kw(load_hours, start, values["load"]) * hours
        demand += energy
        if remaining > 1e-9:
            served.append(index)
            remaining -= energy
    rank_discharges = current in served
    covers_all = usable >= demand - 1e-9
    threshold = periods[served[-1]][2] if served else None
    current_price = periods[current][2]
    rank = candidates.index(current) + 1
    strategy = decision_id if decision_id in PEAK_DECISIONS else None
    verdict = (
        "not_peak" if strategy is None
        else "discharge" if rank_discharges
        else "hold"
    )
    if not served:
        reason = f"Halten: kein nutzbarer Inhalt über MinSOC; jetzt {current_price:.1f} ct"
    elif rank_discharges and covers_all:
        reason = (f"Entladen: {usable:.2f} kWh reichen für alle {len(candidates)} Peak-Slots "
                  f"bis zum Refill ({demand:.2f} kWh)")
    elif rank_discharges:
        reason = (f"Entladen: jetzt {current_price:.1f} ct liegt auf Rang {rank} von "
                  f"{len(candidates)}, {usable:.2f} kWh reichen bis {threshold:.1f} ct")
    else:
        reason = (f"Halten: {usable:.2f} kWh reichen nur für Slots ab {threshold:.1f} ct; jetzt "
                  f"{current_price:.1f} ct auf Rang {rank} von {len(candidates)}")
    return {
        **out,
        "status": "ready",
        "verdict": verdict,
        "strategy_decision": strategy,
        # L1/L2 pick "Akku Dynamisch" by day with PV surplus; it still
        # discharges on deficit, so the verdict applies to both modes.
        "strategy_mode": strategy_mode if strategy is not None and isinstance(strategy_mode, str) else None,
        "rank_discharges": rank_discharges,
        "reason": reason,
        "current_slot_start": periods[current][0].isoformat(),
        "current_price_ct": round(current_price, 2),
        "threshold_price_ct": round(threshold, 2) if threshold is not None else None,
        "current_rank": rank,
        "peak_slots": len(candidates),
        "usable_kwh": round(usable, 3),
        "demand_kwh": round(demand, 3),
        "covers_all": covers_all,
        "refill_at": boundary.isoformat(),
        "refill_kind": "pv_horizon",
        "horizon_end": end.isoformat(),
    }


def _hold_reason(check: Mapping[str, Any]) -> str:
    current = _number(check.get("current_price_ct"))
    threshold = _number(check.get("threshold_price_ct"))
    now_text = f"jetzt {current:.1f} ct" if current is not None else "jetzt ohne Preis"
    if threshold is None:
        return f"Peak-Rangfolge (halten: kein nutzbarer Inhalt über MinSOC, {now_text})"
    return f"Peak-Rangfolge (halten für Slots ab {threshold:.1f} ct, {now_text})"


def apply_peak_rank_hold(
    result: Any,
    check: Any,
    latch: tuple[tuple[str, Any], str] | None,
    *,
    eligible: bool,
) -> tuple[Any, str, tuple[tuple[str, Any], str] | None]:
    """Block an L1/L2 discharge on a clear ``hold``; otherwise keep ``result``.

    Returns the possibly replaced result, a control status and the new latch.
    Only ``status == "ready"`` with ``verdict == "hold"`` sets the hold. Within
    one price slot and the same strategy decision a set hold stays while the
    fresh verdict is ``discharge``: holding keeps the usable energy, PV
    charging can raise it, and discharging lowers it again, so the verdict
    could otherwise alternate within the slot. Any other result (uncertain,
    incomplete, missing data, error) releases the hold and clears the latch:
    without a remembered state the fresh check decides (fail-open).

    "Akku Dynamisch" becomes "Akku nur Laden", so PV charging stays as the
    strategy allowed it. L1/L2 choose "Akku nur Entladen" when charging is
    blocked (charge cap, PV surplus charging off, night); that becomes
    "Akku Pause", so the hold never charges where the strategy did not.
    """
    if (not eligible or getattr(result, "decision_id", None) not in PEAK_DECISIONS
            or getattr(result, "mode", None) not in DISCHARGING_MODES):
        return result, "not_applicable", None
    if not isinstance(check, Mapping) or check.get("status") != "ready":
        return result, "released", None
    key = (result.decision_id, check.get("current_slot_start"))
    verdict = check.get("verdict")
    if verdict == "hold":
        reason, status = _hold_reason(check), "holding"
    elif verdict == "discharge" and latch is not None and latch[0] == key:
        reason, status = latch[1], "latched"
    else:
        return result, "released", None
    mode = "Akku nur Laden" if result.mode == "Akku Dynamisch" else "Akku Pause"
    values = dict(result.states)
    values["sensor.opti_strategie_vorschau"] = mode
    attrs = {
        **result.attributes,
        "sensor.opti_strategie_vorschau": {
            **result.attributes.get("sensor.opti_strategie_vorschau", {}),
            "grund": reason,
        },
    }
    held = replace(result, mode=mode, reason=reason, states=values, attributes=attrs,
                   decision_id=HOLD_DECISION)
    return held, status, (key, reason)


class PeakRankShadow:
    """Time-weighted totals; zero-order hold over the previous check.

    Gaps over 90 s, the first check after a restart and backwards clock steps
    never count. Changed assumptions restart the totals.
    """

    def __init__(self) -> None:
        self.data = self._fresh()
        self._previous: tuple[datetime, dict[str, Any], float | None] | None = None

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {"version": 2, "started_at": None, "last_observation": None,
                "assumptions": None, **dict.fromkeys(_COUNTERS + _CONTROL_COUNTERS, 0.0)}

    def snapshot(self) -> dict[str, Any]:
        return dict(self.data)

    def restore(self, saved: Any) -> None:
        self.data = self._fresh()
        self._previous = None
        if not isinstance(saved, Mapping) or saved.get("version") != 2:
            return
        try:
            for key in ("started_at", "last_observation"):
                raw = saved.get(key)
                if raw is not None and datetime.fromisoformat(raw).tzinfo is None:
                    return
            assumptions = saved.get("assumptions")
            if isinstance(assumptions, list) and len(assumptions) == SETTING_COUNT:
                assumptions = [LEGACY_DISCHARGE_EFFICIENCY, *assumptions]
            if isinstance(assumptions, list) and len(assumptions) == SETTING_COUNT + 1:
                # Only the discharge efficiency was stored; the precharge
                # economics did not exist and correspond to the default model.
                assumptions = [assumptions[0], *LEGACY_ECONOMICS, *assumptions[1:]]
            if assumptions is not None and (
                not isinstance(assumptions, list) or len(assumptions) != ASSUMPTION_COUNT
                or any(item is not None and not isinstance(item, bool) and _number(item) is None
                       for item in assumptions)
            ):
                return
            counters = {key: _number(saved.get(key)) for key in _COUNTERS}
            if any(value is None or value < 0 for value in counters.values()):
                return
            for key in _CONTROL_COUNTERS:
                counters[key] = _number(saved.get(key, 0.0))
                if counters[key] is None or counters[key] < 0:
                    return
            if abs(counters["agree_seconds"] + counters["hold_seconds"]
                   - counters["peak_seconds"]) > 1e-6 * max(1.0, counters["peak_seconds"]):
                return
        except TypeError, ValueError, AttributeError:
            return
        self.data = {"version": 2, "started_at": saved.get("started_at"),
                     "last_observation": saved.get("last_observation"),
                     "assumptions": assumptions, **counters}

    def interrupt(self) -> None:
        self._previous = None

    def totals(self) -> dict[str, Any]:
        out: dict[str, Any] = {key: round(self.data[key], 3) for key in _COUNTERS + _CONTROL_COUNTERS}
        peak = self.data["peak_seconds"]
        out["hold_percent"] = round(self.data["hold_seconds"] / peak * 100, 3) if peak > 0 else None
        out["started_at"] = self.data["started_at"]
        out["last_observation"] = self.data["last_observation"]
        return out

    def observe(self, now: datetime, check: Mapping[str, Any], assumptions: list[Any],
                battery_power_w: Any) -> dict[str, Any]:
        if self.data["assumptions"] != assumptions:
            self.data = self._fresh()
            self.data["assumptions"] = list(assumptions)
            self.data["started_at"] = now.isoformat()
            self._previous = None
        elif self.data["started_at"] is None:
            self.data["started_at"] = now.isoformat()
        if self._previous is not None:
            before, held, power = self._previous
            elapsed = (now - before).total_seconds()
            if 0 < elapsed <= MAX_INTERVAL and held.get("status") == "ready":
                # Negative battery power is discharging.
                discharged = max(0.0, -power) * elapsed / 3600 / 1000 if power is not None else 0.0
                self.data["ready_seconds"] += elapsed
                verdict = held.get("verdict")
                if verdict in ("discharge", "hold"):
                    key = "agree" if verdict == "discharge" else "hold"
                    self.data["peak_seconds"] += elapsed
                    self.data[f"{key}_seconds"] += elapsed
                    self.data[f"discharged_while_{key}_kwh"] += discharged
                if held.get("controls_battery") is True:
                    self.data["blocked_seconds"] += elapsed
        self._previous = (now, dict(check), _number(battery_power_w))
        self.data["last_observation"] = now.isoformat()
        return self.totals()
