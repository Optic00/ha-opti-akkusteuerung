"""Reporting-age policy shared by external HA measurements and diagnostics."""

from datetime import datetime
import math
from typing import Any

# Solcast free plans refresh four times a day (#103); one cycle may be
# almost twelve hours old before the next fetch lands.
DEFAULT_FORECAST_MAX_AGE = 43200


def measurement_max_age(value: Any, *, event_based: bool = False) -> float:
    """Convert a stored option, never an effective limit; zero opts event-based HA measurements in.

    Persisted infinity is invalid. Only this conversion may produce an
    unlimited effective limit; native battery inputs stay bounded.
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


def house_consumption_max_age(options: Any) -> float:
    """Smoothed house sources may stay unchanged longer; unset inherits the general limit."""
    value = options.get("house_consumption_max_age")
    if value is None:
        value = options.get("source_max_age", 900)
    return measurement_max_age(value, event_based=True)
