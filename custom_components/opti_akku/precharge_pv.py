"""PV forecast hours for the peak precharge in the strategy (#155).

The strategy charges the battery from the grid ahead of a price peak
(``peak_precharge``). Without this input it ignored the PV that reaches the
battery before the peak anyway, so grid energy displaced PV that was then
exported. This module turns the Solcast interval forecast into hourly P10
energy that ``sensor.opti_peak_reserve_soc`` nets against the load profile up
to the start of the first peak. P10 deliberately, not the median: on cloudy
days the median overshot by a factor of three (#155, 16.10.).

A missing, stale or invalid forecast yields no hours. The strategy then
credits no PV and precharges exactly as before; this input can only lower the
precharge target, never raise it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .planner_shadow import pv_forecast_hourly

ENTITY = "sensor.opti_pv_forecast_hours"
QUANTILE = "p10"
# The peak reserve horizon spans at most 36 hours from now.
MAX_HOURS = 37


def pv_forecast_hours(
    get_state: Callable[[str], Any],
    source_pairs: Iterable[tuple[str | None, str | None]],
    now: datetime,
    *,
    max_age: float,
) -> tuple[str, dict[str, Any]]:
    """State and attributes of :data:`ENTITY`.

    ``source_pairs`` are (today, tomorrow) forecast entities in order of
    preference, like the shadow planner: the configured forecast first, then
    the demand forecast's Solcast source. The first pair that yields a ready
    forecast wins. ``hours`` maps the UTC hour start in epoch seconds, as text
    like ``sensor.opti_peak_load_profile``, to the P10 energy of that hour in
    kWh. It starts with the current hour and ends before the first gap.
    """
    status, source = "no_forecast", None
    hourly: dict[datetime, tuple[float | None, float | None]] = {}
    for today, tomorrow in source_pairs:
        entities = [entity for entity in (today, tomorrow) if entity]
        if not entities:
            continue
        status, hourly = pv_forecast_hourly([get_state(entity) for entity in entities], now,
                                            max_age=max_age)
        source = entities[0]
        if status == "ready":
            break
    hours: dict[str, float] = {}
    if status == "ready":
        hour = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        end = now + timedelta(hours=MAX_HOURS)
        # Contiguous from the current hour. A gap or a missing P10 (kept as
        # None by pv_forecast_hourly) ends the hours instead of reading as zero.
        while hour < end and (pair := hourly.get(hour)) is not None and pair[0] is not None:
            hours[str(int(hour.timestamp()))] = pair[0]
            hour += timedelta(hours=1)
        if not hours:
            status = "no_forecast"
    return status, {"status": status, "quantile": QUANTILE, "source": source, "hours": hours}
