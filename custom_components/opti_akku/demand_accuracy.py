"""Compare a frozen demand prediction with observed load, never claimed savings."""

from datetime import datetime
from math import isfinite
from copy import deepcopy


class DemandAccuracy:
    def __init__(self):
        self.pending = None
        self.completed = []
        self.previous = None

    def snapshot(self):
        return deepcopy({"version": 1, "pending": self.pending, "completed": self.completed})

    def restore(self, saved):
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
            start, end, last = [datetime.fromisoformat(trial[k]) for k in ("start", "end", "last")]
            if any(t.utcoffset() is None for t in (start, end, last)):
                return
            if not start <= last <= end or not 0 < (end - start).total_seconds() <= 86400:
                return
            nums = {
                k: float(trial[k])
                for k in ("predicted_kwh", "actual_kwh", "covered_seconds", "missing_seconds")
            }
            if not all(isfinite(v) and v >= 0 for v in nums.values()):
                return
            if (
                nums["covered_seconds"] + nums["missing_seconds"] > 86400
                or max(nums["predicted_kwh"], nums["actual_kwh"]) > 1200
            ):
                return
            self.pending = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "last": last.isoformat(),
                "profile_ready": trial.get("profile_ready") is True,
                **nums,
            }
        except ValueError, TypeError, KeyError:
            return

    def observe(self, now, house_w, prediction):
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
                valid = (
                    self.previous is not None
                    and self.previous[0] == last
                    and self.previous[1] is not None
                    and (now - last).total_seconds() <= 90
                )
                if valid:
                    trial["actual_kwh"] += self.previous[1] * seconds / 3600000
                    trial["covered_seconds"] += seconds
                else:
                    trial["missing_seconds"] += seconds
                trial["last"] = stop.isoformat()
                if now >= end:
                    result = deepcopy(trial)
                    covered = result["covered_seconds"]
                    total = covered + result["missing_seconds"]
                    result["coverage_percent"] = round(100 * covered / total, 1) if total else 0
                    result["error_kwh"] = (
                        round(result["actual_kwh"] - result["predicted_kwh"], 3)
                        if result["missing_seconds"] == 0
                        else None
                    )
                    result["note"] = (
                        "House energy until predicted PV onset; not avoided grid import"
                    )
                    self.completed = (self.completed + [result])[-7:]
                    self.pending = None
        if not self.pending and prediction.get("status") in ("learning", "ready"):
            end = datetime.fromisoformat(prediction["pv_cover_from"])
            if 900 <= (end - now).total_seconds() <= 86400:
                self.pending = {
                    "start": now.isoformat(),
                    "end": end.isoformat(),
                    "last": now.isoformat(),
                    "predicted_kwh": prediction["expected_load_kwh"],
                    "profile_ready": prediction.get("profile_ready") is True,
                    "actual_kwh": 0.0,
                    "covered_seconds": 0.0,
                    "missing_seconds": 0.0,
                }
        self.previous = (now, house_w)
        return {"pending": deepcopy(self.pending), "completed": deepcopy(self.completed)}
