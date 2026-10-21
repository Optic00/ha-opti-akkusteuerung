"""Battery energy model with the adapter's mode semantics (see ``sma.py``).

Energies are AC-side kWh per step. Losses are applied per direction, so the
stored energy rises by ``charge * eta_charge`` and falls by
``discharge / eta_discharge``. Every step satisfies
``pv + import + discharge == load + export + charge``.
"""
from __future__ import annotations

from dataclasses import dataclass

DYNAMIC = "Akku Dynamisch"
PAUSE = "Akku Pause"
ONLY_CHARGE = "Akku nur Laden"
ONLY_DISCHARGE = "Akku nur Entladen"
GRID_CHARGE = "Akku Netzladen"
MODES = (DYNAMIC, PAUSE, ONLY_CHARGE, ONLY_DISCHARGE, GRID_CHARGE)


@dataclass(frozen=True)
class BatteryParams:
    capacity_kwh: float
    minsoc: float
    maxsoc: float
    max_charge_w: float
    max_discharge_w: float
    eta_charge: float = 0.95
    eta_discharge: float = 0.95

    @classmethod
    def from_plant(cls, plant: dict, **overrides) -> BatteryParams:
        values = {
            "capacity_kwh": float(plant["capacity_kwh"]),
            "minsoc": float(plant.get("minsoc", 5)),
            "maxsoc": float(plant.get("maxsoc", 95)),
            "max_charge_w": float(plant.get("max_charge_w", 3000)),
            "max_discharge_w": float(plant.get("max_discharge_w", 5000)),
        }
        values.update(overrides)
        return cls(**values)


@dataclass(frozen=True)
class Flows:
    charge_kwh: float
    discharge_kwh: float
    export_kwh: float
    import_kwh: float
    soc_after: float


def step(
    mode: str,
    soc: float,
    pv_kwh: float,
    load_kwh: float,
    hours: float,
    params: BatteryParams,
    charge_limit_w: float | None = None,
    discharge_limit_w: float | None = None,
) -> Flows:
    """Apply one step of ``hours`` duration in ``mode`` and return the flows.

    ``charge_limit_w`` is the strategy's charge power (``opti_charge_power_w``);
    it caps charging in Dynamisch, nur Laden and Netzladen like the adapter.
    ``discharge_limit_w`` caps discharging the same way (the adapter's
    discharge setpoint); the planner policies use it for partial moves.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}")
    stored = soc / 100 * params.capacity_kwh
    room = max(0.0, params.maxsoc / 100 * params.capacity_kwh - stored)
    available = max(0.0, stored - params.minsoc / 100 * params.capacity_kwh)
    power_cap = params.max_charge_w if charge_limit_w is None else min(params.max_charge_w, max(0.0, charge_limit_w))
    discharge_power = (params.max_discharge_w if discharge_limit_w is None
                       else min(params.max_discharge_w, max(0.0, discharge_limit_w)))
    charge_cap = min(power_cap * hours / 1000, room / params.eta_charge)
    discharge_cap = min(discharge_power * hours / 1000, available * params.eta_discharge)
    surplus = pv_kwh - load_kwh
    charge = discharge = 0.0
    if mode == GRID_CHARGE:
        charge = charge_cap
    elif surplus > 0 and mode in (DYNAMIC, ONLY_CHARGE):
        charge = min(surplus, charge_cap)
    elif surplus < 0 and mode in (DYNAMIC, ONLY_DISCHARGE):
        discharge = min(-surplus, discharge_cap)
    net = surplus - charge + discharge
    export_kwh, import_kwh = (net, 0.0) if net >= 0 else (0.0, -net)
    stored += charge * params.eta_charge - discharge / params.eta_discharge
    return Flows(charge, discharge, export_kwh, import_kwh, stored / params.capacity_kwh * 100)
