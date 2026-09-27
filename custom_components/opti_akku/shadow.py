"""Bounded, private read-only observation journal; no HA or device actions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
from uuid import uuid4

MEASUREMENTS = ("soc", "battery_temp", "battery_power_w", "house_consumption_w",
                "pv_generation_w", "pv_power_w", "grid_import_w", "grid_export_w",
                "charge_power_w", "target_soc", "price_current_ct_kwh")
COUNTERS = ("samples", "online_samples", "source_error_samples", "comparisons",
            "mismatches", "reference_missing", "gaps", "settings_changes")
DEMAND_FIELDS = (
    "status", "controls_battery", "observation_only", "profile_ready",
    "historical_forecast_slots", "recent_coverage_seconds", "pv_cover_from",
    "expected_deficit_kwh", "required_battery_kwh", "current_strategy_reserve_soc",
    "suggested_reserve_soc", "comparison_note", "refill_profile_ready",
    "refill_historical_forecast_slots", "refill_horizon_end", "refill_horizon_hours",
    "refill_surplus_kwh", "refill_charge_efficiency", "refill_battery_kwh",
    "refill_target_kwh", "refill_storable_kwh", "refill_missing_kwh",
    "refill_coverage_percent", "refill_covered",
)
COMPARISON_FIELDS = ("status", "reason", "active_score", "candidate_score", "score_delta",
                     "active_target_soc", "candidate_target_soc", "target_delta")
COMPARISON_BLOCKS = ("remaining_day", "tomorrow", "sunny_day", "target_soc")
KNOWN_ERRORS = ("journal_write_failed", "invalid_saved_session")
# Optional counters added after the first journal format; absent in old sessions.
TRANSITION_COUNTERS = ("mode_changes", "reference_changes")
# A reference that never moves while Opti changes mode this often is not a
# live decision (e.g. the legacy automation that wrote it is switched off).
STATIC_REFERENCE_MODE_CHANGES = 3


def _scalar_fields(source: dict, fields: tuple[str, ...]) -> dict:
    """Keep exact bounded scalar values from integration-owned reports."""
    result = {}
    for key in fields:
        if key not in source:
            continue
        value = source.get(key)
        if value is None or isinstance(value, bool):
            result[key] = value
        elif isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value):
            result[key] = value
        elif isinstance(value, str) and len(value) <= 128:
            result[key] = value
    return result


def _demand_sample(data: dict) -> dict:
    """Copy only the bounded fields needed for the passive refill comparison."""
    report = data.get("demand_forecast", {})
    if not isinstance(report, dict):
        return {}
    result = _scalar_fields(report, DEMAND_FIELDS)
    comparison = report.get("strategy_comparison")
    if not isinstance(comparison, dict):
        return result
    bounded = _scalar_fields(comparison, ("status", "reason", "observation_only"))
    blocks = comparison.get("blocks", {})
    if isinstance(blocks, dict):
        bounded["blocks"] = {
            name: _scalar_fields(block, COMPARISON_FIELDS)
            for name in COMPARISON_BLOCKS
            if isinstance((block := blocks.get(name)), dict)
        }
    result["strategy_comparison"] = bounded
    return result


@lru_cache(maxsize=1)
def _build_identity() -> dict[str, str]:
    """Called in the journal executor, once per loaded implementation."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ("manifest.json", "coordinator.py", "engine.py", "sources.py", "sma.py",
                 "shadow.py", "tibber_prices.py", "resources/strategy.json"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return {"version": json.loads((root / "manifest.json").read_text())["version"],
            "sha256": digest.hexdigest()}


class ShadowRecorder:
    """One explicit 24h run; absolute deadline survives HA restarts."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.state = {"status": "idle"}

    def snapshot(self) -> dict:
        return deepcopy(self.state)

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict):
            self.state = {"status": "error", "error": "invalid_saved_session"}
            return
        if not state or state.get("status") == "idle":
            return
        if state.get("status") == "error" and state.get("error") in KNOWN_ERRORS:
            self.state = {"status": "error", "error": state["error"]}
            return
        try:
            if state.get("status") not in ("running", "stopped", "completed"):
                raise ValueError("Invalid status")
            if not re.fullmatch(r"[0-9a-f]{32}", state["session_id"]):
                raise ValueError("Invalid session")
            start = datetime.fromisoformat(state["started_at"])
            end = datetime.fromisoformat(state["deadline"])
            if (
                start.utcoffset() is None
                or end.utcoffset() is None
                or end - start != timedelta(hours=24)
            ):
                raise ValueError("Invalid deadline")
            if any(
                isinstance(state.get(name), bool)
                or not isinstance(state.get(name), int)
                or state[name] < 0
                for name in COUNTERS
            ):
                raise ValueError("Invalid counters")
            samples = state["samples"]
            if (
                state["online_samples"] > samples
                or state["source_error_samples"] > samples
                or state["comparisons"] + state["reference_missing"] != samples
                or state["mismatches"] > state["comparisons"]
                or state["settings_changes"] > samples
                or state["gaps"] > samples + 1
            ):
                raise ValueError("Inconsistent counters")
            if any(
                name in state and (isinstance(state[name], bool) or not isinstance(state[name], int)
                                   or state[name] < 0)
                for name in TRANSITION_COUNTERS
            ):
                raise ValueError("Invalid transition counters")
            max_gap = state.get("max_gap_seconds")
            if (
                isinstance(max_gap, bool)
                or not isinstance(max_gap, int | float)
                or not 0 <= max_gap <= 86400
                or not isfinite(max_gap)
            ):
                raise ValueError("Invalid gap")
            last_sample = state.get("last_sample")
            if last_sample is not None:
                last = datetime.fromisoformat(last_sample)
                if last.utcoffset() is None or not start <= last <= end:
                    raise ValueError("Invalid last sample")
            if not isinstance(state.get("settings"), dict) or not isinstance(
                state.get("reference_entity"), str
            ):
                raise ValueError("Invalid recording configuration")
            self.state = deepcopy(state)
        except (KeyError, TypeError, ValueError):
            self.state = {"status": "error", "error": "invalid_saved_session"}

    def _append(self, row: dict, *, exclusive: bool = False) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / f"{self.state['session_id']}.jsonl"
        flags = os.O_WRONLY | (os.O_CREAT | os.O_EXCL if exclusive else os.O_APPEND)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")

    def start(self, now: datetime, settings: dict, reference_entity: str, version: str) -> dict:
        if self.state.get("status") == "running":
            raise ValueError("An observation is already running")
        previous = self.state
        self.state = {"status": "running", "session_id": uuid4().hex,
                      "started_at": now.isoformat(), "deadline": (now + timedelta(hours=24)).isoformat(),
                      "samples": 0, "online_samples": 0, "source_error_samples": 0,
                      "comparisons": 0, "mismatches": 0, "reference_missing": 0,
                      "gaps": 0, "max_gap_seconds": 0, "settings_changes": 0,
                      "last_sample": None, "settings": dict(settings), "reference_entity": reference_entity,
                      "mode_changes": 0, "reference_changes": 0, "reference_static": False}
        try:
            self._append({"type": "start", "version": version, **self.snapshot()}, exclusive=True)
        except OSError:
            self.state = previous
            raise
        return self.snapshot()

    def stop(self, now: datetime) -> dict:
        """Close a user-interrupted run without discarding its journal."""
        if self.state.get("status") == "running":
            self._append({"type": "end", "time": now.isoformat(),
                          "samples": self.state["samples"], "reason": "user_stopped"})
            self.state["status"] = "stopped"
            self.state["stopped_at"] = now.isoformat()
        return self.snapshot()

    def record(self, now: datetime, data: dict, settings: dict, reference: str | None, modes: tuple) -> dict:
        if self.state.get("status") != "running":
            return self.snapshot()
        if now >= datetime.fromisoformat(self.state["deadline"]):
            last = self.state["last_sample"] or self.state["started_at"]
            gap = (datetime.fromisoformat(self.state["deadline"]) - datetime.fromisoformat(last)).total_seconds()
            self.state["max_gap_seconds"] = max(gap, self.state["max_gap_seconds"])
            self.state["gaps"] += int(gap > 45)
            self._append({"type": "end", "time": now.isoformat(), "samples": self.state["samples"]})
            self.state["status"] = "completed"
            return self.snapshot()
        if self.state["last_sample"] and (now - datetime.fromisoformat(self.state["last_sample"])).total_seconds() < 12:
            # Below the 15 s update period: HA rounds schedule times, so a
            # strict 15 s threshold would drop regular samples.
            return self.snapshot()
        reference = reference if reference in modes else None
        row = {"type": "sample", "time": now.isoformat(), "read_only": True,
               "observation_only": True,
               "build": dict(_build_identity()),
               "mode": data["mode"], "reason": data["reason"], "online": data["online"],
               "source_errors": data["source_errors"], "reference_mode": reference,
               "measurements": {key: data["states"].get(f"sensor.opti_{key}") for key in MEASUREMENTS},
               "entry_shadow_mode": data.get("entry_shadow_mode") is True,
               "write_enabled": data.get("write_enabled") is True,
               "strategy_enabled": data.get("strategy_enabled") is True,
               "command_confirmation": data.get("command_confirmation"),
               "device_source_error_count": len(data.get("device_errors", {}))
               if isinstance(data.get("device_errors"), dict) else 0,
               "price_status": data.get("price_status"),
               "demand_forecast": _demand_sample(data)}
        if settings != self.state["settings"]:
            row["settings"] = dict(settings)
        self._append(row)
        last = self.state["last_sample"] or self.state["started_at"]
        gap = (now - datetime.fromisoformat(last)).total_seconds()
        self.state["max_gap_seconds"] = max(gap, self.state["max_gap_seconds"])
        self.state["gaps"] += int(gap > 45)
        self.state["samples"] += 1
        self.state["online_samples"] += int(data["online"])
        self.state["source_error_samples"] += int(bool(data["source_errors"]))
        self.state["reference_missing"] += int(reference is None)
        if reference is not None:
            self.state["comparisons"] += 1
            self.state["mismatches"] += int(reference != data["mode"])
        # Count only transitions between known values: a first sample, a
        # restored older session or an unavailable reference is no change.
        for name in TRANSITION_COUNTERS:
            self.state.setdefault(name, 0)
        if self.state.get("last_mode") is not None:
            self.state["mode_changes"] = self.state.get("mode_changes", 0) + int(
                data["mode"] != self.state["last_mode"])
        self.state["last_mode"] = data["mode"]
        if reference is not None:
            if self.state.get("last_reference") is not None:
                self.state["reference_changes"] = self.state.get("reference_changes", 0) + int(
                    reference != self.state["last_reference"])
            self.state["last_reference"] = reference
        self.state["reference_static"] = bool(
            self.state["comparisons"]
            and self.state.get("reference_changes", 0) == 0
            and self.state.get("mode_changes", 0) >= STATIC_REFERENCE_MODE_CHANGES)
        if "settings" in row:
            self.state["settings_changes"] += 1
            self.state["settings"] = dict(settings)
        self.state["last_sample"] = now.isoformat()
        return self.snapshot()
