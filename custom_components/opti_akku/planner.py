"""Receding-horizon plan over the stored battery energy. Pure Python, no HA.

A dynamic programme over the stored energy on a SoC grid decides per slot how
much to charge (from PV or the grid) and how much to discharge, given the
known price series, a PV forecast and a load forecast. The plan is observation
material for the shadow sensor and the replay harness; nothing here controls
the battery.

Physics follow ``tools/replay/battery.py``: losses per direction, the stored
energy rises by ``charge * eta_charge`` and falls by ``discharge / eta_discharge``,
the battery never exports and every slot satisfies
``pv + import + discharge == load + export + charge``.

Discretisation: the state is the grid index above MinSOC. Grid actions move by
whole steps. "Exact" actions (absorb the whole surplus, cover the whole deficit,
full grid charge) use the true energy for the costs and round their state move
to the nearest grid point, so the plan is not biased towards partial moves
that leave a remainder to the grid. The bookkeeping error is at most half a
step per slot and never accumulates: every real decision is re-read from the
tables with the measured SoC, and the costs that matter are those of the
executed flows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import floor, inf, isfinite
from typing import Any

DYNAMIC = "Akku Dynamisch"
PAUSE = "Akku Pause"
ONLY_CHARGE = "Akku nur Laden"
ONLY_DISCHARGE = "Akku nur Entladen"
GRID_CHARGE = "Akku Netzladen"
MODES = (DYNAMIC, PAUSE, ONLY_CHARGE, ONLY_DISCHARGE, GRID_CHARGE)
TERMINAL_MODES = ("value", "hold", "none")
MAX_HORIZON_HOURS = 36.0
# A planned flow below this share of what Dynamisch would do counts as "not that".
MODE_SHARE = 0.5
# Grid charging below this share of the charge power is rounding noise in a
# PV slot, not a decision; such moves are neither offered nor reported as Netzladen.
GRID_MIN_SHARE = 0.1
TERMINAL_MARGIN_CT = 0.01
_EPS = 1e-9


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


@dataclass(frozen=True)
class Action:
    offset: int
    charge_kwh: float
    discharge_kwh: float
    cost_eur: float


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
    mode: str
    charge_limit_w: float | None
    # True when no mode could move energy in this slot (empty battery in a
    # deficit, full battery in a surplus): the recommendation is not a decision.
    indifferent: bool = False


@dataclass
class Plan:
    steps: list[Step]
    cost_eur: float  # operational cost over the horizon (import - export + wear)
    terminal_value_eur: float  # credit for usable energy left at the end
    soc_start: float

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


def mode_for(slot: Slot, battery: Battery, soc: float, charge_kwh: float, discharge_kwh: float) -> str:
    """Translate planned flows into the ladder's mode vocabulary for this slot."""
    surplus = slot.pv_kwh - slot.load_kwh
    grid_min = GRID_MIN_SHARE * battery.max_charge_w * slot.hours / 1000
    if charge_kwh - max(0.0, surplus) >= min(grid_min, charge_kwh) - _EPS and charge_kwh > max(0.0, surplus) + _EPS:
        return GRID_CHARGE
    dyn_charge, dyn_discharge = dynamic_flows(slot, battery, soc)
    if surplus > 0:
        return DYNAMIC if charge_kwh >= dyn_charge * MODE_SHARE - _EPS else ONLY_DISCHARGE
    if surplus < 0:
        return DYNAMIC if discharge_kwh >= dyn_discharge * MODE_SHARE - _EPS else ONLY_CHARGE
    return DYNAMIC


class Horizon:
    """Value tables for one slot sequence; walks from any start SoC are cheap."""

    def __init__(self, slots: list[Slot], battery: Battery, settings: Settings) -> None:
        if (problem := battery.problem()) is not None:
            raise ValueError(problem)
        if not slots:
            raise ValueError("no_slots")
        if settings.terminal not in TERMINAL_MODES:
            raise ValueError("terminal_invalid")
        if not 0 < settings.soc_step <= battery.maxsoc - battery.minsoc:
            raise ValueError("soc_step_invalid")
        self.slots = slots
        self.battery = battery
        self.settings = settings
        self.states = int(round((battery.maxsoc - battery.minsoc) / settings.soc_step)) + 1
        self.step_pct = (battery.maxsoc - battery.minsoc) / (self.states - 1)
        self.step_kwh = self.step_pct / 100 * battery.capacity_kwh
        self.actions = [self._actions(slot) for slot in slots]
        self.values: list[list[float]] = []
        self.terminal_ct = self._terminal_ct()

    # -- discretisation -------------------------------------------------
    def index(self, soc: float) -> int:
        raw = int(round((soc - self.battery.minsoc) / self.step_pct))
        return min(self.states - 1, max(0, raw))

    def soc(self, index: int) -> float:
        return self.battery.minsoc + index * self.step_pct

    def _actions(self, slot: Slot) -> list[Action]:
        b, s = self.battery, self.settings
        surplus = max(0.0, slot.pv_kwh - slot.load_kwh)
        deficit = max(0.0, slot.load_kwh - slot.pv_kwh)
        cap_charge = b.max_charge_w * slot.hours / 1000
        cap_discharge = b.max_discharge_w * slot.hours / 1000
        pv_charge_max = min(surplus, cap_charge)
        charge_max = cap_charge if s.grid_charging else pv_charge_max
        discharge_max = min(deficit, cap_discharge)
        candidates: dict[tuple[int, float, float], Action] = {}

        def add(offset: int, charge: float, discharge: float) -> None:
            key = (offset, round(charge, 9), round(discharge, 9))
            if key not in candidates:
                candidates[key] = Action(offset, charge, discharge, slot_cost(slot, charge, discharge, s))

        grid_min = GRID_MIN_SHARE * cap_charge
        up = floor(charge_max * b.eta_charge / self.step_kwh + _EPS)
        down = floor(discharge_max / b.eta_discharge / self.step_kwh + _EPS)
        for offset in range(-down, up + 1):
            charge = offset * self.step_kwh / b.eta_charge if offset > 0 else 0.0
            discharge = -offset * self.step_kwh * b.eta_discharge if offset < 0 else 0.0
            if _EPS < charge - pv_charge_max < grid_min:
                continue
            add(offset, charge, discharge)
        # Exact flows with the state move rounded to the nearest grid point.
        for charge in (pv_charge_max, charge_max):
            if charge > _EPS and not _EPS < charge - pv_charge_max < grid_min:
                add(int(round(charge * b.eta_charge / self.step_kwh)), charge, 0.0)
        if discharge_max > _EPS:
            add(-int(round(discharge_max / b.eta_discharge / self.step_kwh)), 0.0, discharge_max)
        return sorted(candidates.values(), key=lambda a: a.offset)

    def _terminal_ct(self) -> float:
        """Credit per usable kWh at the horizon end.

        A share of the horizon's mean price, as the energy displaces imports at
        an unknown later price. Capped at the cost of refilling from the grid in
        the cheapest slot (losses in both directions), so the programme never
        buys grid energy just to collect the credit.
        """
        if self.settings.terminal != "value":
            return 0.0
        hours = sum(slot.hours for slot in self.slots)
        mean = sum(slot.price_ct * slot.hours for slot in self.slots) / hours if hours > 0 else 0.0
        cheapest = min(slot.price_ct for slot in self.slots)
        # Strictly below the refill cost, so a tie never tips towards buying.
        refill = cheapest / (self.battery.eta_charge * self.battery.eta_discharge) - TERMINAL_MARGIN_CT
        return max(0.0, min(mean * self.settings.terminal_factor, refill))

    def terminal_values(self, start_index: int) -> list[float]:
        if self.settings.terminal == "hold":
            return [0.0 if i >= start_index else inf for i in range(self.states)]
        if self.settings.terminal == "none":
            return [0.0] * self.states
        value = self.step_kwh * self.battery.eta_discharge * self.terminal_ct / 100
        return [-i * value for i in range(self.states)]

    # -- dynamic programme ---------------------------------------------
    def solve(self, start_soc: float) -> None:
        """Fill ``values[t][i]`` = best cost from state ``i`` at slot ``t`` to the end."""
        n = self.states
        tables: list[list[float]] = [[]] * (len(self.slots) + 1)
        tables[-1] = self.terminal_values(self.index(start_soc))
        for t in range(len(self.slots) - 1, -1, -1):
            nxt = tables[t + 1]
            best = [inf] * n
            for action in self.actions[t]:
                d, cost = action.offset, action.cost_eur
                lo, hi = max(0, -d), min(n, n - d)
                if lo >= hi:
                    continue
                candidate = [cost + v for v in nxt[lo + d:hi + d]]
                best[lo:hi] = map(min, best[lo:hi], candidate, strict=True)
            tables[t] = best
        self.values = tables

    def choose(self, t: int, index: int) -> Action | None:
        """Best action at slot ``t`` from state ``index`` using the solved tables."""
        nxt = self.values[t + 1]
        best, chosen = inf, None
        for action in self.actions[t]:
            j = index + action.offset
            if 0 <= j < self.states and (total := action.cost_eur + nxt[j]) < best:
                best, chosen = total, action
        return chosen

    def step(self, t: int, index: int, action: Action) -> Step:
        slot = self.slots[t]
        imported, exported = flows(slot, action.charge_kwh, action.discharge_kwh)
        soc = self.soc(index)
        surplus = max(0.0, slot.pv_kwh - slot.load_kwh)
        grid = max(0.0, action.charge_kwh - surplus)
        mode = mode_for(slot, self.battery, soc, action.charge_kwh, action.discharge_kwh)
        limit = action.charge_kwh / slot.hours * 1000 if mode == GRID_CHARGE and slot.hours > 0 else None
        indifferent = (mode != GRID_CHARGE and action.charge_kwh == 0 and action.discharge_kwh == 0
                       and dynamic_flows(slot, self.battery, soc) == (0.0, 0.0))
        return Step(slot, soc, self.soc(index + action.offset), action.charge_kwh, action.discharge_kwh,
                    grid, imported, exported, action.cost_eur, mode, limit, indifferent)

    def walk(self, start_soc: float) -> Plan:
        """Forward pass from ``start_soc`` over the solved tables."""
        if not self.values:
            self.solve(start_soc)
        index = self.index(start_soc)
        steps: list[Step] = []
        cost = 0.0
        for t in range(len(self.slots)):
            action = self.choose(t, index)
            if action is None:
                break
            step = self.step(t, index, action)
            steps.append(step)
            cost += step.cost_eur
            index += action.offset
        terminal = self.values[-1][index] if steps else 0.0
        return Plan(steps, cost, -terminal if isfinite(terminal) else 0.0, self.soc(self.index(start_soc)))

    def first(self, soc: float) -> Step | None:
        """Only the next move from ``soc``; used between full re-plans."""
        if not self.values:
            self.solve(soc)
        action = self.choose(0, self.index(soc))
        return None if action is None else self.step(0, self.index(soc), action)


def plan(slots: list[Slot], battery: Battery, settings: Settings, soc: float) -> Plan:
    horizon = Horizon(slots, battery, settings)
    horizon.solve(soc)
    return horizon.walk(soc)


def baseline_cost(slots: list[Slot], battery: Battery, settings: Settings, soc: float,
                  terminal_ct: float | None = None) -> float:
    """Cost of ``Akku Dynamisch`` throughout, with the same forecasts and terminal credit."""
    total = 0.0
    stored = min(battery.maxsoc, max(battery.minsoc, soc)) / 100 * battery.capacity_kwh
    for slot in slots:
        charge, discharge = dynamic_flows(slot, battery, stored / battery.capacity_kwh * 100)
        total += slot_cost(slot, charge, discharge, settings)
        stored += charge * battery.eta_charge - discharge / battery.eta_discharge
    if settings.terminal == "value":
        if terminal_ct is None:
            terminal_ct = Horizon(slots, battery, settings).terminal_ct
        usable = max(0.0, stored - battery.minsoc / 100 * battery.capacity_kwh)
        total -= usable * battery.eta_discharge * terminal_ct / 100
    return total


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
    }
