"""Observation-only comparison of the ladder mode with the evcc optimizer.

Nothing here feeds back into control, mode, parameters or the decision id.

Stage 1 reads evcc's battery suggestion for the current slot from a Home
Assistant entity; ha-evcc exposes it as the ``suggestion`` attribute of the
battery SoC sensor. Stage 2 reads the evopt result from evcc's state API and
derives a battery action for every slot with evcc's own rule
(``currentSlotSuggestion`` in core/site_optimizer.go, evcc 0.318.0), so the
plan horizon is visible without evcc's automatic mode.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from .planner import DYNAMIC, GRID_CHARGE, ONLY_CHARGE, ONLY_DISCHARGE
from .planner_shadow import deviation

MAX_INTERVAL = 90
# evcc ignores power below this as numerical noise (suggestionThreshold, W).
THRESHOLD_W = 50.0
# evcc drops a solve older than two quarter-hour slots (suggestionMaxAge).
EVOPT_MAX_AGE = timedelta(minutes=30)
SUGGESTION_MAX_AGE = timedelta(minutes=15)
FETCH_INTERVAL = timedelta(minutes=5)
FETCH_TIMEOUT_S = 10
MAX_SEGMENTS = 96
STATE_PATH = "/api/state"
# The unauthenticated jq parameter trims the state to the evopt result.
STATE_QUERY = {"jq": ".evopt"}

# evcc battery modes in the ladder vocabulary. ``hold`` blocks discharge,
# ``holdcharge`` blocks charging; battery-to-grid discharge has no ladder mode.
LADDER_MODES = {
    "normal": DYNAMIC,
    "hold": ONLY_CHARGE,
    "charge": GRID_CHARGE,
    "holdcharge": ONLY_DISCHARGE,
}
ACTIONS = (*LADDER_MODES, "discharge")
VERDICTS = ("agree", "evcc_grid_charge", "ladder_grid_charge", "evcc_hold",
            "evcc_discharge", "evcc_no_charge", "other")
SOURCES = ("suggestion", "evopt")
_VERDICT_COUNTERS = tuple(f"{verdict}_seconds" for verdict in VERDICTS)
_SOURCE_COUNTERS = tuple(f"{source}_seconds" for source in SOURCES)
_COUNTERS = ("ready_seconds", *_VERDICT_COUNTERS, *_SOURCE_COUNTERS,
             "derivation_checked_seconds", "derivation_mismatch_seconds")
NOTE = (
    "evcc plans with its own battery limits, load profile and forecasts and "
    "knows neither the maxsoc cap nor the peak reserve. A difference shows "
    "where the two plans part, not which one is right."
)
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _instant(value: Any) -> datetime | None:
    """Parse a Go RFC 3339 time; Python accepts at most microseconds."""
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(_FRACTION.sub(r"\1", value.replace("Z", "+00:00")))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def classify(action: Any, ladder_mode: Any) -> str:
    """Name the difference between evcc's action and the ladder mode."""
    mode = LADDER_MODES.get(action)
    if mode is None:
        return "other"
    verdict = deviation(mode, ladder_mode)
    return "evcc_" + verdict.removeprefix("planner_") if verdict.startswith("planner_") else verdict


def suggestion_status(attributes: Any, state: Any, reported_at: Any, now: datetime) -> dict[str, Any]:
    """Read evcc's current-slot suggestion from an entity's attributes."""
    if state is None:
        return {"status": "entity_missing"}
    if state in ("unavailable", "unknown"):
        return {"status": "unavailable"}
    if not isinstance(reported_at, datetime) or now - reported_at > SUGGESTION_MAX_AGE:
        return {"status": "stale"}
    suggestion = attributes.get("suggestion") if isinstance(attributes, Mapping) else None
    if suggestion is None:
        return {"status": "no_suggestion"}
    action = suggestion.get("action") if isinstance(suggestion, Mapping) else None
    if action not in ACTIONS:
        return {"status": "invalid_suggestion"}
    return {"status": "ready", "action": action}


def _slot_action(charge_w: float, discharge_w: float, import_w: float, export_w: float,
                 soc: float | None, s_min: float, s_max: float) -> str:
    """evcc's battery rule for one slot (currentSlotSuggestion)."""
    importing = import_w > THRESHOLD_W
    exporting = export_w > THRESHOLD_W
    idle = charge_w <= THRESHOLD_W and discharge_w <= THRESHOLD_W
    empty = soc is not None and soc <= s_min
    full = soc is not None and s_max > 0 and soc >= s_max
    if charge_w > THRESHOLD_W and importing:
        return "charge"
    if idle and importing and not empty:
        return "hold"
    if idle and exporting and not full:
        return "holdcharge"
    if discharge_w > THRESHOLD_W and exporting:
        return "discharge"
    return "normal"


def _series(values: Any, length: int, *, required: bool) -> list[float] | None:
    """A per-slot energy series; evcc omits empty grid series."""
    if values is None and not required:
        return [0.0] * length
    if not isinstance(values, Sequence) or isinstance(values, str) or len(values) < length:
        return None
    numbers = [_number(value) for value in values[:length]]
    return None if any(value is None for value in numbers) else numbers


def derive_plan(evopt: Any, now: datetime) -> dict[str, Any]:
    """Derive the home battery action for every evopt slot.

    Energies in the result are Wh per slot; evcc divides by the slot length
    before comparing with its threshold, so the derivation does the same.
    """
    if evopt is None:
        return {"status": "no_evopt"}
    if not isinstance(evopt, Mapping):
        return {"status": "invalid"}
    updated = _instant(evopt.get("updated"))
    if updated is None:
        return {"status": "invalid"}
    age = now - updated
    if age > EVOPT_MAX_AGE:
        return {"status": "stale", "updated": updated.isoformat()}
    res = evopt.get("res") if isinstance(evopt.get("res"), Mapping) else {}
    if res.get("status") not in ("Optimal", "Feasible"):
        return {"status": "not_solved", "updated": updated.isoformat()}
    req = evopt.get("req") if isinstance(evopt.get("req"), Mapping) else {}
    details = evopt.get("details") if isinstance(evopt.get("details"), Mapping) else {}
    series = req.get("time_series") if isinstance(req.get("time_series"), Mapping) else {}
    dt = series.get("dt")
    stamps = details.get("timestamp")
    if (not isinstance(dt, Sequence) or not isinstance(stamps, Sequence) or not dt
            or len(dt) != len(stamps)):
        return {"status": "invalid"}
    starts = [_instant(stamp) for stamp in stamps]
    seconds = [_number(value) for value in dt]
    if any(start is None for start in starts) or any(value is None or value <= 0 for value in seconds):
        return {"status": "invalid"}
    kinds = details.get("batteryDetails")
    batteries = res.get("batteries")
    configs = req.get("batteries")
    if not all(isinstance(item, Sequence) for item in (kinds, batteries, configs)) or not (
            len(kinds) == len(batteries) == len(configs)):
        return {"status": "invalid"}
    homes = [index for index, kind in enumerate(kinds)
             if isinstance(kind, Mapping) and kind.get("type") == "battery"]
    if not homes:
        return {"status": "no_home_battery"}
    # evcc applies one mode to all home batteries; the first one decides here too.
    index = homes[0]
    battery, config = batteries[index], configs[index]
    if not isinstance(battery, Mapping) or not isinstance(config, Mapping):
        return {"status": "invalid"}
    length = len(seconds)
    charge = _series(battery.get("charging_power"), length, required=True)
    discharge = _series(battery.get("discharging_power"), length, required=True)
    soc = battery.get("state_of_charge")
    soc = _series(soc, length, required=True) if soc is not None else None
    grid_import = _series(res.get("grid_import"), length, required=False)
    grid_export = _series(res.get("grid_export"), length, required=False)
    s_min, s_max = _number(config.get("s_min")), _number(config.get("s_max"))
    if None in (charge, discharge, grid_import, grid_export, s_min, s_max) or (
            battery.get("state_of_charge") is not None and soc is None):
        return {"status": "invalid"}
    segments: list[list[Any]] = []
    hours: dict[str, float] = {}
    current = None
    for slot in range(length):
        start = starts[slot]
        end = start + timedelta(seconds=seconds[slot])
        if end <= now:
            continue
        slot_hours = seconds[slot] / 3600
        action = _slot_action(
            charge[slot] / slot_hours, discharge[slot] / slot_hours,
            grid_import[slot] / slot_hours, grid_export[slot] / slot_hours,
            soc[slot] if soc is not None else None, s_min, s_max)
        if current is None and start <= now:
            current = action
        hours[action] = hours.get(action, 0.0) + (end - max(start, now)).total_seconds() / 3600
        if segments and segments[-1][2] == action and segments[-1][1] == start.isoformat():
            segments[-1][1] = end.isoformat()
        else:
            segments.append([start.isoformat(), end.isoformat(), action])
    if current is None:
        return {"status": "no_current_slot", "updated": updated.isoformat()}
    return {
        "status": "ready",
        "updated": updated.isoformat(),
        "age_seconds": round(age.total_seconds()),
        "battery": kinds[index].get("name"),
        "home_batteries": len(homes),
        "current_action": current,
        "hours": {key: round(value, 2) for key, value in hours.items()},
        "segments": segments[:MAX_SEGMENTS],
        "segments_truncated": len(segments) > MAX_SEGMENTS,
    }


def evcc_check(suggestion: Mapping[str, Any], plan: Mapping[str, Any], ladder_mode: Any) -> dict[str, Any]:
    """Compare evcc's current action with the ladder; the suggestion wins over the derivation."""
    base: dict[str, Any] = {
        "informational_only": True,
        "controls_battery": False,
        "note": NOTE,
        "suggestion": dict(suggestion),
        "plan": dict(plan),
    }
    if suggestion.get("status") == "ready":
        action, source = suggestion["action"], "suggestion"
    elif plan.get("status") == "ready":
        action, source = plan["current_action"], "evopt"
    else:
        return {**base, "status": "no_evcc_data"}
    derivation_matches = (
        suggestion["action"] == plan["current_action"]
        if suggestion.get("status") == "ready" and plan.get("status") == "ready" else None
    )
    return {
        **base,
        "status": "ready",
        "source": source,
        "evcc_action": action,
        "evcc_mode": LADDER_MODES.get(action),
        "ladder_mode": ladder_mode,
        "verdict": classify(action, ladder_mode),
        "derivation_matches": derivation_matches,
    }


async def fetch_evopt(session: Any, url: str) -> Any:
    """Read the evopt result from evcc's state API; ``None`` while evcc has none."""
    async with session.get(url.rstrip("/") + STATE_PATH, params=STATE_QUERY,
                           timeout=FETCH_TIMEOUT_S) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


class EvccComparison:
    """Time-weighted totals of the evcc-versus-ladder verdicts.

    Zero-order hold over the previous check. Intervals over 90 s, the first
    check after a restart and backwards clock steps never count.
    """

    def __init__(self) -> None:
        self.data = self._fresh()
        self._previous: tuple[datetime, dict[str, Any]] | None = None

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {"version": 1, "started_at": None, "last_observation": None,
                **{key: 0.0 for key in _COUNTERS}}

    def snapshot(self) -> dict[str, Any]:
        return dict(self.data)

    def restore(self, saved: Any) -> None:
        self.data = self._fresh()
        self._previous = None
        try:
            if not isinstance(saved, Mapping) or saved.get("version") != 1:
                return
            stamps: dict[str, Any] = {}
            for key in ("started_at", "last_observation"):
                raw = saved.get(key)
                if raw is not None and datetime.fromisoformat(raw).tzinfo is None:
                    return
                stamps[key] = raw
            counters: dict[str, float] = {}
            for key in _COUNTERS:
                value = _number(saved.get(key))
                if value is None or value < 0:
                    return
                counters[key] = value
            ready = counters["ready_seconds"]
            tolerance = 1e-6 * max(1.0, ready)
            if (abs(sum(counters[key] for key in _VERDICT_COUNTERS) - ready) > tolerance
                    or abs(sum(counters[key] for key in _SOURCE_COUNTERS) - ready) > tolerance
                    or counters["derivation_mismatch_seconds"] > counters["derivation_checked_seconds"] + tolerance):
                return
        except (TypeError, ValueError, OverflowError, AttributeError):
            return
        self.data = {"version": 1, **stamps, **counters}

    def _totals(self) -> dict[str, Any]:
        out: dict[str, Any] = {key: round(self.data[key], 3) for key in _COUNTERS}
        ready = self.data["ready_seconds"]
        out["disagreement_percent"] = (
            round((ready - self.data["agree_seconds"]) / ready * 100, 3) if ready > 0 else None
        )
        out["started_at"] = self.data["started_at"]
        out["last_observation"] = self.data["last_observation"]
        return out

    def interrupt(self) -> None:
        """A failed or disabled observation ends the held verdict; the gap never counts."""
        self._previous = None

    def observe(self, now: datetime, check: Mapping[str, Any]) -> dict[str, Any]:
        if self.data["started_at"] is None:
            self.data["started_at"] = now.isoformat()
        if self._previous is not None:
            before, held = self._previous
            elapsed = (now - before).total_seconds()
            if 0 < elapsed <= MAX_INTERVAL and held.get("status") == "ready":
                self.data["ready_seconds"] += elapsed
                self.data[f"{held['verdict']}_seconds"] += elapsed
                self.data[f"{held['source']}_seconds"] += elapsed
                if held.get("derivation_matches") is not None:
                    self.data["derivation_checked_seconds"] += elapsed
                    if held["derivation_matches"] is False:
                        self.data["derivation_mismatch_seconds"] += elapsed
        self._previous = (now, dict(check))
        self.data["last_observation"] = now.isoformat()
        return self._totals()
