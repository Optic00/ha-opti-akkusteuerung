"""Shadow check: would PV charging wait for today's price window below feed-in?

Observation only. Neither the strategy nor any write reads this result. It
records how often a hold would apply and how much energy the battery charged
before the window and how much was exported inside it, as a basis for deciding
whether a switchable rule is worth building (#136).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta, tzinfo
from math import isfinite
from typing import Any

from .arbitrage import _price_periods

# P10 PV surplus after the window start must exceed the missing energy by this
# factor; charging losses and load errors are covered by the same margin.
PV_MARGIN = 1.2
GOOD_SCORE = 3
# Model charge efficiency plus feed-in tariff, MinSOC and MaxSOC.
ASSUMPTION_COUNT = 4
# Snapshots before the battery model had three assumptions and charged with 0.9.
LEGACY_CHARGE_EFFICIENCY = 0.9
MAX_INTERVAL = 90
_COUNTERS = (
    "ready_seconds",
    "hold_seconds",
    "window_seconds",
    "charged_before_window_kwh",
    "charged_while_hold_kwh",
    "charged_in_window_kwh",
    "exported_in_window_kwh",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    return result if isfinite(result) else None


def _window(periods: Sequence[tuple[datetime, datetime, float]], threshold: float, now: datetime,
            day_end: datetime) -> tuple[datetime, datetime, float] | None:
    """First run below the feed-in tariff that has not ended yet today.

    The source grid is gapless, so consecutive periods form one run.
    """
    run: list[tuple[datetime, datetime, float]] = []
    for period in periods:
        if period[0] >= day_end:
            break
        if period[2] < threshold:
            run.append(period)
            continue
        if run and run[-1][1] > now:
            break
        run = []
    if not run or run[-1][1] <= now:
        return None
    return run[0][0], run[-1][1], min(price for _, _, price in run)


def negative_price_hold_check(
    *,
    now: datetime,
    timezone: tzinfo,
    price_series: Any,
    feed_in_ct: Any,
    pv_slots: Sequence[tuple[datetime, float]],
    load_w: Any,
    score: Any,
    soc: Any,
    capacity_kwh: Any,
    minsoc: Any,
    maxsoc: Any,
    peak_reserve_active: bool,
    peak_reserve_soc: Any,
    charge_efficiency: float,
) -> dict[str, Any]:
    """Return the verdict for this cycle; never raises on bad input.

    ``charge_efficiency`` kommt aus dem Akkumodell (AC -> stored).
    """
    out: dict[str, Any] = {"observation_only": True, "controls_battery": False}
    threshold = _number(feed_in_ct)
    if threshold is None:
        return {**out, "status": "data_missing", "detail": "feed_in"}
    local_now = now.astimezone(timezone)
    day_end = datetime.combine(local_now.date() + timedelta(days=1), time.min,
                               tzinfo=timezone).astimezone(UTC)
    periods = _price_periods(price_series, now, timezone)
    if not periods:
        return {**out, "status": "data_missing", "detail": "price_series"}
    window = _window(periods, threshold, now, day_end)
    if window is None:
        return {**out, "status": "ready", "verdict": "no_window"}
    start, end, lowest = window
    out.update(window_start=start.isoformat(), window_end=end.isoformat(),
               window_min_price_ct=round(lowest, 2), feed_in_ct=threshold)
    if start <= now:
        return {**out, "status": "ready", "verdict": "in_window"}
    values = {"score": _number(score), "soc": _number(soc), "capacity": _number(capacity_kwh),
              "minsoc": _number(minsoc), "maxsoc": _number(maxsoc), "load": _number(load_w)}
    missing = [key for key, value in values.items() if value is None]
    if missing or not values["capacity"] > 0 or not 0 <= values["soc"] <= 100 or values["load"] < 0:
        return {**out, "status": "data_missing", "detail": missing[0] if missing else "battery"}
    if values["score"] < GOOD_SCORE:
        return {**out, "status": "ready", "verdict": "forecast_weak"}
    if values["soc"] <= values["minsoc"]:
        return {**out, "status": "ready", "verdict": "min_soc"}
    reserve = _number(peak_reserve_soc)
    if peak_reserve_active and reserve is None:
        return {**out, "status": "data_missing", "detail": "peak_reserve"}
    if peak_reserve_active and values["soc"] <= reserve:
        return {**out, "status": "ready", "verdict": "peak_reserve"}
    needed = values["capacity"] * max(0.0, values["maxsoc"] - values["soc"]) / 100
    out["needed_kwh"] = round(needed, 2)
    if needed <= 0:
        return {**out, "status": "ready", "verdict": "full"}
    if not pv_slots or pv_slots[-1][0] + timedelta(minutes=30) < day_end - timedelta(hours=3):
        return {**out, "status": "data_missing", "detail": "pv_timing"}
    available = 0.0
    for slot_start, pv_w in pv_slots:
        slot_end = slot_start + timedelta(minutes=30)
        begin, finish = max(slot_start, start), min(slot_end, day_end)
        if finish > begin:
            surplus = max(0.0, pv_w - values["load"])
            available += surplus * (finish - begin).total_seconds() / 3600 / 1000
    available *= charge_efficiency
    out["pv_surplus_from_window_kwh"] = round(available, 2)
    hold = available >= needed * PV_MARGIN
    return {**out, "status": "ready", "verdict": "hold" if hold else "insufficient_pv"}


class NegativePriceHoldShadow:
    """Time-weighted totals; zero-order hold over the previous check.

    Gaps over 90 s, the first check after a restart and backwards clock steps
    never count. A changed charge efficiency, feed-in tariff or SoC range restarts
    the totals.
    """

    def __init__(self) -> None:
        self.data = self._fresh()
        self._previous: tuple[datetime, dict[str, Any], float | None, float | None] | None = None

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {"version": 1, "started_at": None, "last_observation": None,
                "assumptions": None, **dict.fromkeys(_COUNTERS, 0.0)}

    def snapshot(self) -> dict[str, Any]:
        return dict(self.data)

    def restore(self, saved: Any) -> None:
        self.data = self._fresh()
        self._previous = None
        if not isinstance(saved, Mapping) or saved.get("version") != 1:
            return
        try:
            for key in ("started_at", "last_observation"):
                raw = saved.get(key)
                if raw is not None and datetime.fromisoformat(raw).tzinfo is None:
                    return
            assumptions = saved.get("assumptions")
            if isinstance(assumptions, list) and len(assumptions) == ASSUMPTION_COUNT - 1:
                assumptions = [LEGACY_CHARGE_EFFICIENCY, *assumptions]
            if assumptions is not None and (
                not isinstance(assumptions, list) or len(assumptions) != ASSUMPTION_COUNT
                or any(item is not None and _number(item) is None for item in assumptions)
            ):
                return
            counters = {key: _number(saved.get(key)) for key in _COUNTERS}
            if any(value is None or value < 0 for value in counters.values()):
                return
        except TypeError, ValueError, AttributeError:
            return
        self.data = {"version": 1, "started_at": saved.get("started_at"),
                     "last_observation": saved.get("last_observation"),
                     "assumptions": assumptions, **counters}

    def interrupt(self) -> None:
        self._previous = None

    def totals(self) -> dict[str, Any]:
        out: dict[str, Any] = {key: round(self.data[key], 3) for key in _COUNTERS}
        out["started_at"] = self.data["started_at"]
        out["last_observation"] = self.data["last_observation"]
        return out

    def observe(self, now: datetime, check: Mapping[str, Any], assumptions: list[Any],
                battery_power_w: Any, export_w: Any) -> dict[str, Any]:
        if self.data["assumptions"] != assumptions:
            self.data = self._fresh()
            self.data["assumptions"] = list(assumptions)
            self.data["started_at"] = now.isoformat()
            self._previous = None
        elif self.data["started_at"] is None:
            self.data["started_at"] = now.isoformat()
        if self._previous is not None:
            before, held, power, export = self._previous
            elapsed = (now - before).total_seconds()
            if 0 < elapsed <= MAX_INTERVAL and held.get("status") == "ready":
                hours = elapsed / 3600
                charged = max(0.0, power) * hours / 1000 if power is not None else 0.0
                verdict = held.get("verdict")
                self.data["ready_seconds"] += elapsed
                if verdict == "in_window":
                    self.data["window_seconds"] += elapsed
                    self.data["charged_in_window_kwh"] += charged
                    if export is not None:
                        self.data["exported_in_window_kwh"] += max(0.0, export) * hours / 1000
                elif held.get("window_start"):
                    self.data["charged_before_window_kwh"] += charged
                if verdict == "hold":
                    self.data["hold_seconds"] += elapsed
                    self.data["charged_while_hold_kwh"] += charged
        self._previous = (now, dict(check), _number(battery_power_w), _number(export_w))
        self.data["last_observation"] = now.isoformat()
        return self.totals()
