"""Planner-based policies: perfect-foresight oracle and day-ahead receding horizon.

Both use the integration's planner core (``custom_components.opti_akku.planner``)
and the same battery model as every other policy. They answer two questions:
how much the plant could save with perfect knowledge (oracle) and how much of
that a planner with the forecasts of the moment would get (receding horizon).

Oracle: one dynamic programme per measurement block over the actual PV and
load, prices of hours without a price filled with the month's mean. The
decision of every hour is read from the solved tables with the simulated SoC,
so rounding in the plan never accumulates. The terminal condition
``hold`` requires the block to end at least at its starting SoC (fair against
the other policies); ``value`` credits leftover energy like the live planner.

Receding horizon: at every full hour a new plan over the known prices (the
rest of today, tomorrow from 13:00) with the forecasts the harness provides:
PV level from the reconstructed day forecasts (``forecast_remaining`` and
``forecast_tomorrow``), shaped by the previous month's climatology (``naive``)
or the day's actual profile (``calibrated``, future knowledge of timing). Load
is the previous month's mean hourly profile (``climatology``) or the actual
hour (``perfect``). Both policies choose once per hour from the simulated SoC
and hold that move (mode plus charge and discharge setpoints) over the
quarter-hour steps; re-choosing mid-hour from the already moved state mixed
hourly moves with partial hours and cost about 30 EUR in the data set. Without
a price series the receding horizon falls back to Dynamisch.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .battery import DYNAMIC, BatteryParams
from .policies import Decision, StepContext
from .resources import load_planner_module
from .simulate import TOMORROW_KNOWN_FROM_HOUR, _blocks, climatology, previous_month
from .timeseries import Hour

planner = load_planner_module()
Battery, Horizon, Settings, Slot = planner.Battery, planner.Horizon, planner.Settings, planner.Slot


def battery_of(params: BatteryParams) -> Battery:
    return Battery(params.capacity_kwh, params.minsoc, params.maxsoc, params.max_charge_w,
                   params.max_discharge_w, params.eta_charge, params.eta_discharge)


def _hour_key(at: dt.datetime) -> dt.datetime:
    return at.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)


def monthly_mean_prices(hours: list[Hour]) -> dict[tuple[int, int], float]:
    sums: dict[tuple[int, int], list[float]] = defaultdict(list)
    for hour in hours:
        if hour.price_ct is not None:
            sums[(hour.ts_local.year, hour.ts_local.month)].append(hour.price_ct)
    return {key: sum(values) / len(values) for key, values in sums.items()}


def load_climatology(hours: list[Hour]) -> dict[tuple[int, int], list[float]]:
    """Mean load per local hour of day, keyed by (year, month)."""
    sums: dict[tuple[int, int], list[float]] = {}
    counts: dict[tuple[int, int], list[int]] = {}
    for hour in hours:
        if hour.load_kwh is None:
            continue
        key = (hour.ts_local.year, hour.ts_local.month)
        sums.setdefault(key, [0.0] * 24)[hour.ts_local.hour] += hour.load_kwh
        counts.setdefault(key, [0] * 24)[hour.ts_local.hour] += 1
    return {key: [s / c if c else 0.0 for s, c in zip(values, counts[key], strict=True)]
            for key, values in sums.items()}


def _decision(step: Any, policy: str, reason: str, params: BatteryParams) -> Decision:
    """Execute the planned flows faithfully: the executable mode plus both setpoints.

    The ladder vocabulary alone cannot express partial moves (charge a third
    of the surplus, discharge half the deficit); ``step.mode`` with the planned
    powers makes the simulated battery follow the plan exactly.
    """
    charge = step.charge_w if step.charge_w is not None else params.max_charge_w
    return Decision(step.mode, policy, reason, charge, True, step.soc_after, step.discharge_w)


@dataclass
class OraclePolicy:
    """Perfect foresight within each measurement block."""

    hours: list[Hour]
    plant: dict
    params: BatteryParams
    wear_ct: float = 3.0
    terminal: str = "hold"
    soc_step: float = 0.5
    name: str = "oracle"
    _tables: dict[dt.datetime, tuple[Horizon, int]] = field(default_factory=dict, repr=False)
    _held: tuple[dt.datetime, Decision] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.battery = battery_of(self.params)
        self.settings = Settings(feed_in_ct=float(self.plant.get("feed_in_ct", 0)), wear_ct_kwh=self.wear_ct,
                                 grid_charging=bool(self.plant.get("grid_precharge", True)),
                                 terminal=self.terminal, soc_step=self.soc_step)
        fill = monthly_mean_prices(self.hours)
        self.filled_hours = 0
        self.blocks = 0
        for block in _blocks(self.hours):
            slots = []
            for position in block:
                hour = self.hours[position]
                price = hour.price_ct
                if price is None:
                    price = fill.get((hour.ts_local.year, hour.ts_local.month), 0.0)
                    self.filled_hours += 1
                slots.append(Slot(_hour_key(hour.ts_local), 1.0, price, hour.pv_kwh, hour.load_kwh))
            first = self.hours[block[0]]
            start = first.soc if first.soc is not None else 50.0
            horizon = Horizon(slots, self.battery, self.settings)
            horizon.solve(min(self.params.maxsoc, max(self.params.minsoc, start)))
            for t, slot in enumerate(slots):
                self._tables[slot.start] = (horizon, t)
            self.blocks += 1
        self.steps = 0
        self.missing_steps = 0

    def decide(self, context: StepContext) -> Decision:
        """Choose once per hour from the simulated SoC; sub-steps execute that hourly move.

        The tables value a whole hour ahead, so re-choosing mid-hour from the
        already moved state would mix hourly moves with partial hours.
        """
        self.steps += 1
        key = _hour_key(context.now)
        if self._held is not None and self._held[0] == key:
            return self._held[1]
        entry = self._tables.get(key)
        decision = None
        if entry is not None:
            horizon, t = entry
            position = horizon.position(context.soc)
            action = horizon.choose(t, position)
            if action is not None:
                decision = _decision(horizon.step(t, position, action), "oracle", "perfekte Voraussicht", self.params)
        if decision is None:
            self.missing_steps += 1
            decision = Decision(DYNAMIC, "oracle_missing", "Stunde ohne Plan")
        self._held = (key, decision)
        return decision

    def reset(self) -> None:
        """Tables are per block already; only the held hour is discarded."""
        self._held = None

    def summary(self) -> dict[str, Any]:
        return {"planner": "oracle", "terminal": self.terminal, "wear_ct_kwh": self.wear_ct,
                "soc_step": self.soc_step, "blocks": self.blocks, "price_filled_hours": self.filled_hours,
                "steps": self.steps, "missing_steps": self.missing_steps}


@dataclass
class RecedingHorizonPolicy:
    """Day-ahead plan every hour with the harness forecasts of that moment."""

    hours: list[Hour]
    plant: dict
    params: BatteryParams
    forecast_mode: str = "naive"
    load_mode: str = "climatology"
    pv_mode: str = "forecast"  # perfect: actual PV per hour (diagnostic bound)
    wear_ct: float = 3.0
    terminal_factor: float = 0.8
    soc_step: float = 0.5
    # Robustness variants: A = minimum expected advantage over self-consumption
    # per plan (ct), B = plan on PV x pv_trust and allow grid charging only if
    # the plan on the full forecast charges from the grid as well.
    min_benefit_ct: float = 0.0
    pv_trust: float = 1.0
    grid_check: bool = False
    name: str = "rh"

    def __post_init__(self) -> None:
        if self.forecast_mode not in ("naive", "calibrated"):
            raise ValueError(f"Unknown forecast mode: {self.forecast_mode}")
        if self.load_mode not in ("climatology", "perfect"):
            raise ValueError(f"Unknown load mode: {self.load_mode}")
        if self.pv_mode not in ("forecast", "perfect"):
            raise ValueError(f"Unknown pv mode: {self.pv_mode}")
        self.battery = battery_of(self.params)
        self.settings = Settings(feed_in_ct=float(self.plant.get("feed_in_ct", 0)), wear_ct_kwh=self.wear_ct,
                                 grid_charging=bool(self.plant.get("grid_precharge", True)),
                                 terminal="value", terminal_factor=self.terminal_factor, soc_step=self.soc_step,
                                 min_benefit_ct=self.min_benefit_ct)
        if not 0 < self.pv_trust <= 1:
            raise ValueError("pv_trust must be within (0, 1]")
        self.pv_profiles = climatology(self.hours)
        self.load_profiles = load_climatology(self.hours)
        self.by_day: dict[dt.date, list[Hour]] = defaultdict(list)
        for hour in self.hours:
            self.by_day[hour.day].append(hour)
        self.by_hour = {_hour_key(hour.ts_local): hour for hour in self.hours}
        self.tz = self.hours[0].ts_local.tzinfo if self.hours else dt.UTC
        self._plan_key: dt.datetime | None = None
        self._horizon: Horizon | None = None
        self._held: Decision | None = None
        self.steps = 0
        self.plans = 0
        self.fallback_steps = 0
        self.min_benefit_fallbacks = 0
        self.grid_checks_blocked = 0
        self.solve_seconds = 0.0

    def _profile(self, table: dict, key: tuple[int, int]) -> list[float]:
        return table.get(previous_month(key)) or table.get(key) or [0.0] * 24

    def _day_hours(self, day: dt.date, tz: dt.tzinfo) -> list[dt.datetime]:
        start = dt.datetime.combine(day, dt.time(), tzinfo=tz).astimezone(dt.UTC)
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=tz).astimezone(dt.UTC)
        return [start + dt.timedelta(hours=i) for i in range(int(round((end - start).total_seconds() / 3600)))]

    def _pv_shape(self, day: dt.date, month_key: tuple[int, int], stamps: list[dt.datetime]) -> list[float]:
        """Weights per hour for the given stamps: climatology (naive) or the day's actual profile."""
        if self.forecast_mode == "calibrated":
            actual = {_hour_key(h.ts_local): (h.pv_kwh or 0.0) for h in self.by_day.get(day, [])}
            weights = [actual.get(stamp, 0.0) for stamp in stamps]
            if sum(weights) > 0:
                return weights
        profile = self._profile(self.pv_profiles, month_key)
        return [profile[stamp.astimezone(self.tz).hour] for stamp in stamps]

    def _pv(self, stamp: dt.datetime, forecast: float) -> float:
        if self.pv_mode == "perfect" and stamp in self.by_hour and self.by_hour[stamp].pv_kwh is not None:
            return self.by_hour[stamp].pv_kwh
        return forecast

    def _load(self, stamp: dt.datetime, month_key: tuple[int, int], local_hour: int) -> float:
        if self.load_mode == "perfect" and stamp in self.by_hour and self.by_hour[stamp].load_kwh is not None:
            return self.by_hour[stamp].load_kwh
        return self._profile(self.load_profiles, month_key)[local_hour]

    def _slots(self, context: StepContext) -> list[Slot] | None:
        if not context.price_today:
            return None
        now = context.now
        tz = now.tzinfo
        today = now.date()
        month_key = (now.year, now.month)
        stamps_today = self._day_hours(today, tz)
        if len(stamps_today) != len(context.price_today):
            return None
        current = _hour_key(now)
        remaining = [stamp for stamp in stamps_today if stamp >= current]
        shape = self._pv_shape(today, month_key, remaining)
        total = sum(shape)
        pv_total = context.forecast_remaining_kwh or 0.0
        slots = []
        for stamp, weight in zip(remaining, shape, strict=True):
            position = stamps_today.index(stamp)
            local_hour = stamp.astimezone(tz).hour
            pv = pv_total * weight / total if total > 0 else 0.0
            slots.append(Slot(stamp, 1.0, context.price_today[position], self._pv(stamp, pv),
                              self._load(stamp, month_key, local_hour)))
        if context.price_tomorrow and now.hour >= TOMORROW_KNOWN_FROM_HOUR:
            tomorrow = today + dt.timedelta(days=1)
            stamps = self._day_hours(tomorrow, tz)
            if len(stamps) == len(context.price_tomorrow):
                shape = self._pv_shape(tomorrow, month_key, stamps)
                total = sum(shape)
                pv_total = context.forecast_tomorrow_kwh or 0.0
                for stamp, weight, price in zip(stamps, shape, context.price_tomorrow, strict=True):
                    pv = pv_total * weight / total if total > 0 else 0.0
                    slots.append(Slot(stamp, 1.0, price, self._pv(stamp, pv),
                                      self._load(stamp, month_key, stamp.astimezone(tz).hour)))
        return slots

    def _plan_step(self, slots: list[Slot], soc: float) -> Any:
        """First step of the (trusted) plan, with variants A and B applied."""
        trusted = [Slot(s.start, s.hours, s.price_ct, s.pv_kwh * self.pv_trust, s.load_kwh) for s in slots]
        horizon = Horizon(trusted, self.battery, self.settings)
        horizon.solve(soc)
        self._horizon = horizon
        chosen, _ = horizon.recommend(soc)
        if chosen.source == "self_consumption":
            self.min_benefit_fallbacks += 1
        if not chosen.steps:
            return None
        if self.grid_check and self.pv_trust < 1 and chosen.first.grid_charge_kwh > 0:
            full = Horizon(slots, self.battery, self.settings)
            full.solve(soc)
            check, _ = full.recommend(soc)
            if check.steps and check.first.grid_charge_kwh <= 0:
                self.grid_checks_blocked += 1
                return check.first
        return chosen.first

    def decide(self, context: StepContext) -> Decision:
        self.steps += 1
        key = _hour_key(context.now)
        if key != self._plan_key:
            started = time.perf_counter()
            slots = self._slots(context)
            self._horizon = None
            step = None
            if slots:
                step = self._plan_step(slots, context.soc)
                self.plans += 1
            self._plan_key = key
            self.solve_seconds += time.perf_counter() - started
            self._held = (None if step is None
                          else _decision(step, "rh", f"Plan {self.forecast_mode}", self.params))
        if self._held is None:
            self.fallback_steps += 1
            return Decision(DYNAMIC, "rh_fallback", "keine Preisreihe oder kein zulaessiger Zug")
        return self._held

    def reset(self) -> None:
        self._plan_key = None
        self._horizon = None
        self._held = None

    def summary(self) -> dict[str, Any]:
        return {"planner": "receding_horizon", "forecast_mode": self.forecast_mode, "load_mode": self.load_mode,
                "pv_mode": self.pv_mode,
                "wear_ct_kwh": self.wear_ct, "terminal_factor": self.terminal_factor, "soc_step": self.soc_step,
                "min_benefit_ct": self.min_benefit_ct, "pv_trust": self.pv_trust, "grid_check": self.grid_check,
                "steps": self.steps, "plans": self.plans, "fallback_steps": self.fallback_steps,
                "min_benefit_fallbacks": self.min_benefit_fallbacks, "grid_checks_blocked": self.grid_checks_blocked,
                "solve_seconds": round(self.solve_seconds, 1)}
