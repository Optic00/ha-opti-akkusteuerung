"""Receding-horizon plan over the stored battery energy. Pure Python, no HA.

A dynamic programme over the stored energy decides per slot how much to
charge (from PV or the grid) and how much to discharge, given the known price
series, a PV forecast and a load forecast. The plan is observation material
for the shadow sensor and the replay harness; nothing here controls the
battery.

Physics follow ``tools/replay/battery.py``: losses per direction, the stored
energy rises by ``charge * eta_charge`` and falls by ``discharge / eta_discharge``,
the battery never exports and every slot satisfies
``pv + import + discharge == load + export + charge``.

Discretisation: value tables live on a SoC grid, the state itself does not.
Every action moves the stored energy by exactly the energy of its flows; the
value of a state between grid points is interpolated linearly. No action can
therefore deliver energy without lowering the state, store more than it
charged, or collect a terminal credit for energy it does not hold. The only
approximation is the interpolation of the value tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil, floor, inf, isfinite
from time import perf_counter
from typing import Any

DYNAMIC = "Akku Dynamisch"
PAUSE = "Akku Pause"
ONLY_CHARGE = "Akku nur Laden"
ONLY_DISCHARGE = "Akku nur Entladen"
GRID_CHARGE = "Akku Netzladen"
MODES = (DYNAMIC, PAUSE, ONLY_CHARGE, ONLY_DISCHARGE, GRID_CHARGE)
TERMINAL_MODES = ("value", "hold", "none")
MAX_HORIZON_HOURS = 36.0
# A planned flow below this share of what Dynamisch would do is classified as
# "not that" in the ladder vocabulary (diagnosis only, never execution).
MODE_SHARE = 0.5
TERMINAL_MARGIN_CT = 0.01
# Grid charging below this share of the charge power in a PV slot is noise,
# not a decision; such moves are neither offered nor reported as Netzladen.
GRID_MIN_SHARE = 0.1
# Tie-break towards storing PV surplus as soon as it is there instead of a
# later slot of the same forecast: ct per kWh at the horizon start, fading to
# zero at its end. Far below any real price difference; it only decides ties,
# which otherwise bet on PV that the forecast may not deliver.
PV_EARLY_CT = 0.2
# Budget: the grid is coarsened beyond this many states, integer moves are
# thinned beyond this many per slot, and a solve past its deadline is abandoned.
MAX_STATES = 401
MAX_ACTIONS = 120
_EPS = 1e-9


class PlannerBudgetError(RuntimeError):
    """The solve exceeded its time budget."""


@dataclass(frozen=True)
class Battery:
    capacity_kwh: float
    minsoc: float
    maxsoc: float
    max_charge_w: float
    max_discharge_w: float
    eta_charge: float = 0.95
    eta_discharge: float = 0.95

    def problem(self) -> str | None:
        """Reason code for an unusable parameter set, or ``None``."""
        values = (self.capacity_kwh, self.minsoc, self.maxsoc, self.max_charge_w,
                  self.max_discharge_w, self.eta_charge, self.eta_discharge)
        if any(not isinstance(v, int | float) or isinstance(v, bool) or not isfinite(v) for v in values):
            return "battery_invalid"
        if self.capacity_kwh <= 0 or not 0 <= self.minsoc < self.maxsoc <= 100:
            return "battery_invalid"
        if self.max_charge_w < 0 or self.max_discharge_w < 0:
            return "battery_invalid"
        if not 0 < self.eta_charge <= 1 or not 0 < self.eta_discharge <= 1:
            return "battery_invalid"
        return None


@dataclass(frozen=True)
class Slot:
    start: datetime
    hours: float
    price_ct: float
    pv_kwh: float
    load_kwh: float


@dataclass(frozen=True)
class Settings:
    feed_in_ct: float = 0.0
    wear_ct_kwh: float = 3.0
    grid_charging: bool = True
    # value: leftover usable energy is worth terminal_factor x the horizon's mean
    # price (after discharge losses), capped so that grid charging only for the
    # credit never pays; hold: end state >= start state; none: 0.
    terminal: str = "value"
    terminal_factor: float = 0.8
    soc_step: float = 0.5
    # Variant A: the plan replaces self-consumption only when its expected
    # advantage over the horizon (after losses and wear) reaches this amount.
    min_benefit_ct: float = 0.0


@dataclass(frozen=True)
class Action:
    delta: float  # state move in grid units, exactly charge*eta/step - discharge/eta/step
    charge_kwh: float
    discharge_kwh: float
    cost_eur: float  # true cost of the slot
    objective_eur: float  # cost minus the early-PV tie-break, what the programme minimises


@dataclass(frozen=True)
class Step:
    slot: Slot
    soc_before: float
    soc_after: float
    charge_kwh: float
    discharge_kwh: float
    grid_charge_kwh: float
    import_kwh: float
    export_kwh: float
    cost_eur: float
    mode: str  # executable mode: with charge_w/discharge_w it reproduces the flows
    ladder_mode: str  # diagnosis in the ladder vocabulary (full flows only)
    charge_w: float | None  # planned charge power, None = unlimited
    discharge_w: float | None  # planned discharge power, None = unlimited
    # True when no mode could move energy in this slot (empty battery in a
    # deficit, full battery in a surplus): the recommendation is not a decision.
    indifferent: bool = False


@dataclass
class Plan:
    steps: list[Step]
    cost_eur: float  # operational cost over the horizon (import - export + wear)
    terminal_value_eur: float  # credit for usable energy left at the end
    soc_start: float
    source: str = "plan"  # plan | self_consumption (variant A fallback)

    @property
    def first(self) -> Step:
        return self.steps[0]

    @property
    def objective_eur(self) -> float:
        return self.cost_eur - self.terminal_value_eur

    def soc_path(self) -> list[float]:
        return [self.soc_start, *(step.soc_after for step in self.steps)]


def flows(slot: Slot, charge_kwh: float, discharge_kwh: float) -> tuple[float, float]:
    """(import, export) of a slot for the given AC-side battery flows."""
    net = slot.pv_kwh - slot.load_kwh - charge_kwh + discharge_kwh
    return (max(0.0, -net), max(0.0, net))


def slot_cost(slot: Slot, charge_kwh: float, discharge_kwh: float, settings: Settings) -> float:
    imported, exported = flows(slot, charge_kwh, discharge_kwh)
    return (imported * slot.price_ct - exported * settings.feed_in_ct
            + discharge_kwh * settings.wear_ct_kwh) / 100


def dynamic_flows(slot: Slot, battery: Battery, soc: float) -> tuple[float, float]:
    """What ``Akku Dynamisch`` would do in this slot from ``soc`` (charge, discharge)."""
    stored = soc / 100 * battery.capacity_kwh
    room = max(0.0, battery.maxsoc / 100 * battery.capacity_kwh - stored)
    available = max(0.0, stored - battery.minsoc / 100 * battery.capacity_kwh)
    surplus = slot.pv_kwh - slot.load_kwh
    if surplus > 0:
        return min(surplus, battery.max_charge_w * slot.hours / 1000, room / battery.eta_charge), 0.0
    if surplus < 0:
        return 0.0, min(-surplus, battery.max_discharge_w * slot.hours / 1000, available * battery.eta_discharge)
    return 0.0, 0.0


def _grid_kwh(slot: Slot, charge_kwh: float) -> float:
    return max(0.0, charge_kwh - max(0.0, slot.pv_kwh - slot.load_kwh))


def executable_mode(slot: Slot, charge_kwh: float, discharge_kwh: float) -> str:
    """Mode that, with the planned power setpoints, reproduces the flows exactly."""
    if _grid_kwh(slot, charge_kwh) > _EPS:
        return GRID_CHARGE
    surplus = slot.pv_kwh - slot.load_kwh
    if surplus > 0:
        return DYNAMIC if charge_kwh > _EPS else ONLY_DISCHARGE
    if surplus < 0:
        return DYNAMIC if discharge_kwh > _EPS else ONLY_CHARGE
    return DYNAMIC


def ladder_mode(slot: Slot, battery: Battery, soc: float, charge_kwh: float, discharge_kwh: float) -> str:
    """Diagnosis in the ladder's vocabulary: the mode whose full flow comes closest."""
    surplus = slot.pv_kwh - slot.load_kwh
    grid_min = GRID_MIN_SHARE * battery.max_charge_w * slot.hours / 1000
    grid = _grid_kwh(slot, charge_kwh)
    if grid > _EPS and grid >= min(grid_min, charge_kwh) - _EPS:
        return GRID_CHARGE
    dyn_charge, dyn_discharge = dynamic_flows(slot, battery, soc)
    if surplus > 0:
        return DYNAMIC if charge_kwh >= dyn_charge * MODE_SHARE - _EPS else ONLY_DISCHARGE
    if surplus < 0:
        return DYNAMIC if discharge_kwh >= dyn_discharge * MODE_SHARE - _EPS else ONLY_CHARGE
    return DYNAMIC


def interpolate(table: list[float], position: float) -> float:
    """Linear interpolation of a value table at a fractional grid position."""
    k = floor(position)
    if k >= len(table) - 1:
        return table[-1]
    if k < 0:
        return table[0]
    f = position - k
    if f <= _EPS:
        return table[k]
    low, high = table[k], table[k + 1]
    if low == inf or high == inf:
        return inf
    return (1 - f) * low + f * high


class Horizon:
    """Value tables for one slot sequence; walks from any start SoC are cheap."""

    def __init__(self, slots: list[Slot], battery: Battery, settings: Settings) -> None:
        if (problem := battery.problem()) is not None:
            raise ValueError(problem)
        if not slots:
            raise ValueError("no_slots")
        if settings.terminal not in TERMINAL_MODES:
            raise ValueError("terminal_invalid")
        span = battery.maxsoc - battery.minsoc
        if not 0 < settings.soc_step <= span:
            raise ValueError("soc_step_invalid")
        self.slots = list(slots)
        self.battery = battery
        self.settings = settings
        step = max(settings.soc_step, span / (MAX_STATES - 1))
        self.states = int(round(span / step)) + 1
        self.step_pct = span / (self.states - 1)
        self.step_kwh = self.step_pct / 100 * battery.capacity_kwh
        self.terminal_ct = self._terminal_ct()
        self.actions = [self._actions(slot, t) for t, slot in enumerate(self.slots)]
        self.values: list[list[float]] = []
        self.start_position: float | None = None

    # -- discretisation -------------------------------------------------
    def position(self, soc: float) -> float:
        raw = (soc - self.battery.minsoc) / self.step_pct
        return min(self.states - 1.0, max(0.0, raw))

    def soc(self, position: float) -> float:
        return self.battery.minsoc + position * self.step_pct

    def _actions(self, slot: Slot, t: int) -> list[Action]:
        b, s = self.battery, self.settings
        early = PV_EARLY_CT * (len(self.slots) - t) / len(self.slots) / 100
        surplus = max(0.0, slot.pv_kwh - slot.load_kwh)
        deficit = max(0.0, slot.load_kwh - slot.pv_kwh)
        cap_charge = b.max_charge_w * slot.hours / 1000
        cap_discharge = b.max_discharge_w * slot.hours / 1000
        pv_charge_max = min(surplus, cap_charge)
        charge_max = cap_charge if s.grid_charging else pv_charge_max
        discharge_max = min(deficit, cap_discharge)
        grid_min = GRID_MIN_SHARE * cap_charge
        candidates: dict[tuple[float, float], Action] = {}

        def add(charge: float, discharge: float) -> None:
            if _EPS < charge - pv_charge_max < grid_min:
                return  # rounding-sized grid top-up in a PV slot
            key = (round(charge, 9), round(discharge, 9))
            if key not in candidates:
                delta = (charge * b.eta_charge - discharge / b.eta_discharge) / self.step_kwh
                cost = slot_cost(slot, charge, discharge, s)
                candidates[key] = Action(delta, charge, discharge, cost, cost - early * min(charge, pv_charge_max))

        add(0.0, 0.0)  # standing still is always possible, whatever the thinning below drops
        up = floor(charge_max * b.eta_charge / self.step_kwh + _EPS)
        down = floor(discharge_max / b.eta_discharge / self.step_kwh + _EPS)
        stride = max(1, ceil((up + down + 1) / MAX_ACTIONS))
        for offset in [*range(-stride, -down - 1, -stride), *range(stride, up + 1, stride)]:
            charge = offset * self.step_kwh / b.eta_charge if offset > 0 else 0.0
            discharge = -offset * self.step_kwh * b.eta_discharge if offset < 0 else 0.0
            add(charge, discharge)
        # Exact flows: absorb the whole surplus, full grid charge, cover the whole deficit.
        for charge in (pv_charge_max, charge_max):
            if charge > _EPS:
                add(charge, 0.0)
        if discharge_max > _EPS:
            add(0.0, discharge_max)
        return sorted(candidates.values(), key=lambda a: a.delta)

    def _terminal_ct(self) -> float:
        """Credit per usable kWh at the horizon end.

        A share of the horizon's mean price, as the energy displaces imports at
        an unknown later price. Capped strictly below the cost of refilling from
        the grid in the cheapest slot (losses in both directions), so the
        programme never buys grid energy just to collect the credit.
        """
        if self.settings.terminal != "value":
            return 0.0
        hours = sum(slot.hours for slot in self.slots)
        mean = sum(slot.price_ct * slot.hours for slot in self.slots) / hours if hours > 0 else 0.0
        cheapest = min(slot.price_ct for slot in self.slots)
        refill = cheapest / (self.battery.eta_charge * self.battery.eta_discharge) - TERMINAL_MARGIN_CT
        return max(0.0, min(mean * self.settings.terminal_factor, refill))

    def terminal_values(self, start_position: float) -> list[float]:
        if self.settings.terminal == "hold":
            threshold = ceil(start_position - 1e-6)
            return [0.0 if i >= threshold else inf for i in range(self.states)]
        if self.settings.terminal == "none":
            return [0.0] * self.states
        value = self.step_kwh * self.battery.eta_discharge * self.terminal_ct / 100
        return [-i * value for i in range(self.states)]

    # -- dynamic programme ---------------------------------------------
    def _stage(self, t: int, nxt: list[float]) -> list[float]:
        """Best cost-to-go for every grid state at slot ``t`` given ``nxt``."""
        n = self.states
        best = [inf] * n
        for action in self.actions[t]:
            d, cost = action.delta, action.objective_eur
            lo = max(0, ceil(-d - _EPS))
            hi = min(n - 1, floor(n - 1 - d + _EPS))
            if lo > hi:
                continue
            k = floor(d + _EPS)
            f = d - k
            if f <= _EPS:
                candidate = [cost + v for v in nxt[lo + k:hi + k + 1]]
            else:
                w0, w1 = 1 - f, f
                candidate = [cost + (w0 * a + w1 * b if a != inf and b != inf else inf)
                             for a, b in zip(nxt[lo + k:hi + k + 1], nxt[lo + k + 1:hi + k + 2], strict=True)]
            best[lo:hi + 1] = map(min, best[lo:hi + 1], candidate, strict=True)
        return best

    def solve(self, start_soc: float, deadline_s: float | None = None) -> float:
        """Fill ``values[t][i]`` = best cost from grid state ``i`` at slot ``t`` to the end.

        ``deadline_s`` is the budget for this solve; it is checked before and
        after every stage, and the elapsed time is returned.
        """
        started = perf_counter()
        self.start_position = self.position(start_soc)
        tables: list[list[float]] = [[]] * (len(self.slots) + 1)
        tables[-1] = self.terminal_values(self.start_position)
        for t in range(len(self.slots) - 1, -1, -1):
            tables[t] = self._stage(t, tables[t + 1])
            if deadline_s is not None and perf_counter() - started > deadline_s:
                self.values = []
                raise PlannerBudgetError("deadline")
        self.values = tables
        return perf_counter() - started

    def resolve_first(self, slot: Slot, deadline_s: float | None = None) -> float:
        """Replace the running first slot (shorter now) and redo only its stage.

        Counts against the same budget as ``solve``; returns the elapsed time.
        """
        if not self.values:
            raise ValueError("unsolved")
        started = perf_counter()
        self.slots[0] = slot
        self.actions[0] = self._actions(slot, 0)
        self.values[0] = self._stage(0, self.values[1])
        elapsed = perf_counter() - started
        if deadline_s is not None and elapsed > deadline_s:
            self.values = []
            raise PlannerBudgetError("deadline")
        return elapsed

    def choose(self, t: int, position: float) -> Action | None:
        """Best action at slot ``t`` from the exact state ``position``."""
        nxt = self.values[t + 1]
        best, chosen = inf, None
        for action in self.actions[t]:
            q = position + action.delta
            if -_EPS <= q <= self.states - 1 + _EPS:
                total = action.objective_eur + interpolate(nxt, min(self.states - 1.0, max(0.0, q)))
                if total < best:
                    best, chosen = total, action
        return chosen

    def step(self, t: int, position: float, action: Action) -> Step:
        slot = self.slots[t]
        imported, exported = flows(slot, action.charge_kwh, action.discharge_kwh)
        soc = self.soc(position)
        mode = executable_mode(slot, action.charge_kwh, action.discharge_kwh)
        charge_w = action.charge_kwh / slot.hours * 1000 if action.charge_kwh > _EPS and slot.hours > 0 else None
        discharge_w = (action.discharge_kwh / slot.hours * 1000
                       if action.discharge_kwh > _EPS and slot.hours > 0 else None)
        indifferent = (action.charge_kwh <= _EPS and action.discharge_kwh <= _EPS
                       and dynamic_flows(slot, self.battery, soc) == (0.0, 0.0))
        return Step(slot, soc, self.soc(position + action.delta), action.charge_kwh, action.discharge_kwh,
                    _grid_kwh(slot, action.charge_kwh), imported, exported, action.cost_eur, mode,
                    ladder_mode(slot, self.battery, soc, action.charge_kwh, action.discharge_kwh),
                    charge_w, discharge_w, indifferent)

    def walk(self, start_soc: float) -> Plan:
        """Forward pass from ``start_soc`` over the solved tables."""
        if not self.values:
            self.solve(start_soc)
        position = self.position(start_soc)
        steps: list[Step] = []
        cost = 0.0
        for t in range(len(self.slots)):
            action = self.choose(t, position)
            if action is None:
                break
            step = self.step(t, position, action)
            steps.append(step)
            cost += step.cost_eur
            position = min(self.states - 1.0, max(0.0, position + action.delta))
        terminal = interpolate(self.values[-1], position) if steps else 0.0
        return Plan(steps, cost, -terminal if isfinite(terminal) else 0.0, self.soc(self.position(start_soc)))

    def first(self, soc: float) -> Step | None:
        """Only the next move from ``soc``; used between full re-plans."""
        if not self.values:
            self.solve(soc)
        action = self.choose(0, self.position(soc))
        return None if action is None else self.step(0, self.position(soc), action)

    def self_consumption(self, start_soc: float) -> Plan:
        """``Akku Dynamisch`` throughout, with the same forecasts and terminal credit."""
        position = self.position(start_soc)
        steps: list[Step] = []
        cost = 0.0
        for t, slot in enumerate(self.slots):
            charge, discharge = dynamic_flows(slot, self.battery, self.soc(position))
            delta = (charge * self.battery.eta_charge - discharge / self.battery.eta_discharge) / self.step_kwh
            action = Action(delta, charge, discharge, slot_cost(slot, charge, discharge, self.settings), 0.0)
            step = self.step(t, position, action)
            steps.append(step)
            cost += step.cost_eur
            position = min(self.states - 1.0, max(0.0, position + delta))
        terminal = 0.0
        if self.settings.terminal == "value":
            terminal = position * self.step_kwh * self.battery.eta_discharge * self.terminal_ct / 100
        return Plan(steps, cost, terminal, self.soc(self.position(start_soc)), "self_consumption")

    def recommend(self, soc: float) -> tuple[Plan, Plan]:
        """(recommended plan, self-consumption plan); variant A falls back to the latter.

        The recommended plan is the optimum unless its advantage over
        self-consumption stays below ``min_benefit_ct``.
        """
        optimum = self.walk(soc)
        baseline = self.self_consumption(soc)
        if (optimum.steps and self.settings.min_benefit_ct > 0
                and (baseline.objective_eur - optimum.objective_eur) * 100 < self.settings.min_benefit_ct):
            return baseline, baseline
        return optimum, baseline


def plan(slots: list[Slot], battery: Battery, settings: Settings, soc: float) -> Plan:
    horizon = Horizon(slots, battery, settings)
    horizon.solve(soc)
    return horizon.walk(soc)


def baseline_cost(slots: list[Slot], battery: Battery, settings: Settings, soc: float) -> float:
    """Objective of ``Akku Dynamisch`` throughout (same forecasts, same terminal credit)."""
    return Horizon(slots, battery, settings).self_consumption(soc).objective_eur


def describe(value: Any) -> dict[str, Any]:
    """Compact JSON-safe view of a plan for attributes and reports."""
    if not isinstance(value, Plan):
        return {}
    return {
        "cost_eur": round(value.cost_eur, 3),
        "terminal_value_eur": round(value.terminal_value_eur, 3),
        "grid_charge_kwh": round(sum(step.grid_charge_kwh for step in value.steps), 3),
        "discharge_kwh": round(sum(step.discharge_kwh for step in value.steps), 3),
        "modes": [step.mode for step in value.steps],
        "source": value.source,
    }
