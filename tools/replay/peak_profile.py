"""Learned peak load profile (``sensor.opti_peak_load_profile``) for the replay.

The integration learns an hourly house profile in ``DemandForecast`` (cells
``date|hour|context`` with integrated watt-seconds) and turns it into the
attribute ``hours``/``methods`` mapping with ``peak_load_profile``. Both pieces
run here unchanged: ``DemandForecast._expected`` (median over the last 42 days,
same weekday or same weekday class, at least 30 min coverage per cell) and
``peak_load_profile`` (20 % margin, short-term uplift, fixed-value fallback per
hour, 37 hours ahead).

Approximation: live, ``DemandForecast._learn`` integrates point observations
with gaps of at most 90 s. The replay steps every 15 min, so each step adds the
previous step's mean house power for exactly that step's duration to the cell
of the step's local hour. Only completed steps are learned (no future
knowledge); no thermal or EV sources, so the context is always ``unknown``;
no recorder prior (``HistoricalPrior`` stays empty), so a fresh profile falls
back to the fixed value until it has learned enough days.
"""
from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import sys
import types
from collections import deque
from typing import Any

from .resources import REPO_ROOT

PACKAGE = "opti_replay_integration"
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "opti_akku"
PEAK_LOAD_PROFILE = "sensor.opti_peak_load_profile"
FALLBACK_SETTING = "input_number.opti_peak_verbrauch_kw"
FINGERPRINT = "replay"
CONTEXT = "unknown"
RETENTION_DAYS = 42
RECENT_WINDOW_S = 1800.0  # DemandForecast.recent window
RECENT_MIN_COVERAGE_S = 1200.0  # uplift needs this much recent coverage
STEP_TOLERANCE = dt.timedelta(milliseconds=1)
OPTIONS: dict[str, Any] = {"demand_forecast": {"enabled": True, "use_for_peak_reserve": True, "sources": {}}}


def load_integration_module(name: str) -> types.ModuleType:
    """Import a pure integration module with relative imports, without HA.

    A synthetic package points at the integration directory but never runs its
    ``__init__.py`` (which needs Home Assistant).
    """
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(INTEGRATION_DIR)]
        package.__spec__ = importlib.util.spec_from_loader(PACKAGE, loader=None, is_package=True)
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{name}")


class PeakProfileFeed:
    """Learn from completed replay steps and publish the integration's attribute."""

    def __init__(self, step_seconds: float) -> None:
        self.step = dt.timedelta(seconds=step_seconds)
        self._demand = load_integration_module("demand")
        self._peak = load_integration_module("peak_load")
        self.model = self._demand.DemandForecast()
        self._last: dt.datetime | None = None
        # Completed steps (start, end, mean W) for DemandForecast.recent's 30-min window.
        self._recent: deque[tuple[dt.datetime, dt.datetime, float]] = deque()
        self.status_counts: dict[str, int] = {}
        self.method_counts: dict[str, int] = {}

    def reset(self) -> None:
        """Measurement gap: keep the learned cells (stored live), cut continuity."""
        self._last = None
        self._recent.clear()
        self.model.previous = None

    def _learn(self, now: dt.datetime, load_w: float) -> None:
        """Add the previous step (``_last`` .. ``now``) with its mean power."""
        last = self._last
        self._last = now
        # Same-zone subtraction would use wall time and break at the DST fold.
        # Sub-steps like 3600/11 s round to whole microseconds, hence the tolerance.
        gap = (now.astimezone(dt.UTC) - last.astimezone(dt.UTC)) if last is not None else None
        if gap is None or abs(gap - self.step) > STEP_TOLERANCE or not 0 <= load_w <= 50000:
            self._recent.clear()
            return
        zone = now.tzinfo
        cursor, stop = last.astimezone(dt.UTC), now.astimezone(dt.UTC)
        while cursor < stop:
            # Split at UTC minute boundaries as _learn does: local hours and DST folds stay exact.
            end = min(stop, cursor.replace(second=0, microsecond=0) + dt.timedelta(minutes=1))
            local = cursor.astimezone(zone)
            key = f"{local.date().isoformat()}|{local.hour}|{CONTEXT}"
            row = self.model.cells.setdefault(key, [0.0, 0.0, 0.0, 0.0])
            seconds = (end - cursor).total_seconds()
            row[0] += load_w * seconds
            row[3] += seconds
            cursor = end
        cutoff = (now - dt.timedelta(days=RETENTION_DAYS)).date().isoformat()
        today = now.date().isoformat()
        self.model.cells = {k: v for k, v in self.model.cells.items() if cutoff <= k[:10] <= today}
        self._recent.append((last.astimezone(dt.UTC), now.astimezone(dt.UTC), load_w))
        while self._recent and (stop - self._recent[0][1]).total_seconds() >= RECENT_WINDOW_S:
            self._recent.popleft()

    def _uplift(self, now: dt.datetime) -> float:
        """``extra_base_load_w`` of DemandForecast: recent mean above the profile."""
        # Time-weighted mean over the window, steps cut at its start like LoadProfile.
        cutoff = now.astimezone(dt.UTC) - dt.timedelta(seconds=RECENT_WINDOW_S)
        covered = energy = 0.0
        for start, end, value in self._recent:
            seconds = (end - max(start, cutoff)).total_seconds()
            if seconds > 0:
                covered += seconds
                energy += value * seconds
        if covered < RECENT_MIN_COVERAGE_S:
            return 0.0
        expected = self.model._expected(now, CONTEXT, now.tzinfo)
        if expected is None:
            return 0.0
        return max(0.0, energy / covered - expected[0])

    def attributes(self, now: dt.datetime, load_w: float, settings: dict[str, Any]) -> dict[str, Any]:
        """The attribute dict the coordinator would publish at ``now``."""
        self._learn(now, load_w)
        timezone = now.tzinfo
        self.model.fingerprint = self._demand.DemandForecast.profile_binding(FINGERPRINT, OPTIONS, timezone)
        uplift = self._uplift(now)
        # recent_fresh in peak_load_profile: a current observation exists.
        self.model.previous = (now.astimezone(dt.UTC), load_w, 0.0, 0.0, CONTEXT)
        data = {
            "online": True,
            "states": {"sensor.opti_house_consumption_w": load_w},
            "source_errors": {},
            "demand_forecast": {"extra_base_load_w": uplift},
        }
        result: dict[str, Any] = self._peak.peak_load_profile(
            self.model, now, data, settings, OPTIONS, {}, timezone, FINGERPRINT)
        self.status_counts[result["status"]] = self.status_counts.get(result["status"], 0) + 1
        first = result["methods"].get(str(int(now.replace(minute=0, second=0, microsecond=0).timestamp())))
        if first:
            self.method_counts[first] = self.method_counts.get(first, 0) + 1
        return result
