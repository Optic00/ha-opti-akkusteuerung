"""Dated prices through HA's Tibber service; no credentials or global entities."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .sources import _price_slots, _price_timestamp, finite

FETCH_TIMEOUT_SECONDS = 15
REFRESH_SECONDS = 1800
RETRY_SECONDS = 300


class TibberPriceError(ValueError):
    """A public, data-free error suitable for config-flow and runtime diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PriceInterval:
    start: datetime
    end: datetime
    price_ct: float

    def as_slot(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "price": self.price_ct}


@dataclass(frozen=True)
class TibberPriceSnapshot:
    home: str
    fetched_at: datetime
    intervals: tuple[PriceInterval, ...]
    entry_id: str

    def normalized(self, now: datetime, max_age: float, home: str) -> tuple[float, dict, dict]:
        """Select today's actual intervals, never relabel the day of a cached array."""
        if home != self.home:
            raise TibberPriceError("tibber_home_mismatch")
        age = (now - self.fetched_at).total_seconds()
        limit = finite(max_age)
        if limit is None or limit <= 0 or not -60 <= age <= limit:
            raise TibberPriceError("missing_or_stale")
        today = dt_util.start_of_local_day(dt_util.as_local(now))
        tomorrow = today + timedelta(days=1)
        end = tomorrow + timedelta(days=1)
        starts = (today.astimezone(UTC), tomorrow.astimezone(UTC), end.astimezone(UTC))
        today_slots = [i.as_slot() for i in self.intervals if starts[0] <= i.start < starts[1]]
        tomorrow_slots = [i.as_slot() for i in self.intervals if starts[1] <= i.start < starts[2]]
        try:
            today_prices = _price_slots(today_slots, 1, today)
            tomorrow_prices = _price_slots(tomorrow_slots, 1, tomorrow) if tomorrow_slots else []
        except ValueError as err:
            raise TibberPriceError("invalid_price_series") from err
        current = next((i for i in self.intervals if i.start <= now < i.end), None)
        if current is None:
            raise TibberPriceError("missing_or_stale")
        return current.price_ct, {"today": today_prices, "tomorrow": tomorrow_prices}, {
            "start": current.start.isoformat(), "end": current.end.isoformat(),
        }


def _snapshot(home: str, raw: Any, start: datetime, fetched_at: datetime, entry_id: str) -> TibberPriceSnapshot:
    """Infer interval ends only from a complete, uniform, explicitly dated day."""
    if not isinstance(raw, list) or not raw:
        raise TibberPriceError("invalid_price_series")
    days = (start, start + timedelta(days=1), start + timedelta(days=2))
    boundaries = tuple(day.astimezone(UTC) for day in days)
    groups: list[list[tuple[datetime, float]]] = [[], []]
    previous = None
    try:
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("invalid slot")
            instant = _price_timestamp(item.get("start_time"))
            price = finite(item.get("price"))
            if price is None or finite(price * 100) is None:
                raise ValueError("invalid price")
            if not boundaries[0] <= instant < boundaries[2] or (previous is not None and instant <= previous):
                raise ValueError("invalid ordering or day")
            groups[0 if instant < boundaries[1] else 1].append((instant, price * 100))
            previous = instant
        intervals = []
        for index, group in enumerate(groups):
            if not group and index == 1:
                continue  # Tomorrow may not have been published yet.
            if not group:
                raise ValueError("missing today")
            step = (boundaries[index + 1] - boundaries[index]).total_seconds() / len(group)
            if step not in (900, 3600):
                raise ValueError("incomplete day")
            for offset, (instant, price_ct) in enumerate(group):
                if instant != boundaries[index] + timedelta(seconds=offset * step):
                    raise ValueError("gap or overlap")
                intervals.append(PriceInterval(instant, instant + timedelta(seconds=step), price_ct))
    except (ValueError, OverflowError) as err:
        raise TibberPriceError("invalid_price_series") from err
    snapshot = TibberPriceSnapshot(home, fetched_at, tuple(intervals), entry_id)
    snapshot.normalized(fetched_at, 60, home)
    return snapshot


async def async_fetch_prices(hass: HomeAssistant) -> dict[str, TibberPriceSnapshot]:
    """Return one validated home snapshot or a data-free TibberPriceError.

    HA's service selects its first config entry and keys homes by nickname.
    This version deliberately rejects multiple entries or response homes.
    Currency is not returned by HA; the caller must require explicit EUR consent.
    """
    entries = hass.config_entries.async_entries("tibber")
    if len(entries) != 1:
        raise TibberPriceError("tibber_config_entries")
    entry_id = entries[0].entry_id
    start = dt_util.start_of_local_day()
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            response = await hass.services.async_call("tibber", "get_prices", {
                "start": start.isoformat(), "end": (start + timedelta(days=2)).isoformat(),
            }, blocking=True, return_response=True)
    except TimeoutError as err:
        raise TibberPriceError("tibber_timeout") from err
    except Exception as err:
        raise TibberPriceError("tibber_fetch_failed") from err
    # Recheck selection in case setup/removal happened during the awaited call.
    if [e.entry_id for e in hass.config_entries.async_entries("tibber")] != [entry_id]:
        raise TibberPriceError("tibber_config_entries")
    prices = response.get("prices") if isinstance(response, dict) else None
    if not isinstance(prices, dict) or len(prices) != 1:
        raise TibberPriceError("tibber_home_count")
    home, raw = next(iter(prices.items()))
    if not isinstance(home, str) or not home:
        raise TibberPriceError("tibber_home_count")
    return {home: _snapshot(home, raw, start, dt_util.utcnow(), entry_id)}
