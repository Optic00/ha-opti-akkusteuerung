"""PV forecast half-hours for the peak precharge in the strategy (#155).

The strategy charges the battery from the grid ahead of a price peak
(``peak_precharge``). Without this input it ignored the PV that reaches the
battery before the peak anyway, so grid energy displaced PV that was then
exported. This module turns the Solcast ``detailedForecast`` into half-hourly
P10 power that ``sensor.opti_peak_reserve_soc`` nets against the load profile
up to the start of the first peak. P10 deliberately, not the median: on cloudy
days the median overshot by a factor of three (#155, 16.10.). Half hours, not
hours: an hourly mean would credit a sunny first half hour to the rest of the
hour after it has passed.

A missing, stale or invalid forecast yields no half hours. The strategy then
credits no PV and precharges exactly as before; this input can only lower the
precharge target, never raise it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .demand import instant, number
from .source_quality import reported_at

ENTITY = "sensor.opti_pv_forecast_hours"
QUANTILE = "p10"
STEP = timedelta(minutes=30)
# The peak reserve horizon spans up to 36 hours; demand.pv_intervals stops at 25.
HORIZON = timedelta(hours=37)
ROW_LIMIT = 100


def _rows(states: Any, entities: Sequence[str], now: datetime,
          max_age: float) -> list[tuple[datetime, float]]:
    """Validated future P10 half hours (start, W) of ``detailedForecast``.

    Like ``demand.pv_intervals``, but up to :data:`HORIZON`. A stale or
    invalid entity, or conflicting rows, invalidate the whole result (fail
    closed: no PV credit).
    """
    rows: dict[datetime, float] = {}
    for entity in entities:
        state = states.get(entity)
        if state is None or state.state in ("unknown", "unavailable"):
            continue
        try:
            age = (now - instant(reported_at(state))).total_seconds()
            detail = state.attributes.get("detailedForecast")
            if not -60 <= age <= max_age or not isinstance(detail, list) or len(detail) > ROW_LIMIT:
                continue
            for row in detail:
                start = instant(row["period_start"])
                p10 = number(row.get("pv_estimate10"))
                if p10 is None or not 0 <= p10 <= 1000:
                    raise ValueError("Missing P10 power")
                if start + STEP <= now or start > now + HORIZON:
                    continue
                if start in rows and rows[start] != p10 * 1000:
                    raise ValueError("Conflicting PV intervals")
                rows[start] = p10 * 1000
        except KeyError, ValueError, TypeError, AttributeError:
            return []
    return sorted(rows.items())


def _contiguous(rows: Sequence[tuple[datetime, float]], now: datetime) -> dict[str, float]:
    """P10 kW per half hour, from the one covering ``now`` until the first gap."""
    halfhours: dict[str, float] = {}
    expected: datetime | None = None
    for start, watts in rows:
        start = start.astimezone(UTC)
        if expected is None:
            if start > now:
                return {}
        elif start != expected:
            break
        if start.minute % 30 or start.second or start.microsecond:
            break
        halfhours[str(int(start.timestamp()))] = round(watts / 1000, 4)
        expected = start + STEP
    return halfhours


def pv_forecast_hours(
    states: Any,
    source_pairs: Iterable[tuple[str | None, str | None]],
    now: datetime,
    *,
    max_age: float,
) -> tuple[str, dict[str, Any]]:
    """State and attributes of :data:`ENTITY`.

    ``source_pairs`` are (today, tomorrow) forecast entities: the configured
    forecast and the demand forecast's Solcast source. The pair whose
    contiguous P10 half hours reach furthest wins, the first on a tie, so a
    source covering only the current hour cannot shadow one that reaches the
    peak. ``halfhours`` maps the UTC half-hour start in epoch seconds, as
    text, to the mean P10 power of that half hour in kW.
    """
    best: dict[str, float] = {}
    source = None
    for today, tomorrow in source_pairs:
        if not today and not tomorrow:
            continue
        entities = [entity for entity in (today, tomorrow) if entity]
        halfhours = _contiguous(_rows(states, entities, now, max_age), now)
        if len(halfhours) > len(best):
            best, source = halfhours, today or tomorrow
    status = "ready" if best else "no_forecast"
    return status, {"status": status, "quantile": QUANTILE, "source": source, "halfhours": best}
