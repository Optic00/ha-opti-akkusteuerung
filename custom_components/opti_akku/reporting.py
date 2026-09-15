"""Bounded, read-only explanations and observed operating evidence.

Nothing in this module is a control input. Energy is a sampled estimate, never
an inverter counter. Missing intervals are reported, not extrapolated.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from math import isfinite

MAX_INTERVAL = 90


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def reserve_plan(data: dict, settings: dict, now: datetime, *, shadow: bool) -> dict:
    states = data.get("states", {})
    attrs = data.get("attributes", {}).get("sensor.opti_peak_reserve_soc", {})
    total = number(states.get("sensor.opti_peak_reserve_soc"))
    priority = number(attrs.get("reserve_ve_soc"))
    try:
        horizon = datetime.fromisoformat(attrs.get("horizont_ende", ""))
        valid_horizon = horizon.tzinfo is not None and horizon > now
    except (TypeError, ValueError):
        valid_horizon = False
    valid = (data.get("online") and not data.get("source_errors") and valid_horizon
             and total is not None and priority is not None and 0 <= priority <= total <= 100)
    status = "planned"
    if not data.get("strategy_enabled"):
        status = "disabled"
    elif data.get("online") and not data.get("source_errors") and states.get("binary_sensor.opti_peak_reserve_aktiv") == "off":
        status = "no_peak"
    elif not valid:
        status = "no_valid_plan"
    elif shadow or not data.get("write_enabled"):
        status = "observation"
    elif data.get("manual_mode"):
        status = "manual"
    elif states.get("binary_sensor.opti_peak_reserve_aktiv") != "on":
        status = "no_peak"
    elif data.get("command_confirmation") == "pending" or data.get("command_result_this_update") in ("failed", "superseded"):
        status = "unconfirmed"
    elif "Peak-Leiter L3" in data.get("reason", "") or "Peak-Leiter L4" in data.get("reason", ""):
        status = "hold_requested"
    elif "Peak-Leiter L1" in data.get("reason", "") or "Peak-Leiter L2" in data.get("reason", ""):
        status = "discharge_requested"
    elif "Peak-Vorladen" in data.get("reason", ""):
        status = "charge_requested"
    load = number(settings.get("input_number.opti_peak_verbrauch_kw"))
    soc = number(states.get("sensor.opti_soc"))
    return {
        "status": status,
        "planned_reserve_soc": total if valid else None,
        "priority_reserve_soc": priority if valid else None,
        "horizon_end": attrs.get("horizont_ende") if valid else None,
        "assumed_load_w": number(attrs.get("assumed_load_w")) if number(attrs.get("assumed_load_w")) is not None else load * 1000 if load is not None else None,
        "assumed_discharge_efficiency": 0.9,
        "required_battery_kwh": number(attrs.get("benoetigt_kwh")) if valid else None,
        "expensive_hours": number(attrs.get("peak_stunden_exp")) if valid else None,
        "very_expensive_hours": number(attrs.get("peak_stunden_ve")) if valid else None,
        "day_target_soc": number(states.get("sensor.opti_target_soc")),
        "load_assumption_source": "hourly_profile" if (number(attrs.get("profile_hours")) or 0) > 0 else "fixed_setting",
        "profile_peak_hours": number(attrs.get("profile_hours")),
        "profile_status": data.get("attributes", {}).get("sensor.opti_peak_load_profile", {}).get("status", "disabled"),
        "profile_fallback_reason": data.get("attributes", {}).get("sensor.opti_peak_load_profile", {}).get("reason"),
        "profile_margin_percent": 20 if (number(attrs.get("profile_hours")) or 0) > 0 else None,
        "release_threshold_soc": ((priority if "Peak-Leiter L3" in data.get("reason", "") else total) + 2) if valid else None,
        "hold_threshold_soc": (priority if "Peak-Leiter L3" in data.get("reason", "") else total)
            if (valid and data.get("strategy_enabled") and data.get("write_enabled") and not shadow
                and not data.get("manual_mode") and states.get("binary_sensor.opti_peak_reserve_aktiv") == "on"
                and any(tag in data.get("reason", "") for tag in ("Peak-Leiter L3", "Peak-Leiter L4"))) else None,
        "current_soc": soc, "requested_mode": data.get("mode"),
        "decision_reason": data.get("reason"),
        "command_confirmation": data.get("command_confirmation"),
        "last_confirmed_command_at": data["last_write"].isoformat() if isinstance(data.get("last_write"), datetime) else data.get("last_write"),
        "battery_power_w": number(states.get("sensor.opti_battery_power_w")),
    }


def bucket() -> dict:
    return {"observed_seconds": 0.0, "missing_seconds": 0.0,
            "house_energy_kwh": 0.0, "battery_discharge_kwh": 0.0,
            "min_soc": None, "max_soc": None,
            "hold_observed_seconds": 0.0, "hold_missing_seconds": 0.0,
            "hold_discharge_seconds": 0.0, "hold_below_threshold_seconds": 0.0}


class OperatingReport:
    """One cumulative report plus current/last expensive period, bounded storage.

    Counters cover this report's lifetime. Restart intervals and >90s sampling
    gaps never contribute energy. Period edges use the preceding sample (zero
    order hold) only when both endpoints have fresh required measurements.
    """

    def __init__(self):
        self.data = {"version": 1, "started_at": None, "last_observation": None,
                     "totals": bucket(), "disconnects": 0, "source_error_episodes": 0,
                     "write_failures": 0, "sampling_gaps": 0, "restarts": 0, "clock_changes": 0,
                     "current_peak": None, "last_peak": None, "incidents": []}
        self._previous = None
        self._health = None
        self._restored = False

    def restore(self, saved: dict) -> None:
        # Local persistent data is validated; malformed records start fresh.
        if not isinstance(saved, dict) or saved.get("version") != 1:
            return
        try:
            if not isinstance(saved.get("totals"), dict):
                return
            if not isinstance(saved.get("incidents"), list) or len(saved["incidents"]) > 20:
                return
            for key in ("started_at", "last_observation"):
                if saved.get(key) is not None:
                    parsed = datetime.fromisoformat(saved[key])
                    if parsed.tzinfo is None:
                        return
            for key in ("disconnects", "source_error_episodes", "write_failures", "sampling_gaps", "restarts", "clock_changes"):
                if type(saved[key]) is not int or saved[key] < 0:
                    return
            for item in (saved["totals"], saved.get("current_peak"), saved.get("last_peak")):
                if item is None:
                    continue
                for key in bucket():
                    val = item[key]
                    if val is None and key in ("min_soc", "max_soc"):
                        continue
                    if number(val) is None or val < 0:
                        return
        except (KeyError, TypeError, ValueError):
            return
        if any(key not in saved for key in self.data):
            return
        self.data = {key: deepcopy(saved[key]) for key in self.data}
        self.data["restarts"] += 1
        self._restored = True
        self._previous = None
        self._health = None

    def _incident(self, now, kind):
        self.data["incidents"].append({"at": now.isoformat(), "kind": kind})
        self.data["incidents"] = self.data["incidents"][-20:]

    @staticmethod
    def _soc(target, soc):
        if soc is not None and 0 <= soc <= 100:
            target["min_soc"] = soc if target["min_soc"] is None else min(target["min_soc"], soc)
            target["max_soc"] = soc if target["max_soc"] is None else max(target["max_soc"], soc)

    @staticmethod
    def _accumulate(target, elapsed, previous, valid):
        if valid:
            target["observed_seconds"] += elapsed
            target["house_energy_kwh"] += max(0, previous["house"]) * elapsed / 3600000
            target["battery_discharge_kwh"] += max(0, -previous["battery"]) * elapsed / 3600000
        else:
            target["missing_seconds"] += elapsed
        if previous and previous["holding"]:
            if valid:
                target["hold_observed_seconds"] += elapsed
                if previous["hold_discharge"]:
                    target["hold_discharge_seconds"] += elapsed
                if previous["hold_below_threshold"]:
                    target["hold_below_threshold_seconds"] += elapsed
            else:
                target["hold_missing_seconds"] += elapsed

    def observe(self, now: datetime, data: dict, plan: dict, *, write_failed: bool = False) -> dict:
        stamp = now.timestamp()
        states = data.get("states", {})
        house = number(states.get("sensor.opti_house_consumption_w"))
        battery = number(states.get("sensor.opti_battery_power_w"))
        soc = number(states.get("sensor.opti_soc"))
        online = data.get("online") is True
        errors = bool(data.get("source_errors"))
        valid = online and not errors and house is not None and battery is not None and soc is not None and 0 <= soc <= 100
        price_level = states.get("sensor.opti_price_level")
        active = states.get("binary_sensor.opti_peak_reserve_aktiv")
        classification_known = (online and active in ("on", "off")
            and price_level in ("VERY_CHEAP", "CHEAP", "NORMAL", "EXPENSIVE", "VERY_EXPENSIVE")
            and not any("price" in key for key in data.get("source_errors", {})))
        peak = active == "on" and price_level in ("EXPENSIVE", "VERY_EXPENSIVE")
        disabled = data.get("strategy_enabled") is not True
        if self.data["started_at"] is None:
            self.data["started_at"] = now.isoformat()
        last = self.data["last_observation"]
        elapsed = stamp - datetime.fromisoformat(last).timestamp() if last else 0
        if self._restored:
            self._incident(now, "restart")
            self._restored = False
        if elapsed < 0:
            # Establish a new time base without counting overlapping energy.
            self._previous = None
            elapsed = 0
            valid = False
            self.data["clock_changes"] += 1
            self._incident(now, "clock_moved_backwards")
        previous = self._previous
        covered = bool(previous and previous["valid"] and valid and 0 < elapsed <= MAX_INTERVAL)
        if elapsed:
            if elapsed > MAX_INTERVAL or previous is None:
                self.data["sampling_gaps"] += 1
                self._incident(now, "coverage_gap")
            self._accumulate(self.data["totals"], elapsed, previous, covered)
            if self.data["current_peak"] is not None:
                self._accumulate(self.data["current_peak"], elapsed, previous, covered)
        if self._health is not None:
            self.data["disconnects"] += int(self._health[0] and not online)
            self.data["source_error_episodes"] += int(not self._health[1] and errors)
        else:
            self.data["disconnects"] += int(not online)
            self.data["source_error_episodes"] += int(errors)
        if not online and (self._health is None or self._health[0]):
            self._incident(now, "offline")
        if errors and (self._health is None or not self._health[1]):
            self._incident(now, "source_error")
        if write_failed:
            self._incident(now, "write_failed")
        self._health = (online, errors)
        self.data["write_failures"] += int(write_failed)
        if (disabled or (classification_known and not peak)) and self.data["current_peak"] is not None:
            self.data["current_peak"]["ended_at"] = now.isoformat()
            self.data["current_peak"]["end_reason"] = "strategy_disabled" if disabled else "price_period_ended"
            self.data["last_peak"] = self.data["current_peak"]
            self.data["current_peak"] = None
        if classification_known and peak and not disabled and self.data["current_peak"] is None:
            self.data["current_peak"] = {**bucket(), "started_at": now.isoformat(),
                "start_soc": soc if valid else None, "plan_at_start": deepcopy(plan),
                "control_enabled_at_start": data.get("write_enabled") is True}
        if valid:
            self._soc(self.data["totals"], soc)
            if self.data["current_peak"] is not None:
                self._soc(self.data["current_peak"], soc)
                self.data["current_peak"]["last_soc"] = soc
        self.data["last_observation"] = now.isoformat()
        threshold = plan.get("hold_threshold_soc")
        self._previous = {"valid": valid, "house": house, "battery": battery,
                          "holding": threshold is not None,
                          "hold_discharge": bool(valid and battery < -200),
                          "hold_below_threshold": bool(valid and threshold is not None and soc < threshold - 1)}
        self.data["last_control"] = {
            "requested_mode": data.get("engine_requested_mode"),
            "effective_mode": data.get("mode"),
            "command_result_this_update": data.get("command_result_this_update", "not_attempted"),
            "write_enabled": data.get("write_enabled") is True,
            "reason": data.get("reason"),
        }
        result = self.snapshot()
        result["status"] = "collecting" if valid else "data_missing"
        result["last_control"] = deepcopy(self.data["last_control"])
        result["current_online"] = online
        result["current_source_errors"] = errors
        seconds = result["totals"]["observed_seconds"]
        result["observed_mean_load_w"] = result["totals"]["house_energy_kwh"] * 3600000 / seconds if seconds else None
        total_seconds = seconds + result["totals"]["missing_seconds"]
        result["coverage_percent"] = 100 * seconds / total_seconds if total_seconds else None
        result["energy_method"] = "sampled_zero_order_hold_max_90s"
        return result

    def snapshot(self) -> dict:
        return deepcopy({key: value for key, value in self.data.items() if key != "last_control"})
