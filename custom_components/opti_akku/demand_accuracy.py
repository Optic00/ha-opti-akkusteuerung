"""Compare a frozen demand prediction with observed load, never claimed savings."""

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from math import isfinite
from typing import TypedDict

MAX_COMPLETED = 7
MAX_WINDOW_SECONDS = 86400
MAX_ENERGY_KWH = 1200
# Covered plus missing time is a sum of float interval lengths.
DURATION_TOLERANCE_SECONDS = 0.01
NOTE = "House energy until predicted PV onset; not avoided grid import"


class PendingTrial(TypedDict):
    """Frozen prediction and the observations collected for its time window."""

    start: str
    end: str
    last: str
    profile_ready: bool
    predicted_kwh: float
    actual_kwh: float
    covered_seconds: float
    missing_seconds: float


class CompletedTrial(PendingTrial):
    """Finished trial with an error only when the full window was observed."""

    coverage_percent: float
    error_kwh: float | None
    note: str


class AccuracySnapshot(TypedDict):
    """Persisted accuracy state; version 1 snapshots may lack ``completed``."""

    version: int
    pending: PendingTrial | None
    completed: list[CompletedTrial]


class AccuracyResult(TypedDict):
    """Current accuracy state exposed through the demand forecast."""

    pending: PendingTrial | None
    completed: list[CompletedTrial]


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _amount(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if isfinite(number) and number >= 0 else None


def _restore_trial(saved: object, *, completed: bool) -> PendingTrial | None:
    """Accept only a trial whose times and observed durations agree."""
    if not isinstance(saved, dict):
        return None
    start = _timestamp(saved.get("start"))
    end = _timestamp(saved.get("end"))
    last = _timestamp(saved.get("last"))
    if start is None or end is None or last is None:
        return None
    if not start <= last <= end or not 0 < (end - start).total_seconds() <= MAX_WINDOW_SECONDS:
        return None
    if completed and last != end:
        return None
    predicted_kwh = _amount(saved.get("predicted_kwh"))
    actual_kwh = _amount(saved.get("actual_kwh"))
    covered_seconds = _amount(saved.get("covered_seconds"))
    missing_seconds = _amount(saved.get("missing_seconds"))
    if predicted_kwh is None or actual_kwh is None or covered_seconds is None or missing_seconds is None:
        return None
    if max(predicted_kwh, actual_kwh) > MAX_ENERGY_KWH:
        return None
    # Every elapsed second is either observed or missing, never both.
    elapsed = (last - start).total_seconds()
    if abs(covered_seconds + missing_seconds - elapsed) > DURATION_TOLERANCE_SECONDS:
        return None
    if covered_seconds == 0 and actual_kwh > 0:
        return None
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "last": last.isoformat(),
        "profile_ready": saved.get("profile_ready") is True,
        "predicted_kwh": predicted_kwh,
        "actual_kwh": actual_kwh,
        "covered_seconds": covered_seconds,
        "missing_seconds": missing_seconds,
    }


def _complete(trial: PendingTrial) -> CompletedTrial:
    """Derive the summary from observed durations only."""
    covered = trial["covered_seconds"]
    total = covered + trial["missing_seconds"]
    result: CompletedTrial = {
        **deepcopy(trial),
        "coverage_percent": round(100 * covered / total, 1) if total else 0.0,
        "error_kwh": (
            round(trial["actual_kwh"] - trial["predicted_kwh"], 3)
            if trial["missing_seconds"] == 0
            else None
        ),
        "note": NOTE,
    }
    return result


def _restore_completed(saved: object) -> list[CompletedTrial]:
    """All-or-nothing: a partly trusted history would misstate what was observed."""
    if not isinstance(saved, list) or len(saved) > MAX_COMPLETED:
        return []
    restored: list[CompletedTrial] = []
    previous_end: datetime | None = None
    for item in saved:
        trial = _restore_trial(item, completed=True)
        if trial is None:
            return []
        start = datetime.fromisoformat(trial["start"])
        if previous_end is not None and start < previous_end:
            return []
        previous_end = datetime.fromisoformat(trial["end"])
        restored.append(_complete(trial))
    return restored


class DemandAccuracy:
    def __init__(self) -> None:
        self.pending: PendingTrial | None = None
        self.completed: list[CompletedTrial] = []
        self.previous: tuple[datetime, float | None] | None = None

    def snapshot(self) -> AccuracySnapshot:
        snapshot: AccuracySnapshot = {
            "version": 1,
            "pending": self.pending,
            "completed": self.completed,
        }
        return deepcopy(snapshot)

    def restore(self, saved: object) -> None:
        # The latest sample is never restored: the restart gap stays missing.
        self.previous = None
        self.pending = None
        self.completed = []
        if not isinstance(saved, dict) or saved.get("version") != 1:
            return
        self.pending = _restore_trial(saved.get("pending"), completed=False)
        self.completed = _restore_completed(saved.get("completed", []))
        if (
            self.pending is not None
            and self.completed
            and datetime.fromisoformat(self.pending["start"])
            < datetime.fromisoformat(self.completed[-1]["end"])
        ):
            # Trials never overlap; keep the compatible in-progress trial.
            self.completed = []

    def observe(
        self,
        now: datetime,
        house_w: float | None,
        prediction: Mapping[str, object],
    ) -> AccuracyResult:
        if self.pending:
            trial = self.pending
            last = datetime.fromisoformat(trial["last"])
            end = datetime.fromisoformat(trial["end"])
            if now < last:
                self.pending = None
            else:
                stop = min(end, now)
                seconds = (stop - last).total_seconds()
                # No extrapolation through a long sampling gap or a restart.
                previous = self.previous
                if (
                    previous is not None
                    and previous[0] == last
                    and previous[1] is not None
                    and (now - last).total_seconds() <= 90
                ):
                    trial["actual_kwh"] += previous[1] * seconds / 3600000
                    trial["covered_seconds"] += seconds
                else:
                    trial["missing_seconds"] += seconds
                trial["last"] = stop.isoformat()
                if now >= end:
                    self.completed = (self.completed + [_complete(trial)])[-MAX_COMPLETED:]
                    self.pending = None
        if not self.pending and prediction.get("status") in ("learning", "ready"):
            raw_end = prediction.get("pv_cover_from")
            expected = prediction.get("expected_load_kwh")
            candidate_end: datetime | None = None
            expected_value: float | None = None
            try:
                candidate_end = datetime.fromisoformat(raw_end) if isinstance(raw_end, str) else None
                if not isinstance(expected, bool) and isinstance(expected, int | float):
                    candidate_value = float(expected)
                    if isfinite(candidate_value) and 0 <= candidate_value <= MAX_ENERGY_KWH:
                        expected_value = candidate_value
                valid_window = (
                    candidate_end is not None
                    and candidate_end.utcoffset() is not None
                    and 900 <= (candidate_end - now).total_seconds() <= MAX_WINDOW_SECONDS
                )
            except (ValueError, TypeError, OverflowError):
                valid_window = False
            if valid_window and expected_value is not None and candidate_end is not None:
                pending: PendingTrial = {
                    "start": now.isoformat(),
                    "end": candidate_end.isoformat(),
                    "last": now.isoformat(),
                    "predicted_kwh": expected_value,
                    "profile_ready": prediction.get("profile_ready") is True,
                    "actual_kwh": 0.0,
                    "covered_seconds": 0.0,
                    "missing_seconds": 0.0,
                }
                self.pending = pending
        self.previous = (now, house_w)
        return {"pending": deepcopy(self.pending), "completed": deepcopy(self.completed)}
