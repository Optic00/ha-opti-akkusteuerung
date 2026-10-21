"""Expected PV energy above the AC surplus limit today. Observation only.

The estimate never feeds the strategy or any write. It answers one question for
a later shadow comparison: how much free battery capacity would be needed when
PV exceeds the configured AC limit. Solcast models AC output; a site configured
with the inverter as AC capacity is already capped and then shows a plateau.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from math import isfinite
from typing import Any

from .source_quality import DEFAULT_FORECAST_MAX_AGE

SLOT = timedelta(minutes=30)
ESTIMATES = (("p50", "pv_estimate"), ("p10", "pv_estimate10"), ("p90", "pv_estimate90"))
# Several slots at the same peak suggest Solcast already caps at its AC capacity.
PLATEAU_TOLERANCE = 0.005
PLATEAU_MIN_SLOTS = 3


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    return result if isfinite(result) else None


def _instant(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("An explicit timezone is required")
    return value.astimezone(UTC)


def _slots(state: Any, now: datetime, timezone: tzinfo, max_age: float):
    """Today's half-hour slots as {start: {p50, p10, p90 in W}}, or a status."""
    if state is None or state.state in ("unknown", "unavailable"):
        return "no_forecast", {}
    detail = state.attributes.get("detailedForecast")
    if not isinstance(detail, list) or not detail or len(detail) > 100:
        return "no_detail", {}
    try:
        age = (now - _instant(state.last_reported or state.last_updated)).total_seconds()
    except ValueError, TypeError, AttributeError:
        return "invalid", {}
    if max_age > 0 and not -60 <= age <= max_age:
        return "stale", {}
    today = now.astimezone(timezone).date()
    rows: dict[datetime, dict[str, float]] = {}
    try:
        for row in detail:
            start = _instant(row["period_start"])
            if start.astimezone(timezone).date() != today:
                continue
            values = {}
            for name, key in ESTIMATES:
                kw = _number(row.get(key))
                if kw is None or not 0 <= kw <= 1000:
                    raise ValueError("Missing or invalid PV estimate")
                values[name] = kw * 1000
            if start in rows and rows[start] != values:
                raise ValueError("Conflicting PV intervals")
            rows[start] = values
    except KeyError, ValueError, TypeError, AttributeError:
        return "invalid", {}
    ordered = sorted(rows)
    if not ordered:
        return "no_today_slots", {}
    if any(b - a < SLOT for a, b in zip(ordered, ordered[1:], strict=False)):
        return "invalid", {}
    return "ready", rows


def clipping_forecast(
    state: Any,
    limit_w: Any,
    now: datetime,
    timezone: tzinfo,
    *,
    soc: Any = None,
    capacity_kwh: Any = None,
    maxsoc: Any = None,
    max_age: float = DEFAULT_FORECAST_MAX_AGE,
) -> tuple[float | str, dict[str, Any]]:
    """Return (remaining P50 kWh or 'unavailable', attributes)."""
    attrs: dict[str, Any] = {"observation_only": True, "controls_battery": False}
    limit = _number(limit_w)
    if limit is None or limit <= 0:
        return "unavailable", {**attrs, "status": "not_configured"}
    attrs["limit_w"] = round(limit)
    status, rows = _slots(state, now, timezone, max_age)
    if status != "ready":
        return "unavailable", {**attrs, "status": status}
    remaining = {name: 0.0 for name, _ in ESTIMATES}
    day_p50 = 0.0
    clipped_starts = []
    for start, values in sorted(rows.items()):
        end = start + SLOT
        over = {name: max(0.0, values[name] - limit) for name, _ in ESTIMATES}
        day_p50 += over["p50"] * 0.5 / 1000
        if over["p50"] > 0:
            clipped_starts.append(start)
        hours = max(0.0, (end - max(start, now)).total_seconds()) / 3600
        for name in remaining:
            remaining[name] += over[name] * hours / 1000
    peak = max(values["p90"] for values in rows.values())
    plateau = sum(1 for values in rows.values() if values["p90"] >= peak * (1 - PLATEAU_TOLERANCE))
    attrs.update(
        status="ready",
        remaining_p10_kwh=round(remaining["p10"], 2),
        remaining_p90_kwh=round(remaining["p90"], 2),
        day_p50_kwh=round(day_p50, 2),
        first_clipping_start=clipped_starts[0].isoformat() if clipped_starts else None,
        last_clipping_end=(clipped_starts[-1] + SLOT).isoformat() if clipped_starts else None,
        forecast_peak_p90_w=round(peak),
        forecast_capped_suspected=peak > 0 and plateau >= PLATEAU_MIN_SLOTS,
    )
    soc_value, capacity, upper = _number(soc), _number(capacity_kwh), _number(maxsoc)
    if (soc_value is not None and capacity is not None and upper is not None
            and 0 <= soc_value <= 100 and capacity > 0 and 0 < upper <= 100):
        free = max(0.0, capacity * (upper - soc_value) / 100)
        attrs["free_capacity_kwh"] = round(free, 2)
        attrs["reserve_shortfall_kwh"] = round(max(0.0, remaining["p50"] - free), 2)
    else:
        attrs["free_capacity_kwh"] = None
        attrs["reserve_shortfall_kwh"] = None
    return round(remaining["p50"], 2), attrs
