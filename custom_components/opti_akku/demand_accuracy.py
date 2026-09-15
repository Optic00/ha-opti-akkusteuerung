"""Compare a frozen demand prediction with observed load, never claimed savings."""

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from math import isfinite
from typing import TypedDict


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
    """Persisted accuracy state; completed history is not restored after restart."""

    version: int
    pending: PendingTrial | None
    completed: list[CompletedTrial]


class AccuracyResult(TypedDict):
    """Current accuracy state exposed through the demand forecast."""

    pending: PendingTrial | None
    completed: list[CompletedTrial]


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
        self.previous = None
        self.pending = None
        self.completed = []
        if not isinstance(saved, dict) or saved.get("version") != 1:
            return
        # Historical display is deliberately not restored from unchecked payloads.
        trial = saved.get("pending")
        if not isinstance(trial, dict):
            return
        try:
            raw_times = [trial[k] for k in ("start", "end", "last")]
            if not all(isinstance(value, str) for value in raw_times):
                return
            start, end, last = [datetime.fromisoformat(value) for value in raw_times]
            if any(t.utcoffset() is None for t in (start, end, last)):
                return
            if not start <= last <= end or not 0 < (end - start).total_seconds() <= 86400:
                return
            raw_numbers = [
                trial[key]
                for key in ("predicted_kwh", "actual_kwh", "covered_seconds", "missing_seconds")
            ]
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float, str))
                for value in raw_numbers
            ):
                return
            predicted_kwh = float(trial["predicted_kwh"])
            actual_kwh = float(trial["actual_kwh"])
            covered_seconds = float(trial["covered_seconds"])
            missing_seconds = float(trial["missing_seconds"])
            numbers = (predicted_kwh, actual_kwh, covered_seconds, missing_seconds)
            if not all(isfinite(value) and value >= 0 for value in numbers):
                return
            if (
                covered_seconds + missing_seconds > 86400
                or max(predicted_kwh, actual_kwh) > 1200
            ):
                return
            pending: PendingTrial = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "last": last.isoformat(),
                "profile_ready": trial.get("profile_ready") is True,
                "predicted_kwh": predicted_kwh,
                "actual_kwh": actual_kwh,
                "covered_seconds": covered_seconds,
                "missing_seconds": missing_seconds,
            }
            self.pending = pending
        except (ValueError, TypeError, KeyError, OverflowError):
            return

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
                        "note": "House energy until predicted PV onset; not avoided grid import",
                    }
                    self.completed = (self.completed + [result])[-7:]
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
                    if isfinite(candidate_value) and 0 <= candidate_value <= 1200:
                        expected_value = candidate_value
                valid_window = (
                    candidate_end is not None
                    and candidate_end.utcoffset() is not None
                    and 900 <= (candidate_end - now).total_seconds() <= 86400
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
