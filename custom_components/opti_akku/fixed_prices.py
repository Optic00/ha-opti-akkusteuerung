"""Fixed daily price windows, e.g. a tariff with a cheap night window.

The user enters local wall-clock windows with a price in ct/kWh. They must
cover the day without gaps or overlaps; one window may cross midnight. The
dated today/tomorrow series are generated on a real-time grid from local
midnight to the next local midnight, so DST days have 23 or 25 hours (92 or
100 quarter hours). Every slot takes the price of the window that contains
its local start time. The current price comes from the same slots.

There is no provider and therefore no data age: the configured windows are
the tariff. A changed tariff needs a changed configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .sources import _price_slots, finite

DAY_MINUTES = 1440
WINDOW_STEP_MINUTES = 15
MAX_WINDOWS = 96
# Plausibility bound in ct/kWh; negative prices remain allowed.
MAX_ABS_PRICE_CT = 1000


class FixedPriceError(ValueError):
    """A data-free error code for config flow and source diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PriceWindow:
    start: int  # minutes after local midnight
    end: int  # minutes after local midnight; 0 means midnight
    price_ct: float

    @property
    def duration(self) -> int:
        # A single window with start == end covers the whole day.
        return (self.end - self.start) % DAY_MINUTES or DAY_MINUTES

    def contains(self, minute: int) -> bool:
        return (minute - self.start) % DAY_MINUTES < self.duration


def _minutes(value: Any) -> int:
    """Accept HH:MM or HH:MM:SS (TimeSelector) on the quarter-hour grid."""
    if not isinstance(value, str):
        raise FixedPriceError("price_windows_invalid")
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() and len(part) == 2 for part in parts):
        raise FixedPriceError("price_windows_invalid")
    hour, minute, second = (int(part) for part in (*parts, "00")[:3])
    if hour > 23 or minute > 59 or second != 0:
        raise FixedPriceError("price_windows_invalid")
    if minute % WINDOW_STEP_MINUTES:
        raise FixedPriceError("price_windows_grid")
    return hour * 60 + minute


def parse_windows(raw: Any) -> tuple[PriceWindow, ...]:
    """Validate configured windows; return them sorted by local start time."""
    if not isinstance(raw, (list, tuple)) or not raw or len(raw) > MAX_WINDOWS:
        raise FixedPriceError("price_windows_invalid")
    windows = []
    for item in raw:
        if not isinstance(item, dict):
            raise FixedPriceError("price_windows_invalid")
        price = finite(item.get("price"))
        if price is None or abs(price) > MAX_ABS_PRICE_CT:
            raise FixedPriceError("price_windows_invalid")
        windows.append(PriceWindow(_minutes(item.get("start")), _minutes(item.get("end")), price))
    windows.sort(key=lambda window: window.start)
    if len(windows) > 1 and any(window.start == window.end for window in windows):
        # A full-day window next to another window always overlaps it.
        raise FixedPriceError("price_windows_overlap")
    total = sum(window.duration for window in windows)
    if total < DAY_MINUTES:
        raise FixedPriceError("price_windows_gap")
    if total > DAY_MINUTES:
        raise FixedPriceError("price_windows_overlap")
    # With exactly 24 h in total, any break in the chain means a gap and an
    # overlap at the same time; report the overlap.
    for index, window in enumerate(windows):
        if window.end % DAY_MINUTES != windows[(index + 1) % len(windows)].start:
            raise FixedPriceError("price_windows_overlap")
    return tuple(windows)


def _price_at(windows: tuple[PriceWindow, ...], instant: datetime) -> float:
    local = dt_util.as_local(instant)
    minute = local.hour * 60 + local.minute
    return next(window.price_ct for window in windows if window.contains(minute))


def _grid_seconds(windows: tuple[PriceWindow, ...], days: tuple[datetime, ...]) -> int:
    """Hourly when every window boundary and both local days allow it."""
    if any(window.start % 60 or window.end % 60 for window in windows):
        return 900
    for day in days:
        # A half-hour DST shift (e.g. Lord Howe Island) changes the day length
        # by 30 minutes; whole real-time hours would then miss local hours.
        length = ((day + timedelta(days=1)).astimezone(UTC) - day.astimezone(UTC)).total_seconds()
        if length % 3600:
            return 900
    return 3600


def _day_slots(windows: tuple[PriceWindow, ...], day: datetime, step: int) -> list[dict[str, Any]]:
    start = day.astimezone(UTC)
    end = (day + timedelta(days=1)).astimezone(UTC)
    slots = []
    instant = start
    while instant < end:
        following = instant + timedelta(seconds=step)
        slots.append({"start": instant, "end": following, "price": _price_at(windows, instant)})
        instant = following
    return slots


def fixed_price_plan(raw: Any, now: datetime) -> tuple[float, dict[str, list[float]], dict[str, str]]:
    """Current price, dated series and current interval from the configured windows."""
    windows = parse_windows(raw)
    today = dt_util.start_of_local_day(dt_util.as_local(now))
    tomorrow = today + timedelta(days=1)
    step = _grid_seconds(windows, (today, tomorrow))
    today_slots = _day_slots(windows, today, step)
    tomorrow_slots = _day_slots(windows, tomorrow, step)
    try:
        # Reuse the shared validator so the engine sees the same contract as
        # for sensor and Tibber series.
        series = {"today": _price_slots(today_slots, 1, today),
                  "tomorrow": _price_slots(tomorrow_slots, 1, tomorrow)}
    except ValueError as err:
        raise FixedPriceError("invalid_price_series") from err
    current = next((slot for slot in today_slots if slot["start"] <= now < slot["end"]), None)
    if current is None:
        raise FixedPriceError("invalid_price_series")
    return current["price"], series, {"start": current["start"].isoformat(), "end": current["end"].isoformat()}


def format_windows(raw: Any) -> str:
    """Language-neutral summary for the setup overview."""
    try:
        windows = parse_windows(raw)
    except FixedPriceError:
        return "—"
    return ", ".join(
        f"{window.start // 60:02d}:{window.start % 60:02d}–{window.end // 60:02d}:{window.end % 60:02d} "
        f"{window.price_ct:g} ct/kWh"
        for window in windows
    )


def normalized_windows(raw: Any) -> list[dict[str, Any]]:
    """Stored form: HH:MM strings and float prices, sorted by start."""
    return [
        {"start": f"{window.start // 60:02d}:{window.start % 60:02d}",
         "end": f"{window.end // 60:02d}:{window.end % 60:02d}",
         "price": window.price_ct}
        for window in parse_windows(raw)
    ]
