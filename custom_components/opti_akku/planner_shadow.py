"""Shadow operation of the horizon planner: inputs, verdict, counters, history.

Observation only. Neither the strategy nor any write reads these results.
The planner recommends a mode in the ladder's vocabulary for the current
slot; the shadow records how often that recommendation differs from the mode
the ladder actually chose, so the value of a planner can be judged after
weeks of parallel operation. A rolling store keeps snapshots of the PV
forecast and the price series for later offline replays.

The evaluation is pure CPU work without Home Assistant imports, so the
coordinator can run it in an executor thread.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from math import isfinite
from time import perf_counter
from typing import Any

from .arbitrage import _price_periods
from .source_quality import DEFAULT_FORECAST_MAX_AGE
from .planner import (
    DYNAMIC,
    GRID_CHARGE,
    MAX_HORIZON_HOURS,
    MODES,
    ONLY_CHARGE,
    ONLY_DISCHARGE,
    Battery,
    Horizon,
    PlannerBudgetError,
    Settings,
    Slot,
    Step,
)

# Solcast interval attributes and their slot length.
FORECAST_ATTRIBUTES = (("detailedForecast", timedelta(minutes=30)), ("detailedHourly", timedelta(hours=1)))
QUANTILE_KEYS = {"p10": "pv_estimate10", "p50": "pv_estimate"}
QUANTILES = tuple(QUANTILE_KEYS)
# The entity's last update is only a proxy for the fetch time; the forecast
# must additionally cover the current hour. The coordinator passes the
# configured ``forecast_max_age`` option (#103).
FORECAST_MAX_AGE = DEFAULT_FORECAST_MAX_AGE
FORECAST_ROW_LIMIT = 100
PLAN_INTERVAL = timedelta(minutes=10)
REPLAN_MIN_INTERVAL = timedelta(minutes=2)
# Total solve budget per evaluation (main plan plus the variant B check);
# building the slots and walking the plan are outside it.
SOLVE_BUDGET_S = 1.0
# Plausible snapshot stamps: integration era up to a generous future bound.
HISTORY_MIN_STAMP = 1_700_000_000
HISTORY_MAX_STAMP = 4_000_000_000
MIN_HORIZON_HOURS = 2.0
SOC_TARGET_HOURS = 12
MAX_INTERVAL = 90
# Load and PV rates in the cache fingerprint are quantised to this (kWh per
# hour) so measurement noise does not force a new plan every cycle.
FINGERPRINT_KWH = 0.05
DEVIATIONS = ("planner_grid_charge", "ladder_grid_charge", "planner_hold", "planner_discharge",
              "planner_no_charge", "indifferent", "other")
_COUNTERS = ("ready_seconds", "agree_seconds", *(f"{name}_seconds" for name in DEVIATIONS))
HISTORY_DAYS = 60
HISTORY_MAX_ENTRIES = 2400
HISTORY_VERSION = 3  # 3: packed comma strings; 2: plain number lists (migrated on restore)
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

PvHours = dict[datetime, tuple[float | None, float | None]]


def pv_forecast_hourly(states: Sequence[Any], now: datetime,
                       max_age: float = FORECAST_MAX_AGE) -> tuple[str, PvHours]:
    """Solcast interval rows of several entities as hourly (P10, P50) kWh.

    ``detailedForecast`` (half hours) or ``detailedHourly`` (hours) per entity;
    a missing quantile stays ``None`` instead of invalidating the rows. An hour
    counts only when its rows cover all 60 minutes (both half hours, or the
    full hourly row); a partly covered hour is left out, so the horizon ends
    at that gap instead of reading half the energy. The result must cover the
    current hour. One invalid entity invalidates the whole result (fail closed).
    """
    hours: dict[datetime, list[float | None]] = {}
    covered: dict[datetime, float] = {}
    seen: dict[datetime, tuple[float | None, float | None]] = {}
    found = False
    for state in states:
        if state is None or getattr(state, "state", None) in (None, "unknown", "unavailable"):
            continue
        attributes = getattr(state, "attributes", {}) or {}
        detail, length = None, None
        for name, slot_length in FORECAST_ATTRIBUTES:
            if isinstance(attributes.get(name), list) and attributes[name]:
                detail, length = attributes[name], slot_length
                break
        if detail is None:
            continue
        if len(detail) > FORECAST_ROW_LIMIT:
            return "invalid", {}
        try:
            age = (now - _instant(state.last_updated)).total_seconds()
        except ValueError, TypeError, AttributeError:
            return "invalid", {}
        if max_age > 0 and not -60 <= age <= max_age:
            return "stale", {}
        try:
            for row in detail:
                start = _instant(row["period_start"])
                values: list[float | None] = []
                for key in QUANTILE_KEYS.values():
                    if row.get(key) is None:
                        values.append(None)
                        continue
                    kw = _number(row.get(key))
                    if kw is None or not 0 <= kw <= 1000:
                        raise ValueError("Invalid PV estimate")
                    values.append(kw * length.total_seconds() / 3600)
                pair = (values[0], values[1])
                if start in seen:
                    if seen[start] != pair:
                        raise ValueError("Conflicting PV intervals")
                    continue
                seen[start] = pair
                found = True
                covered[_hour(start)] = covered.get(_hour(start), 0.0) + length.total_seconds()
                bucket = hours.setdefault(_hour(start), [0.0, 0.0])
                for index in range(2):
                    if pair[index] is None:
                        bucket[index] = None
                    elif bucket[index] is not None:
                        bucket[index] += pair[index]
        except KeyError, ValueError, TypeError, AttributeError:
            return "invalid", {}
    if not found:
        return "no_forecast", {}
    hours = {hour: v for hour, v in hours.items() if covered.get(hour) == 3600.0}
    if _hour(now) not in hours:
        return "stale", {}
    return "ready", {hour: (None if v[0] is None else round(v[0], 4), None if v[1] is None else round(v[1], 4))
                     for hour, v in sorted(hours.items())}


def _load_w(load_hours: Any, start: datetime, fallback_w: float) -> float:
    if isinstance(load_hours, Mapping):
        value = _number(load_hours.get(str(int(_hour(start).timestamp()))))
        if value is not None and 0 <= value <= 50000:
            return value
    return fallback_w


def build_slots(now: datetime, periods: Sequence[tuple[datetime, datetime, float]],
                pv_hourly: Mapping[datetime, tuple[float | None, float | None]], quantile: str,
                load_hours: Any, fallback_load_w: float,
                max_hours: float = MAX_HORIZON_HOURS) -> list[Slot]:
    """Contiguous price periods from the one covering ``now``, while PV hours cover them.

    Returns ``[]`` when no period covers ``now``: a plan that starts later
    must never be read as the recommendation for now.
    """
    which = QUANTILES.index(quantile)
    slots: list[Slot] = []
    total = 0.0
    expected_start: datetime | None = None
    for start, end, price in periods:
        if end <= now:
            continue
        if expected_start is None:
            if start > now:
                return []
        elif start != expected_start:
            break
        begin = max(start, now)
        hours = (end - begin).total_seconds() / 3600
        pv = pv_hourly.get(_hour(begin))
        if pv is None or pv[which] is None or total + hours > max_hours + 1e-9:
            break
        slots.append(Slot(begin, hours, price, pv[which] * hours,
                          _load_w(load_hours, begin, fallback_load_w) / 1000 * hours))
        total += hours
        expected_start = end
    return slots


@dataclass(frozen=True)
class PlannerInputs:
    price_series: Any
    pv_status: str
    pv_hourly: Mapping[datetime, tuple[float | None, float | None]]
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
    min_benefit_ct: float = 0.0  # variant A
    grid_check_quantile: str | None = None  # variant B: e.g. "p50"


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


def _structure(slots: Sequence[Slot], battery: Battery, settings: Settings) -> tuple:
    """What must match exactly for any reuse: period grid, prices, battery limits, settings."""
    return (tuple((s.start + timedelta(hours=s.hours), round(s.price_ct, 3)) for s in slots), battery, settings)


def _rates(slots: Sequence[Slot]) -> tuple:
    """Quantised hourly PV and load rates; jitter here may keep a recent plan briefly."""
    return tuple((round(s.pv_kwh / s.hours / FINGERPRINT_KWH), round(s.load_kwh / s.hours / FINGERPRINT_KWH))
                 for s in slots)


class _Cached:
    """One solved horizon with the identity it was solved for."""

    def __init__(self, horizon: Horizon, structure: tuple, rates: tuple, planned_at: datetime,
                 solve_ms: float) -> None:
        self.horizon = horizon
        self.structure = structure
        self.rates = rates
        self.planned_at = planned_at
        self.solve_ms = solve_ms


class PlannerShadow:
    """Cached horizons, per-cycle verdict and time-weighted deviation counters."""

    def __init__(self) -> None:
        self.data = self._fresh()
        self._previous: tuple[datetime, str] | None = None
        self._cache: dict[str, _Cached] = {}

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
    def _horizon(self, key: str, now: datetime, slots: list[Slot], battery: Battery,
                 settings: Settings, soc: float, budget_s: float) -> _Cached:
        """Solved horizon for ``slots``; reused while only the first slot shrinks.

        Any change of the period grid, prices, battery limits or settings
        invalidates the cache at once. Only PV/load rate jitter may keep a plan
        younger than ``REPLAN_MIN_INTERVAL``; ``budget_s`` is the solve budget left.
        """
        structure = _structure(slots, battery, settings)
        rates = _rates(slots[1:])
        cached = self._cache.get(key)
        age = None if cached is None else now - cached.planned_at
        recent = cached is not None and age is not None and timedelta(0) <= age
        if recent and cached.structure == structure and (
                (cached.rates == rates and age <= PLAN_INTERVAL) or age < REPLAN_MIN_INTERVAL):
            cached.horizon.resolve_first(slots[0], deadline_s=budget_s)
            return cached
        horizon = Horizon(slots, battery, settings)
        elapsed = horizon.solve(soc, deadline_s=budget_s)
        cached = _Cached(horizon, structure, rates, now, round(elapsed * 1000, 1))
        self._cache[key] = cached
        return cached

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
        if not slots:
            return {**out, "status": "data_missing", "detail": "price_now"}
        horizon_hours = sum(slot.hours for slot in slots)
        if horizon_hours < MIN_HORIZON_HOURS:
            return {**out, "status": "data_missing", "detail": "horizon_short",
                    "horizon_hours": round(horizon_hours, 2)}
        settings = Settings(feed_in_ct=feed_in, wear_ct_kwh=inputs.wear_ct_kwh,
                            grid_charging=bool(inputs.grid_charging),
                            terminal_factor=inputs.terminal_factor,
                            min_benefit_ct=max(0.0, _number(inputs.min_benefit_ct) or 0.0))
        started = perf_counter()
        try:
            cached = self._horizon("main", now, slots, battery, settings, soc, SOLVE_BUDGET_S)
            result, baseline = cached.horizon.recommend(soc)
            grid_check = None
            if (inputs.grid_check_quantile in QUANTILES and inputs.grid_check_quantile != inputs.quantile
                    and result.steps and result.first.grid_charge_kwh > 0):
                alternative = build_slots(now, periods, inputs.pv_hourly, inputs.grid_check_quantile,
                                          inputs.load_hours, fallback_load)
                if len(alternative) != len(slots):
                    # Fail closed: grid charging was promised only with the check's confirmation.
                    return {**out, "status": "data_missing",
                            "detail": f"grid_check_{inputs.grid_check_quantile}_missing"}
                remaining = max(0.0, SOLVE_BUDGET_S - (perf_counter() - started))
                check_plan, _ = self._horizon("check", now, alternative, battery, settings, soc,
                                              remaining).horizon.recommend(soc)
                if check_plan.steps and check_plan.first.grid_charge_kwh <= 0:
                    result, grid_check = check_plan, f"blocked_by_{inputs.grid_check_quantile}"
                else:
                    grid_check = f"confirmed_by_{inputs.grid_check_quantile}"
        except PlannerBudgetError:
            self._cache.pop("main", None)
            self._cache.pop("check", None)
            return {**out, "status": "data_missing", "detail": "budget_exceeded"}
        if not result.steps:
            return {**out, "status": "data_missing", "detail": "no_plan"}
        step = result.first
        verdict = "indifferent" if step.indifferent else deviation(step.ladder_mode, inputs.ladder_mode)
        out.update(
            status="ready", verdict=step.ladder_mode, executable_mode=step.mode,
            ladder_mode=inputs.ladder_mode, deviation=verdict, plan_source=result.source,
            grid_check=grid_check,
            planned_charge_w=round(step.charge_w) if step.charge_w is not None else 0,
            planned_discharge_w=round(step.discharge_w) if step.discharge_w is not None else 0,
            grid_charge_w=round(step.grid_charge_kwh / step.slot.hours * 1000) if step.slot.hours > 0 else 0,
            planned_at=cached.planned_at.isoformat(), solve_ms=cached.solve_ms,
            horizon_hours=round(horizon_hours, 2), slots=len(slots),
            slot_minutes=int(round(slots[-1].hours * 60)),
            plan_cost_eur=round(result.objective_eur, 3),
            baseline_self_consumption_eur=round(baseline.objective_eur, 3),
            expected_saving_eur=round(baseline.objective_eur - result.objective_eur, 3),
            terminal_ct_kwh=round(cached.horizon.terminal_ct, 2),
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
        """Disagreement among the seconds in which a decision was possible."""
        decided = bucket["ready_seconds"] - bucket["indifferent_seconds"]
        return round((decided - bucket["agree_seconds"]) / decided * 100, 1) if decided > 0 else None

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


# -- history ------------------------------------------------------------------

def _pv_rows(pv_hourly: Mapping[datetime, tuple[float | None, float | None]], hour: datetime) -> dict[str, Any] | None:
    """All PV hours from ``hour`` on as index-addressed lists of Wh (``None`` = no forecast).

    Everything the source offers is kept (not only the planning horizon), so an
    unchanged forecast looks identical an hour later and causes no snapshot.
    """
    items = sorted((h, v) for h, v in pv_hourly.items() if h >= hour)[:72]
    if not items:
        return None
    first = items[0][0]
    count = int((items[-1][0] - first).total_seconds() // 3600) + 1
    p10: list[int | None] = [None] * count
    p50: list[int | None] = [None] * count
    for stamp, (a, b) in items:
        index = int((stamp - first).total_seconds() // 3600)
        p10[index] = None if a is None else int(round(a * 1000))
        p50[index] = None if b is None else int(round(b * 1000))
    return {"from": int(first.timestamp()), "p10": _pack(p10), "p50": _pack(p50)}


def _pack(values: Sequence[Any]) -> str:
    """Comma-joined values, ``-`` for ``None``: one JSON string instead of one line per number.

    ``[None]`` becomes ``"-"`` and stays distinct from the empty list ``""``.
    """
    return ",".join("-" if v is None else str(v) for v in values)


def _unpack(text: Any, cast) -> list[Any]:
    if not isinstance(text, str):
        raise TypeError("packed list expected")
    return [None if item == "-" else cast(item) for item in text.split(",")] if text else []


def _migrate_v2(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Version 2 kept plain number lists; pack them into the version 3 strings."""
    migrated = dict(entry)
    pv = entry.get("pv")
    if isinstance(pv, Mapping):
        migrated["pv"] = {"from": pv["from"], "p10": _pack(list(pv["p10"])), "p50": _pack(list(pv["p50"]))}
    if isinstance(entry.get("price"), list):
        migrated["price"] = [[start, seconds, _pack(list(values))] for start, seconds, values in entry["price"]]
    return migrated


def _pv_unchanged(last: Any, new: Mapping[str, Any]) -> bool:
    """True when ``new`` is ``last`` shifted by whole hours without new or changed values."""
    if not isinstance(last, Mapping):
        return False
    offset = (new["from"] - last["from"]) // 3600
    if offset < 0 or (new["from"] - last["from"]) % 3600:
        return False
    for key in ("p10", "p50"):
        if _unpack(last[key], int)[offset:] != _unpack(new[key], int):
            return False
    return True


def _price_segments(periods: Sequence[tuple[datetime, datetime, float]]) -> list[list[Any]]:
    """Contiguous runs of equal period length as ``[start, seconds, [ct, ...]]``.

    The whole known series is kept (today and tomorrow), so the snapshot only
    changes when new prices arrive or the day rolls over.
    """
    segments: list[list[Any]] = []
    for start, end, price in periods:
        seconds = int((end - start).total_seconds())
        if segments and segments[-1][1] == seconds and segments[-1][3] == start:
            segments[-1][2].append(round(price, 3))
            segments[-1][3] = end
        else:
            segments.append([int(start.timestamp()), seconds, [round(price, 3)], end])
    return [[start, seconds, _pack(values)] for start, seconds, values, _end in segments]


def history_pv(entry: Mapping[str, Any]) -> PvHours:
    """Inverse of ``_pv_rows``: hourly (P10, P50) kWh keyed by UTC hour."""
    rows = entry.get("pv")
    if not isinstance(rows, Mapping):
        return {}
    first = datetime.fromtimestamp(rows["from"], UTC)
    out: PvHours = {}
    for index, (a, b) in enumerate(zip(_unpack(rows["p10"], int), _unpack(rows["p50"], int), strict=True)):
        if a is None and b is None:
            continue
        out[first + timedelta(hours=index)] = (None if a is None else a / 1000, None if b is None else b / 1000)
    return out


def history_prices(entry: Mapping[str, Any]) -> list[tuple[datetime, datetime, float]]:
    """Inverse of ``_price_segments``: dated periods."""
    periods = []
    for start, seconds, values in entry.get("price") or []:
        cursor = datetime.fromtimestamp(start, UTC)
        for value in _unpack(values, float):
            periods.append((cursor, cursor + timedelta(seconds=seconds), value))
            cursor += timedelta(seconds=seconds)
    return periods


class ForecastHistory:
    """Rolling snapshots of PV forecast and price series (local store only).

    A snapshot is written only when the forecast or the price series changed,
    at most once per hour, so a day holds a dozen PV entries and one or two
    price entries instead of 24 copies of everything.
    """

    def __init__(self, max_days: int = HISTORY_DAYS, max_entries: int = HISTORY_MAX_ENTRIES) -> None:
        self.max_days = max_days
        self.max_entries = max_entries
        self.entries: list[dict[str, Any]] = []
        self.error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {"version": HISTORY_VERSION, "max_days": self.max_days, "entries": list(self.entries)}

    def restore(self, saved: Any) -> None:
        self.entries = []
        if saved is None:
            return
        version = saved.get("version") if isinstance(saved, Mapping) else None
        if version not in (2, HISTORY_VERSION):
            self.error = "unsupported_format"
            return
        entries = saved.get("entries")
        if not isinstance(entries, list):
            self.error = "unsupported_format"
            return
        kept = []
        dropped = 0
        for entry in entries:
            stamp = entry.get("t") if isinstance(entry, Mapping) else None
            if not isinstance(stamp, int) or isinstance(stamp, bool) or not HISTORY_MIN_STAMP <= stamp <= HISTORY_MAX_STAMP:
                dropped += 1
                continue
            try:
                candidate = _migrate_v2(entry) if version == 2 else dict(entry)
                history_pv(candidate)
                history_prices(candidate)
            except KeyError, TypeError, ValueError, OverflowError:
                dropped += 1
                continue
            kept.append(candidate)
        if dropped:
            self.error = f"{dropped}_entries_invalid"
        self.entries = kept[-self.max_entries:]

    def summary(self) -> dict[str, Any]:
        return {"entries": len(self.entries), "max_entries": self.max_entries, "max_days": self.max_days,
                "first": datetime.fromtimestamp(self.entries[0]["t"], UTC).isoformat() if self.entries else None,
                "last": datetime.fromtimestamp(self.entries[-1]["t"], UTC).isoformat() if self.entries else None,
                "error": self.error}

    def _last(self, kind: str) -> Any:
        for entry in reversed(self.entries):
            if kind in entry:
                return entry[kind]
        return None

    def record(self, now: datetime, pv_hourly: Mapping[datetime, tuple[float | None, float | None]],
               periods: Sequence[tuple[datetime, datetime, float]]) -> bool:
        """Add what changed since the last snapshot; returns True when something was added."""
        hour = _hour(now)
        stamp = int(hour.timestamp())
        if self.entries and self.entries[-1]["t"] > stamp:
            # Entries from the future (clock jump, foreign file) must not block recording.
            self.entries = [item for item in self.entries if item["t"] <= stamp]
        if self.entries and self.entries[-1]["t"] == stamp:
            return False
        entry: dict[str, Any] = {"t": stamp}
        pv = _pv_rows(pv_hourly, hour)
        if pv is not None and not _pv_unchanged(self._last("pv"), pv):
            entry["pv"] = pv
        prices = _price_segments(periods)
        if prices and prices != self._last("price"):
            entry["price"] = prices
        if len(entry) == 1:
            return False
        self.entries.append(entry)
        oldest = stamp - self.max_days * 86400
        self.entries = [item for item in self.entries if item["t"] >= oldest][-self.max_entries:]
        return True
