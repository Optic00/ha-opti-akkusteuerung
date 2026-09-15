"""Small PV-only preparation policy; no charger service calls or grid charging."""

from dataclasses import replace
from datetime import datetime
import json

from .sources import finite


LOW_PRIORITY = frozenset({"above_target", "below_target", "default", "surplus_veto"})


def signals(cfg, states, now):
    """Flags are authoritative until unavailable; SOC expires after 24 hours."""
    soc_state = states.get(cfg.get("vehicle_soc", ""))
    charge = states.get(cfg.get("charging", ""))
    away = states.get(cfg.get("away", "")) if cfg.get("away") else None
    soc = finite(soc_state.state) if soc_state else None
    try:
        stamp = soc_state.last_reported or soc_state.last_updated
        fresh = (
            isinstance(stamp, datetime)
            and stamp.utcoffset() is not None
            and -60 <= (now - stamp).total_seconds() <= 86400
        )
    except AttributeError, TypeError:
        fresh = False
    if (
        soc is None
        or not 0 <= soc <= 100
        or not fresh
        or soc_state.attributes.get("unit_of_measurement") != "%"
    ):
        soc = None
    charging = {"on": True, "off": False}.get(charge.state) if charge else None
    absent = (
        {"on": True, "off": False}.get(away.state) if away else (None if cfg.get("away") else False)
    )
    return soc, charging, absent


def command_signals(cfg, states, now):
    """Invalidate on validity/decision boundaries, not irrelevant SOC ticks.

    The three valid regions preserve both edges of the demand hysteresis.
    Flags and freshness remain part of every pre-write check.
    """
    soc, charging, away = signals(cfg, states, now)
    threshold = finite(cfg.get("vehicle_threshold", 40))
    region = "invalid"
    if soc is not None and threshold is not None and 0 <= threshold <= 95:
        region = "need" if soc < threshold else "satisfied" if soc >= min(100, threshold + 5) else "band"
    return region, charging, away


class EVPreparation:
    def __init__(self):
        self.binding = None
        self.demand = False
        self.full = False
        self.since = None
        self.previous = None

    def update(self, cfg, states, now, measurements, maximum, eligible, can_prepare=True):
        out = {
            "status": "disabled",
            "ready": False,
            "charging_guard": False,
            "controls_battery": False,
        }
        if cfg.get("enabled") is not True or not eligible:
            self.__init__()
            return out
        binding = json.dumps([cfg, maximum], sort_keys=True)
        if binding != self.binding or (
            self.previous and not 0 <= (now - self.previous).total_seconds() <= 90
        ):
            self.__init__()
            self.binding = binding
        self.previous = now
        soc, charging, away = signals(cfg, states, now)
        out.update(charging_guard=charging is not False, vehicle_soc=soc)
        if charging is not False:
            self.since = None
            return {**out, "status": "car_charging" if charging else "data_missing"}
        if not can_prepare:
            self.since = None
            return {**out, "status": "waiting_surplus"}
        if away is not False or soc is None:
            self.since = None
            self.demand = False
            return {**out, "status": "away" if away else "data_missing"}
        threshold = finite(cfg.get("vehicle_threshold", 40))
        target = finite(cfg.get("house_target", 80))
        if (
            threshold is None
            or not 0 <= threshold <= 95
            or target is None
            or not 50 <= target <= 95
            or maximum is None
        ):
            self.since = None
            return {**out, "status": "data_missing"}
        target = min(target, maximum)
        out["target_soc"] = target
        if soc < threshold:
            self.demand = True
        elif soc >= min(100, threshold + 5):
            self.demand = False
        house = finite(measurements.get("sensor.opti_soc"))
        export = finite(measurements.get("sensor.opti_grid_export_w"))
        grid_import = finite(measurements.get("sensor.opti_grid_import_w"))
        battery = finite(measurements.get("sensor.opti_battery_power_w"))
        if None in (house, export, grid_import, battery):
            self.since = None
            return {**out, "status": "data_missing"}
        if house >= target:
            self.full = True
        elif house <= target - 2:
            self.full = False
        if not self.demand or self.full:
            self.since = None
            return {**out, "status": "target_reached" if self.full else "no_demand"}
        # Include current battery charging. Do not mistake discharge for PV.
        surplus = max(0.0, export - grid_import + battery)
        out["surplus_before_battery_w"] = surplus
        sufficient = surplus >= (100 if self.since else 300)
        if not sufficient:
            self.since = None
            return {**out, "status": "waiting_surplus"}
        self.since = self.since or now
        ready = (now - self.since).total_seconds() >= 60
        return {**out, "status": "ready" if ready else "waiting_surplus", "ready": ready}


def apply_preparation(result, preparation):
    """Extend ordinary surplus behavior, retaining higher-priority decisions."""
    mode, reason = result.mode, result.reason
    ordinary = result.decision_id in LOW_PRIORITY
    if preparation.get("charging_guard"):
        # Stop ordinary PV competition and all automatic discharge into the EV.
        # Explicit higher-priority charging (minimum SoC/balancing) is retained.
        if ordinary or mode in (
            "Akku Dynamisch",
            "Akku nur Entladen",
            "Akku schnell Entladen",
            "Akku Automatisch",
        ):
            mode, reason = "Akku Pause", "Auto-Vorrang (Ladung aktiv oder Ladesignal unbekannt)"
    elif preparation.get("ready") and ordinary:
        mode, reason = (
            "Akku nur Laden",
            f"Auto-Vorbereitung (PV bis {preparation['target_soc']:g}%)",
        )
    effective = (mode, reason) != (result.mode, result.reason)
    report = {**preparation, "controls_battery": effective}
    if preparation.get("ready"):
        report["status"] = "preparing" if effective else "higher_priority"
    if not effective:
        return result, report
    values = dict(result.states)
    values["sensor.opti_strategie_vorschau"] = mode
    attrs = {
        **result.attributes,
        "sensor.opti_strategie_vorschau": {
            **result.attributes.get("sensor.opti_strategie_vorschau", {}),
            "grund": reason,
        },
    }
    return replace(result, mode=mode, reason=reason, states=values, attributes=attrs,
                   decision_id="ev_priority" if preparation.get("charging_guard") else "ev_preparation"), report
