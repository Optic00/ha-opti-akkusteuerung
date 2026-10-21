"""Run a policy through the hourly series in fixed sub-hour steps.

Assumption: each recorded hour is spread evenly over its steps (default four
quarter hours). This smooths real intra-hour peaks, so surplus and deficit
magnitudes are hourly means. The engine sees the previous step's powers as its
"live" measurements and the simulated SoC as the battery state.

Sub-steps advance on the UTC axis so DST days keep their 23/25 local hours
without duplicated or backwards timestamps. Measurement blocks separated by
more than ``BLOCK_GAP`` restart the simulation: SoC from the first measured
hour, a fresh policy state, and ``WARMUP_HOURS`` of records flagged as warm-up.
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from . import battery
from .policies import Decision, PowerSnapshot, StepContext
from .timeseries import Hour

TOMORROW_KNOWN_FROM_HOUR = 13
BLOCK_GAP = dt.timedelta(hours=6)
WARMUP_HOURS = 24
FORECAST_MODES = ("calibrated", "naive")


@dataclass
class StepRecord:
    ts_local: dt.datetime
    policy: str
    mode: str
    decision_id: str
    reason: str
    pv_kwh: float
    load_kwh: float
    charge_kwh: float
    discharge_kwh: float
    export_kwh: float
    import_kwh: float
    soc_before: float
    soc_after: float
    price_ct: float | None
    charge_limit_w: float | None
    target_soc: float | None
    engine_ok: bool
    warmup: bool = False
    discharge_limit_w: float | None = None
    night: bool = False  # sun below the horizon at the step (sun model)
    score: int | None = None  # sensor.opti_forecast_score
    score_sonnentag: int | None = None  # sensor.opti_forecast_score_sonnentag
    horizon_long: bool | None = None  # binary_sensor.opti_peak_horizont_lang


@dataclass
class DayTable:
    prices: list[float] | None
    pv_total: float
    pv_before: dict[int, float]  # hour index within the day -> pv before that hour


def day_tables(hours: list[Hour]) -> dict[dt.date, DayTable]:
    """Per-day price lists (only complete days) and cumulative PV profile."""
    by_day: dict[dt.date, list[Hour]] = {}
    for hour in hours:
        by_day.setdefault(hour.day, []).append(hour)
    tables = {}
    for day, items in by_day.items():
        start = dt.datetime.combine(day, dt.time(), tzinfo=items[0].ts_local.tzinfo)
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=start.tzinfo)
        expected = round((end.timestamp() - start.timestamp()) / 3600)
        prices = [item.price_ct for item in items]
        complete = len(items) == expected and all(value is not None for value in prices)
        pv_before, running = {}, 0.0
        for index, item in enumerate(items):
            pv_before[index] = running
            running += item.pv_kwh or 0.0
        tables[day] = DayTable(prices if complete else None, running, pv_before)
    return tables


def climatology(hours: list[Hour]) -> dict[tuple[int, int], list[float]]:
    """Share of the month's PV per local hour of day, keyed by (year, month)."""
    sums: dict[tuple[int, int], list[float]] = {}
    for hour in hours:
        if hour.pv_kwh is None:
            continue
        key = (hour.ts_local.year, hour.ts_local.month)
        sums.setdefault(key, [0.0] * 24)[hour.ts_local.hour] += hour.pv_kwh
    return {key: [v / total if (total := sum(values)) > 0 else 0.0 for v in values] for key, values in sums.items()}


def load_day_climatology(hours: list[Hour]) -> dict[tuple[int, int], float]:
    """Mean daily load (kWh) per (year, month): sum of the mean load per local hour of day."""
    sums: dict[tuple[int, int], list[float]] = {}
    counts: dict[tuple[int, int], list[int]] = {}
    for hour in hours:
        if hour.load_kwh is None:
            continue
        key = (hour.ts_local.year, hour.ts_local.month)
        sums.setdefault(key, [0.0] * 24)[hour.ts_local.hour] += hour.load_kwh
        counts.setdefault(key, [0] * 24)[hour.ts_local.hour] += 1
    return {key: sum(s / c for s, c in zip(values, counts[key], strict=True) if c)
            for key, values in sums.items()}


def previous_month(key: tuple[int, int]) -> tuple[int, int]:
    year, month = key
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _remaining_calibrated(hour: Hour, table: DayTable, index: int, fraction: float) -> float | None:
    """Today's forecast scaled by the share of the day's actual PV still ahead.

    Uses the actual later PV profile of the same day (future knowledge).
    """
    if hour.forecast_today_kwh is None:
        return None
    if table.pv_total <= 0:
        return hour.forecast_today_kwh if hour.ts_local.hour < 16 else 0.0
    produced = table.pv_before[index] + (hour.pv_kwh or 0.0) * fraction
    return hour.forecast_today_kwh * max(0.0, 1 - produced / table.pv_total)


def _remaining_naive(hour: Hour, profile: list[float], fraction: float) -> float | None:
    """Today's forecast times the climatological share of the day still ahead."""
    if hour.forecast_today_kwh is None:
        return None
    ahead = sum(profile[hour.ts_local.hour + 1:]) + profile[hour.ts_local.hour] * (1 - fraction)
    return hour.forecast_today_kwh * ahead


def _blocks(hours: list[Hour]) -> list[list[int]]:
    """Indices of complete hours grouped into measurement blocks."""
    blocks: list[list[int]] = []
    for index, hour in enumerate(hours):
        if not hour.complete:
            continue
        if blocks and hour.ts_local - hours[blocks[-1][-1]].ts_local <= BLOCK_GAP:
            blocks[-1].append(index)
        else:
            blocks.append([index])
    return blocks


def run(
    hours: list[Hour],
    policy: Any,
    params: battery.BatteryParams,
    *,
    steps_per_hour: int = 4,
    start_soc: float | None = None,
    forecast_mode: str = "calibrated",
) -> list[StepRecord]:
    if forecast_mode not in FORECAST_MODES:
        raise ValueError(f"Unknown forecast mode: {forecast_mode}")
    tables = day_tables(hours)
    profiles = climatology(hours) if forecast_mode == "naive" else {}
    load_days = load_day_climatology(hours)
    sun = getattr(policy, "sun", None)
    index_in_day: dict[int, int] = {}
    counter: dict[dt.date, int] = {}
    for position, hour in enumerate(hours):
        index_in_day[position] = counter.get(hour.day, 0)
        counter[hour.day] = index_in_day[position] + 1
    step_h = 1 / steps_per_hour
    records: list[StepRecord] = []
    for block_number, block in enumerate(_blocks(hours)):
        first = hours[block[0]]
        if block_number > 0 or start_soc is None:
            soc = first.soc if first.soc is not None else 50.0
        else:
            soc = start_soc
        soc = min(params.maxsoc, max(params.minsoc, soc))
        if block_number > 0:
            policy.reset()
        warmup_until = first.ts_local + dt.timedelta(hours=WARMUP_HOURS)
        previous = PowerSnapshot()
        for position in block:
            hour = hours[position]
            table = tables[hour.day]
            tomorrow = tables.get(hour.day + dt.timedelta(days=1))
            pv_kwh, load_kwh = hour.pv_kwh / steps_per_hour, hour.load_kwh / steps_per_hour
            start_utc = hour.ts_local.astimezone(dt.UTC)
            month_key = (hour.ts_local.year, hour.ts_local.month)
            profile = profiles.get(previous_month(month_key)) or profiles.get(month_key) or [0.0] * 24
            load_day = load_days.get(previous_month(month_key), load_days.get(month_key))
            for sub in range(steps_per_hour):
                fraction = sub / steps_per_hour
                now = (start_utc + dt.timedelta(hours=fraction)).astimezone(hour.ts_local.tzinfo)
                remaining = (
                    _remaining_naive(hour, profile, fraction) if forecast_mode == "naive"
                    else _remaining_calibrated(hour, table, index_in_day[position], fraction)
                )
                context = StepContext(
                    now=now, soc=soc, previous=previous, price_ct=hour.price_ct,
                    price_today=table.prices,
                    price_tomorrow=(tomorrow.prices or []) if tomorrow and now.hour >= TOMORROW_KNOWN_FROM_HOUR else [],
                    forecast_today_kwh=hour.forecast_today_kwh,
                    forecast_tomorrow_kwh=hour.forecast_tomorrow_kwh,
                    forecast_remaining_kwh=remaining,
                    load_day_kwh=load_day,
                )
                decision: Decision = policy.decide(context)
                extra = decision.extra
                flows = battery.step(decision.mode, soc, pv_kwh, load_kwh, step_h, params, decision.charge_limit_w,
                                     decision.discharge_limit_w)
                records.append(StepRecord(
                    now, policy.name, decision.mode, decision.decision_id, decision.reason,
                    pv_kwh, load_kwh, flows.charge_kwh, flows.discharge_kwh, flows.export_kwh, flows.import_kwh,
                    soc, flows.soc_after, hour.price_ct, decision.charge_limit_w, decision.target_soc, decision.ok,
                    warmup=block_number > 0 and now < warmup_until,
                    discharge_limit_w=decision.discharge_limit_w,
                    night=sun.state(now)[0] == "below_horizon" if sun is not None else False,
                    score=_int_or_none(extra.get("score")),
                    score_sonnentag=_int_or_none(extra.get("score_sonnentag")),
                    horizon_long=_on_off(extra.get("horizon_long")),
                ))
                soc = flows.soc_after
                factor = 1000 / step_h
                previous = PowerSnapshot(
                    pv_kwh * factor, load_kwh * factor, flows.export_kwh * factor, flows.import_kwh * factor,
                    (flows.charge_kwh - flows.discharge_kwh) * factor,
                )
    return records


def run_actual(hours: list[Hour], steps_per_hour: int = 4) -> list[StepRecord]:
    """Replay the recorded flows unchanged as the reference policy ``actual``.

    The hourly maximum SoC stands in for the per-step SoC, so export with a
    free battery is only counted when it stayed below the cap for the whole hour.
    """
    records = []
    for hour in hours:
        values = (hour.pv_kwh, hour.load_kwh, hour.charge_kwh, hour.discharge_kwh, hour.export_kwh, hour.import_kwh)
        if any(value is None for value in values):
            continue
        soc = hour.soc_max if hour.soc_max is not None else (hour.soc if hour.soc is not None else 50.0)
        start_utc = hour.ts_local.astimezone(dt.UTC)
        for sub in range(steps_per_hour):
            now = (start_utc + dt.timedelta(hours=sub / steps_per_hour)).astimezone(hour.ts_local.tzinfo)
            records.append(StepRecord(
                now, "actual", "actual", "actual", "Messung",
                *(value / steps_per_hour for value in values),
                soc, soc, hour.price_ct, None, None, True,
            ))
    return records


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _on_off(value: Any) -> bool | None:
    return {"on": True, "off": False}.get(value)


_COLUMNS = [f.name for f in fields(StepRecord)]


def write_records(records: list[StepRecord], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_COLUMNS)
        for record in records:
            row = []
            for name in _COLUMNS:
                value = getattr(record, name)
                if isinstance(value, dt.datetime):
                    value = value.isoformat()
                elif isinstance(value, float):
                    value = f"{value:.5f}"
                elif value is None:
                    value = ""
                row.append(value)
            writer.writerow(row)
