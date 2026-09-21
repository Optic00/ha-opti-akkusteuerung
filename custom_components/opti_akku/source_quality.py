"""Reporting-age policy shared by external HA power sources and diagnostics."""

from datetime import datetime
import math
from typing import Any


def measurement_max_age(value: Any, *, event_based: bool = False) -> float:
    """Convert a stored option, never an effective limit; zero opts HA power in.

    Persisted infinity is invalid. Only this conversion may produce an
    unlimited effective limit; battery inputs stay bounded.
    """
    if isinstance(value, bool):
        return -math.inf
    try:
        age = float(value)
    except (TypeError, ValueError, OverflowError):
        return -math.inf
    if not math.isfinite(age) or age < 0:
        return -math.inf
    return (math.inf if event_based else 900.0) if age == 0 else age


def reported_recently(state: Any, now: datetime, max_age: float) -> bool:
    """An old report is not proof of failure for event-based HA sources."""
    reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    if not isinstance(reported, datetime) or reported.utcoffset() is None:
        return False
    try:
        age = (now - reported).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return False
    return -60 <= age <= max_age
