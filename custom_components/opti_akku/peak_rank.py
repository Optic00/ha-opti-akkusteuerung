"""Shadow check: would a price-rank allocation keep peak energy for later?

Observation only. Neither the strategy nor any write reads this result.

Peak ladder L1 discharges in every VERY_EXPENSIVE slot, which is only a
percentile over today and tomorrow. With little usable energy it can spend the
battery on an evening peak although a more expensive morning peak follows
before the next refill. This check ranks the current slot and the coming peak
slots up to the next dependable refill by price, assigns each slot its expected
house load and fills them from the most expensive one with the usable energy.
The current slot is worth discharging only if it is among the served slots.

Peak slots use the same classification as ``sensor.opti_peak_reserve_soc``
(midrank percentile >= 0.6 and the minimum surcharge over the window low).
Only peak slots and the current slot draw on the battery; consumption between
peaks is not modelled. The confirmed boundary is the PV horizon of the peak
reserve. Future SoC, reserve and forecast score are not predicted: a possible
price-driven grid refill makes the check uncertain instead of shortening it.
Prices must cover the whole reserve horizon, also for the refill's peak average.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, tzinfo
from math import isfinite
from typing import Any

from .arbitrage import _instant, _price_periods

DISCHARGE_EFFICIENCY = 0.9
PEAK_PERCENTILE = 0.60
WEAK_SCORE = 3
# Same entry band as the precharge window in decisions.yaml.
WINDOW_BAND_CT = 0.5
HOLD_WINDOW_BAND_CT = 1.5
PEAK_DECISIONS = frozenset({"peak_l1", "peak_l2"})
MAX_INTERVAL = 90
_COUNTERS = (
    "ready_seconds",
    "peak_seconds",
    "agree_seconds",
    "hold_seconds",
    "discharged_while_agree_kwh",
    "discharged_while_hold_kwh",
)

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
                          charging: bool = False) -> str | None:
    """Ladebedingungen aus decisions.yaml; fehlende Zukunftswerte sind nur möglich.

    Einstieg: SoC < Ziel - 3, Fenster bis Tief + 0,5 ct. Bereits laufendes
    Netzladen: SoC < Ziel, Fenster bis Tief + 1,5 ct. Ein möglicher Zweig ist
    ohne verlässliche Zukunftswerte ausdrücklich kein Nachladebeleg.
    """
    stopband = 0 if charging else 3
    band = HOLD_WINDOW_BAND_CT if charging else WINDOW_BAND_CT
    if minimum is not None and price > minimum + band:
        return None
    if (price < feed_in and (score is None or score < WEAK_SCORE)
            and (soc is None or soc < maxsoc - stopband)):
        return "grid_below_feed_in"
    if (average is not None and average - price >= spread
            and (soc is None or reserve_soc is None or soc < reserve_soc - stopband)):
        return "grid_precharge"
    return None


def _refill(slots: Sequence[Slot], peaks: set[int], first: int, end: datetime, timezone: tzinfo, *,
            grid: bool, spread: float, feed_in: float, maxsoc: float,
            surcharge: float, charging: bool) -> tuple[int, str] | None:
    """Erstes mögliches Netzladefenster, kein belastbarer Refill.

    Mirrors the grid branches of decisions.yaml as seen from that slot. Like
    the peak reserve template, it looks from the start of that slot's full
    hour: the peak average includes earlier peaks of the same hour. The
    charging window ignores expired slots, as in the template. A charge
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
            if slots[i][1] <= slots[index][0]:
                continue
            if i in future_peaks:
                break
            before_peak.append(slots[i][2])
        ahead = [slots[i][2] for i in view if i in future_peaks]
        kind = _grid_charge_possible(
            price=price, minimum=round(min(before_peak), 2) if before_peak else None,
            average=round(sum(ahead) / len(ahead), 2) if ahead else None,
            spread=spread, feed_in=feed_in, maxsoc=maxsoc,
            # Heutige Messwerte belegen weder SoC noch Reserve/Score dieses Slots.
            soc=None, reserve_soc=None, score=None, charging=charging,
        )
        if kind is not None:
            return index, kind
    return None


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
    maxsoc: Any = 95,
) -> dict[str, Any]:
    """Return the rank verdict for this cycle; never raises on bad input.

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
    peaks = {
        i for i in window
        if _percentile(prices, periods[i][2]) >= PEAK_PERCENTILE
        and (window_min is None or periods[i][2] >= window_min + values["surcharge"])
    }
    refill = _refill(
        periods, peaks, current + 1, end, timezone, grid=grid_charging,
        spread=value if (value := _number(spread_ct)) is not None else 10,
        feed_in=value if (value := _number(feed_in_ct)) is not None else 0,
        maxsoc=value if (value := _number(maxsoc)) is not None else 95,
        surcharge=values["surcharge"], charging=strategy_mode == "Akku Netzladen",
    )
    if refill:
        return {**out, "status": "uncertain", "verdict": "uncertain",
                "detail": "future_charge_conditions", "horizon_end": end.isoformat(),
                "possible_refill_at": periods[refill[0]][0].isoformat(),
                "possible_refill_kind": refill[1],
                "reason": "Unsicher: künftiger SoC, Reserve-SoC, Prognose-Score und "
                          "Lademodus für das mögliche Netzladefenster sind nicht belegt"}
    boundary = end

    candidates = sorted(
        {current, *(i for i in peaks if current < i and periods[i][0] < boundary)},
        key=lambda i: (-periods[i][2], periods[i][0]),
    )
    usable = values["capacity"] * max(0.0, values["soc"] - values["minsoc"]) / 100 * DISCHARGE_EFFICIENCY
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
                "assumptions": None, **dict.fromkeys(_COUNTERS, 0.0)}

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
            if assumptions is not None and (
                not isinstance(assumptions, list) or len(assumptions) != 6
                or any(item is not None and not isinstance(item, bool) and _number(item) is None
                       for item in assumptions)
            ):
                return
            counters = {key: _number(saved.get(key)) for key in _COUNTERS}
            if any(value is None or value < 0 for value in counters.values()):
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
        out: dict[str, Any] = {key: round(self.data[key], 3) for key in _COUNTERS}
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
        self._previous = (now, dict(check), _number(battery_power_w))
        self.data["last_observation"] = now.isoformat()
        return self.totals()
