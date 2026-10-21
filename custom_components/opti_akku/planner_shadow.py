"""Shadow operation of the horizon planner: inputs, verdict, counters, history.

Observation only. Neither the strategy nor any write reads these results.
The planner recommends a mode in the ladder's vocabulary for the current
slot; the shadow records how often that recommendation differs from the mode
the ladder actually chose, so the value of a planner can be judged after
weeks of parallel operation. A rolling store keeps hourly snapshots of the PV
forecast and the price series for later offline replays.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from math import isfinite
from time import perf_counter
from typing import Any

from .arbitrage import _price_periods
from .planner import (
    DYNAMIC,
    GRID_CHARGE,
    MAX_HORIZON_HOURS,
    MODES,
    ONLY_CHARGE,
    ONLY_DISCHARGE,
    Battery,
    Horizon,
    Settings,
    Slot,
    Step,
    baseline_cost,
)

SLOT_HALF_HOUR = timedelta(minutes=30)
FORECAST_MAX_AGE = 21600
FORECAST_ROW_LIMIT = 100
PLAN_INTERVAL = timedelta(minutes=10)
MIN_HORIZON_HOURS = 2.0
SOC_TARGET_HOURS = 12
MAX_INTERVAL = 90
QUANTILES = ("p10", "p50")
DEVIATIONS = ("planner_grid_charge", "ladder_grid_charge", "planner_hold", "planner_discharge",
              "planner_no_charge", "indifferent", "other")
_COUNTERS = ("ready_seconds", "agree_seconds", *(f"{name}_seconds" for name in DEVIATIONS))
HISTORY_DAYS = 60
HISTORY_VERSION = 1
price_periods = _price_periods


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    return result if isfinite(result) else None


def _instant(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("An explicit timezone is required")
    return value.astimezone(UTC)


def _hour(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


# -- inputs -------------------------------------------------------------------

def pv_forecast_hourly(states: Sequence[Any], now: datetime,
                       max_age: float = FORECAST_MAX_AGE) -> tuple[str, dict[datetime, tuple[float, float]]]:
    """Solcast ``detailedForecast`` rows of several entities as hourly (P10, P50) kWh.

    Returns a status and the hours. Half-hour rows of the same hour are
    summed. One invalid entity invalidates the whole result (fail closed).
    """
    hours: dict[datetime, list[float]] = {}
    seen: dict[datetime, tuple[float, float]] = {}
    found = False
    for state in states:
        if state is None or getattr(state, "state", None) in (None, "unknown", "unavailable"):
            continue
        detail = getattr(state, "attributes", {}).get("detailedForecast")
        if not isinstance(detail, list) or not detail:
            continue
        if len(detail) > FORECAST_ROW_LIMIT:
            return "invalid", {}
        try:
            age = (now - _instant(state.last_reported or state.last_updated)).total_seconds()
        except ValueError, TypeError, AttributeError:
            return "invalid", {}
        if max_age > 0 and not -60 <= age <= max_age:
            return "stale", {}
        try:
            for row in detail:
                start = _instant(row["period_start"])
                values = []
                for key in ("pv_estimate10", "pv_estimate"):
                    kw = _number(row.get(key))
                    if kw is None or not 0 <= kw <= 1000:
                        raise ValueError("Missing or invalid PV estimate")
                    values.append(kw * SLOT_HALF_HOUR.total_seconds() / 3600)
                pair = (values[0], values[1])
                if start in seen:
                    if seen[start] != pair:
                        raise ValueError("Conflicting PV intervals")
                    continue
                seen[start] = pair
                found = True
                bucket = hours.setdefault(_hour(start), [0.0, 0.0])
                bucket[0] += pair[0]
                bucket[1] += pair[1]
        except KeyError, ValueError, TypeError, AttributeError:
            return "invalid", {}
    if not found:
        return "no_forecast", {}
    return "ready", {hour: (round(v[0], 4), round(v[1], 4)) for hour, v in sorted(hours.items())}


def _load_w(load_hours: Any, start: datetime, fallback_w: float) -> float:
    if isinstance(load_hours, Mapping):
        value = _number(load_hours.get(str(int(_hour(start).timestamp()))))
        if value is not None and 0 <= value <= 50000:
            return value
    return fallback_w


def build_slots(now: datetime, periods: Sequence[tuple[datetime, datetime, float]],
                pv_hourly: Mapping[datetime, tuple[float, float]], quantile: str,
                load_hours: Any, fallback_load_w: float,
                max_hours: float = MAX_HORIZON_HOURS) -> list[Slot]:
    """Price periods from ``now`` on, as long as PV forecast hours cover them."""
    which = QUANTILES.index(quantile)
    slots: list[Slot] = []
    total = 0.0
    for start, end, price in periods:
        if end <= now:
            continue
        begin = max(start, now)
        hours = (end - begin).total_seconds() / 3600
        if hours <= 0:
            continue
        pv = pv_hourly.get(_hour(begin))
        if pv is None or total + hours > max_hours + 1e-9:
            break
        slots.append(Slot(begin, hours, price, pv[which] * hours, _load_w(load_hours, begin, fallback_load_w) / 1000 * hours))
        total += hours
    return slots


@dataclass(frozen=True)
class PlannerInputs:
    price_series: Any
    pv_status: str
    pv_hourly: Mapping[datetime, tuple[float, float]]
    load_hours: Any
    load_source: str
    fallback_load_w: Any
    soc: Any
    capacity_kwh: Any
    minsoc: Any
    maxsoc: Any
    max_charge_w: Any
    max_discharge_w: Any
    feed_in_ct: Any
    grid_charging: bool
    ladder_mode: Any
    quantile: str = "p10"
    wear_ct_kwh: float = 3.0
    terminal_factor: float = 0.8


def _battery(inputs: PlannerInputs) -> tuple[Battery | None, str | None]:
    values = {name: _number(getattr(inputs, name)) for name in (
        "capacity_kwh", "minsoc", "maxsoc", "max_charge_w", "max_discharge_w")}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        return None, missing[0]
    battery = Battery(values["capacity_kwh"], values["minsoc"], values["maxsoc"],
                      values["max_charge_w"], values["max_discharge_w"])
    return (None, battery.problem()) if battery.problem() else (battery, None)


def deviation(planner_mode: str, ladder_mode: Any) -> str:
    """Classify why planner and ladder differ; ``agree`` when they do not."""
    if ladder_mode not in MODES:
        return "other"
    if planner_mode == ladder_mode:
        return "agree"
    if planner_mode == GRID_CHARGE:
        return "planner_grid_charge"
    if ladder_mode == GRID_CHARGE:
        return "ladder_grid_charge"
    planner_discharges = planner_mode in (DYNAMIC, ONLY_DISCHARGE)
    ladder_discharges = ladder_mode in (DYNAMIC, ONLY_DISCHARGE)
    if ladder_discharges and not planner_discharges:
        return "planner_hold"
    if planner_discharges and not ladder_discharges:
        return "planner_discharge"
    planner_charges = planner_mode in (DYNAMIC, ONLY_CHARGE)
    ladder_charges = ladder_mode in (DYNAMIC, ONLY_CHARGE)
    if ladder_charges and not planner_charges:
        return "planner_no_charge"
    return "other"


def _soc_targets(steps: Sequence[Step], now: datetime, hours: int) -> list[int]:
    """SoC the plan reaches at the end of each of the next ``hours`` full hours."""
    targets: list[int] = []
    boundary = _hour(now) + timedelta(hours=1)
    for step in steps:
        end = step.slot.start + timedelta(hours=step.slot.hours)
        while end >= boundary - timedelta(seconds=1) and len(targets) < hours:
            targets.append(int(round(step.soc_after)))
            boundary += timedelta(hours=1)
        if len(targets) >= hours:
            break
    return targets


class PlannerShadow:
    """Cached horizon, per-cycle verdict and time-weighted deviation counters."""

    def __init__(self) -> None:
        self.data = self._fresh()
        self._previous: tuple[datetime, str] | None = None
        self._horizon: Horizon | None = None
        self._fingerprint: tuple | None = None
        self._planned_at: datetime | None = None
        self._solve_ms: float | None = None

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {"version": 1, "started_at": None, "last_observation": None, "day": None,
                "today": dict.fromkeys(_COUNTERS, 0.0), "total": dict.fromkeys(_COUNTERS, 0.0)}

    def snapshot(self) -> dict[str, Any]:
        return {**self.data, "today": dict(self.data["today"]), "total": dict(self.data["total"])}

    def restore(self, saved: Any) -> None:
        self.data = self._fresh()
        self._previous = None
        if not isinstance(saved, Mapping) or saved.get("version") != 1:
            return
        try:
            for key in ("started_at", "last_observation"):
                raw = saved.get(key)
                if raw is not None and datetime.fromisoformat(raw).tzinfo is None:
                    return
            buckets = {}
            for bucket in ("today", "total"):
                raw = saved.get(bucket)
                if not isinstance(raw, Mapping):
                    return
                counters = {key: _number(raw.get(key)) for key in _COUNTERS}
                if any(value is None or value < 0 for value in counters.values()):
                    return
                buckets[bucket] = counters
            day = saved.get("day")
            if day is not None and not isinstance(day, str):
                return
        except TypeError, ValueError, AttributeError:
            return
        self.data = {"version": 1, "started_at": saved.get("started_at"),
                     "last_observation": saved.get("last_observation"), "day": day, **buckets}

    def interrupt(self) -> None:
        """A failed evaluation ends the held verdict; the gap never counts."""
        self._previous = None

    # -- planning ----------------------------------------------------------
    def evaluate(self, now: datetime, timezone: tzinfo, inputs: PlannerInputs) -> dict[str, Any]:
        """Return this cycle's verdict; never raises on bad input."""
        out: dict[str, Any] = {"observation_only": True, "controls_battery": False,
                               "quantile": inputs.quantile, "load_source": inputs.load_source}
        if inputs.quantile not in QUANTILES:
            return {**out, "status": "data_missing", "detail": "quantile"}
        battery, problem = _battery(inputs)
        if battery is None:
            return {**out, "status": "data_missing", "detail": problem}
        soc = _number(inputs.soc)
        if soc is None or not 0 <= soc <= 100:
            return {**out, "status": "data_missing", "detail": "soc"}
        feed_in = _number(inputs.feed_in_ct)
        if feed_in is None:
            return {**out, "status": "data_missing", "detail": "feed_in"}
        fallback_load = _number(inputs.fallback_load_w)
        if fallback_load is None or fallback_load < 0:
            return {**out, "status": "data_missing", "detail": "load"}
        periods = _price_periods(inputs.price_series, now, timezone)
        if not periods:
            return {**out, "status": "data_missing", "detail": "price_series"}
        if inputs.pv_status != "ready":
            return {**out, "status": "data_missing", "detail": f"pv_forecast_{inputs.pv_status}"}
        slots = build_slots(now, periods, inputs.pv_hourly, inputs.quantile,
                            inputs.load_hours, fallback_load)
        horizon_hours = sum(slot.hours for slot in slots)
        if horizon_hours < MIN_HORIZON_HOURS:
            return {**out, "status": "data_missing", "detail": "horizon_short",
                    "horizon_hours": round(horizon_hours, 2)}
        settings = Settings(feed_in_ct=feed_in, wear_ct_kwh=inputs.wear_ct_kwh,
                            grid_charging=bool(inputs.grid_charging),
                            terminal_factor=inputs.terminal_factor)
        # Slot ends, prices and hourly rates stay the same within a period, so
        # the running first slot does not force a new plan every cycle.
        fingerprint = (
            tuple((s.start + timedelta(hours=s.hours), round(s.price_ct, 3),
                   round(s.pv_kwh / s.hours, 3), round(s.load_kwh / s.hours, 3)) for s in slots),
            battery, settings,
        )
        stale = (self._planned_at is None or now - self._planned_at > PLAN_INTERVAL
                 or now < self._planned_at)
        if self._horizon is None or fingerprint != self._fingerprint or stale:
            started = perf_counter()
            horizon = Horizon(slots, battery, settings)
            horizon.solve(soc)
            self._solve_ms = round((perf_counter() - started) * 1000, 1)
            self._horizon, self._fingerprint, self._planned_at = horizon, fingerprint, now
        horizon = self._horizon
        result = horizon.walk(soc)
        if not result.steps:
            return {**out, "status": "data_missing", "detail": "no_plan"}
        step = result.first
        baseline = baseline_cost(slots, battery, settings, soc, horizon.terminal_ct)
        verdict = "indifferent" if step.indifferent else deviation(step.mode, inputs.ladder_mode)
        out.update(
            status="ready", verdict=step.mode, ladder_mode=inputs.ladder_mode,
            deviation=verdict,
            grid_charge_w=round(step.charge_limit_w) if step.charge_limit_w is not None else 0,
            planned_at=self._planned_at.isoformat() if self._planned_at else None,
            solve_ms=self._solve_ms,
            horizon_hours=round(horizon_hours, 2), slots=len(slots),
            slot_minutes=int(round(slots[0].hours * 60)) if len(slots) == 1 else int(round(slots[1].hours * 60)),
            plan_cost_eur=round(result.objective_eur, 3),
            baseline_self_consumption_eur=round(baseline, 3),
            expected_saving_eur=round(baseline - result.objective_eur, 3),
            terminal_ct_kwh=round(horizon.terminal_ct, 2),
            plan_grid_charge_kwh=round(sum(s.grid_charge_kwh for s in result.steps), 2),
            plan_discharge_kwh=round(sum(s.discharge_kwh for s in result.steps), 2),
            soc_target_next_hours=_soc_targets(result.steps, now, SOC_TARGET_HOURS),
            first_grid_charge=next((s.slot.start.isoformat() for s in result.steps if s.grid_charge_kwh > 0), None),
        )
        return out

    # -- counters ------------------------------------------------------------
    def totals(self) -> dict[str, Any]:
        return {
            "started_at": self.data["started_at"], "last_observation": self.data["last_observation"],
            "day": self.data["day"],
            "today": {key: round(value) for key, value in self.data["today"].items()},
            "total": {key: round(value) for key, value in self.data["total"].items()},
            "today_disagreement_percent": self._share(self.data["today"]),
            "total_disagreement_percent": self._share(self.data["total"]),
        }

    @staticmethod
    def _share(bucket: Mapping[str, float]) -> float | None:
        ready = bucket["ready_seconds"]
        return round((ready - bucket["agree_seconds"]) / ready * 100, 1) if ready > 0 else None

    def observe(self, now: datetime, timezone: tzinfo, check: Mapping[str, Any]) -> dict[str, Any]:
        """Zero-order hold over the previous verdict; gaps over 90 s never count."""
        day = now.astimezone(timezone).date().isoformat()
        if self.data["day"] != day:
            self.data["day"] = day
            self.data["today"] = dict.fromkeys(_COUNTERS, 0.0)
        if self.data["started_at"] is None:
            self.data["started_at"] = now.isoformat()
        if self._previous is not None:
            before, held = self._previous
            elapsed = (now - before).total_seconds()
            if 0 < elapsed <= MAX_INTERVAL and held:
                for bucket in ("today", "total"):
                    self.data[bucket]["ready_seconds"] += elapsed
                    self.data[bucket][f"{held}_seconds"] += elapsed
        held = check.get("deviation") if check.get("status") == "ready" else None
        self._previous = (now, held if isinstance(held, str) and f"{held}_seconds" in _COUNTERS else "")
        self.data["last_observation"] = now.isoformat()
        return self.totals()


class ForecastHistory:
    """Rolling hourly snapshots of PV forecast and price series (local store only)."""

    def __init__(self, max_days: int = HISTORY_DAYS) -> None:
        self.max_entries = max_days * 24
        self.entries: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        return {"version": HISTORY_VERSION, "max_days": self.max_entries // 24,
                "entries": [dict(entry) for entry in self.entries]}

    def restore(self, saved: Any) -> None:
        self.entries = []
        if not isinstance(saved, Mapping) or saved.get("version") != HISTORY_VERSION:
            return
        entries = saved.get("entries")
        if not isinstance(entries, list):
            return
        kept = []
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("at"), str):
                continue
            try:
                _instant(entry["at"])
            except ValueError, TypeError:
                continue
            kept.append(dict(entry))
        self.entries = kept[-self.max_entries:]

    def summary(self) -> dict[str, Any]:
        return {"entries": len(self.entries), "max_entries": self.max_entries,
                "first": self.entries[0]["at"] if self.entries else None,
                "last": self.entries[-1]["at"] if self.entries else None}

    def record(self, now: datetime, pv_hourly: Mapping[datetime, tuple[float, float]],
               periods: Sequence[tuple[datetime, datetime, float]]) -> bool:
        """Add one snapshot per full hour; returns True when something was added."""
        hour = _hour(now)
        stamp = hour.isoformat()
        if self.entries and self.entries[-1]["at"] >= stamp:
            return False
        pv = [(h, v) for h, v in sorted(pv_hourly.items()) if h >= hour][: int(MAX_HORIZON_HOURS)]
        prices = [(start, price) for start, end, price in periods if end > hour]
        if not pv and not prices:
            return False
        entry: dict[str, Any] = {"at": stamp}
        if pv:
            entry["pv_from"] = pv[0][0].isoformat()
            entry["pv_p10"] = [round(v[0], 3) for _, v in pv]
            entry["pv_p50"] = [round(v[1], 3) for _, v in pv]
        if prices:
            entry["price_from"] = prices[0][0].isoformat()
            entry["price_minutes"] = int(round((periods[0][1] - periods[0][0]).total_seconds() / 60))
            entry["price_ct"] = [round(price, 3) for _, price in prices]
        self.entries.append(entry)
        del self.entries[:-self.max_entries]
        return True
