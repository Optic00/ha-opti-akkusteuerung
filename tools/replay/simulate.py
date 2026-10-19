"""Run a policy through the hourly series in fixed sub-hour steps.

Assumption: each recorded hour is spread evenly over its steps (default four
quarter hours). This smooths real intra-hour peaks, so surplus and deficit
magnitudes are hourly means. The engine sees the previous step's powers as its
"live" measurements and the simulated SoC as the battery state.
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


def _remaining_forecast(hour: Hour, table: DayTable, index: int, fraction: float) -> float | None:
    """Today's forecast scaled by the share of the day's actual PV still ahead."""
    if hour.forecast_today_kwh is None:
        return None
    if table.pv_total <= 0:
        return hour.forecast_today_kwh if hour.ts_local.hour < 16 else 0.0
    produced = table.pv_before[index] + (hour.pv_kwh or 0.0) * fraction
    return hour.forecast_today_kwh * max(0.0, 1 - produced / table.pv_total)


def run(
    hours: list[Hour],
    policy: Any,
    params: battery.BatteryParams,
    *,
    steps_per_hour: int = 4,
    start_soc: float | None = None,
) -> list[StepRecord]:
    tables = day_tables(hours)
    index_in_day: dict[int, int] = {}
    counter: dict[dt.date, int] = {}
    for position, hour in enumerate(hours):
        index_in_day[position] = counter.get(hour.day, 0)
        counter[hour.day] = index_in_day[position] + 1
    soc = start_soc if start_soc is not None else next((h.soc for h in hours if h.soc is not None), 50.0)
    soc = min(params.maxsoc, max(params.minsoc, soc))
    step_h = 1 / steps_per_hour
    previous = PowerSnapshot()
    records: list[StepRecord] = []
    for position, hour in enumerate(hours):
        if not hour.complete:
            previous = PowerSnapshot()
            continue
        table = tables[hour.day]
        tomorrow = tables.get(hour.day + dt.timedelta(days=1))
        pv_kwh, load_kwh = hour.pv_kwh / steps_per_hour, hour.load_kwh / steps_per_hour
        for sub in range(steps_per_hour):
            now = hour.ts_local + dt.timedelta(minutes=60 * sub / steps_per_hour)
            context = StepContext(
                now=now, soc=soc, previous=previous, price_ct=hour.price_ct,
                price_today=table.prices,
                price_tomorrow=(tomorrow.prices or []) if tomorrow and now.hour >= TOMORROW_KNOWN_FROM_HOUR else [],
                forecast_today_kwh=hour.forecast_today_kwh,
                forecast_tomorrow_kwh=hour.forecast_tomorrow_kwh,
                forecast_remaining_kwh=_remaining_forecast(hour, table, index_in_day[position], sub / steps_per_hour),
            )
            decision: Decision = policy.decide(context)
            flows = battery.step(decision.mode, soc, pv_kwh, load_kwh, step_h, params, decision.charge_limit_w)
            records.append(StepRecord(
                now, policy.name, decision.mode, decision.decision_id, decision.reason,
                pv_kwh, load_kwh, flows.charge_kwh, flows.discharge_kwh, flows.export_kwh, flows.import_kwh,
                soc, flows.soc_after, hour.price_ct, decision.charge_limit_w, decision.target_soc, decision.ok,
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

    The hourly maximum SoC stands in for the per-step SoC, so "avoidable export"
    is only counted when the battery stayed below the cap for the whole hour.
    """
    records = []
    for hour in hours:
        values = (hour.pv_kwh, hour.load_kwh, hour.charge_kwh, hour.discharge_kwh, hour.export_kwh, hour.import_kwh)
        if any(value is None for value in values):
            continue
        soc = hour.soc_max if hour.soc_max is not None else (hour.soc if hour.soc is not None else 50.0)
        for sub in range(steps_per_hour):
            now = hour.ts_local + dt.timedelta(minutes=60 * sub / steps_per_hour)
            records.append(StepRecord(
                now, "actual", "actual", "actual", "Messung",
                *(value / steps_per_hour for value in values),
                soc, soc, hour.price_ct, None, None, True,
            ))
    return records


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
