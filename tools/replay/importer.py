"""Turn exported HA long-term statistics into the normalised hourly series.

Input: one or more JSON exports shaped ``{"data": {"entities": [{"entity_id",
"statistics": [{"start", "end", "change" | "mean" | "min" | "max"}]}]}}`` plus a
mapping from plant quantity to entities. Gaps stay gaps; only short price gaps
are bridged, and every bridged or reconstructed value is flagged on the hour.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .sun import SunModel
from .timeseries import (
    PRICE_FALLBACK,
    PRICE_INTERPOLATED,
    PRICE_MEASURED,
    PRICE_MISSING,
    Hour,
)

BLOCK_GAP = dt.timedelta(hours=6)
CHANGE_KEYS = {
    "pv_kwh": "pv_kwh",
    "export_kwh": "export_kwh",
    "import_kwh": "import_kwh",
    "battery_charge_kwh": "charge_kwh",
    "battery_discharge_kwh": "discharge_kwh",
    "house_kwh_meter": "house_meter_kwh",
}


def load_raw(paths: list[Path]) -> dict[str, dict[int, dict[str, Any]]]:
    """Merge the statistics of all files by entity and hour start (ms)."""
    merged: dict[str, dict[int, dict[str, Any]]] = {}
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for entity in data["data"]["entities"]:
            rows = merged.setdefault(entity["entity_id"], {})
            for row in entity["statistics"]:
                rows[int(row["start"])] = row
    return merged


def _value(raw: dict, entity: str, start: int, key: str) -> float | None:
    row = raw.get(entity, {}).get(start)
    if row is None or row.get(key) is None:
        return None
    return float(row[key])


def _sum_change(raw: dict, entities: list[str], start: int) -> tuple[float | None, list[str]]:
    """Sum meter changes; any missing summand makes the hour a gap, never a zero."""
    total, missing = 0.0, []
    for entity in entities:
        value = _value(raw, entity, start, "change")
        if value is None:
            missing.append(entity)
        else:
            total += value
    if missing:
        return None, missing
    return total, missing


def _night(hour: Hour, sun: SunModel) -> bool:
    """True when the sun is below the horizon for the whole hour."""
    end = hour.ts_local + dt.timedelta(hours=1)
    return sun.state(hour.ts_local)[0] == "below_horizon" and sun.state(end)[0] == "below_horizon"


def _blocks(hours: list[Hour]) -> list[list[int]]:
    """Index runs separated by more than BLOCK_GAP (e.g. missing months)."""
    blocks: list[list[int]] = []
    for index, hour in enumerate(hours):
        if blocks and hour.ts_local - hours[blocks[-1][-1]].ts_local <= BLOCK_GAP:
            blocks[-1].append(index)
        else:
            blocks.append([index])
    return blocks


def _interpolate_prices(hours: list[Hour], max_gap: int) -> int:
    filled = 0
    for block in _blocks(hours):
        known = [i for i in block if hours[i].price_ct is not None]
        for left, right in zip(known, known[1:], strict=False):
            width = right - left
            if width <= 1 or width - 1 > max_gap:
                continue
            a, b = hours[left].price_ct, hours[right].price_ct
            for offset in range(1, width):
                hour = hours[left + offset]
                hour.price_ct = a + (b - a) * offset / width
                hour.price_src = PRICE_INTERPOLATED
                hour.gaps.append("price_interpolated")
                filled += 1
    return filled


def _reconstruct_forecasts(hours: list[Hour], raw: dict, mapping: dict) -> dict[str, Any]:
    """Rebuild forecast levels from ``change`` rows (state_class ``total``).

    The export carries only hourly changes of the Solcast day totals, so the
    level is ``anchor + cumulative change``. The anchor of the today forecast is
    the median residual against the measured daily PV yield; the tomorrow
    anchor is tied to the next day's today forecast after the midnight roll.
    Both anchors are estimates and are reported in the import summary.
    """
    today_entities = mapping["forecast_today_kwh"]["entities"]
    tomorrow_entities = mapping["forecast_tomorrow_kwh"]["entities"]
    summary: dict[str, Any] = {"blocks": []}
    for block in _blocks(hours):
        cum_today: list[float] = []
        cum_tomorrow: list[float] = []
        run_today = run_tomorrow = 0.0
        for index in block:
            hour = hours[index]
            start = int(hour.ts_local.timestamp() * 1000)
            change_today, _ = _sum_change(raw, today_entities, start)
            change_tomorrow, _ = _sum_change(raw, tomorrow_entities, start)
            if change_today is None:
                hour.gaps.append("forecast_today_change_missing")
            if change_tomorrow is None:
                hour.gaps.append("forecast_tomorrow_change_missing")
            run_today += change_today or 0.0
            run_tomorrow += change_tomorrow or 0.0
            cum_today.append(run_today)
            cum_tomorrow.append(run_tomorrow)

        first_of_day: dict[dt.date, int] = {}
        last_of_day: dict[dt.date, int] = {}
        pv_by_day: dict[dt.date, float | None] = {}
        for position, index in enumerate(block):
            day = hours[index].day
            first_of_day.setdefault(day, position)
            last_of_day[day] = position
            pv = hours[index].pv_kwh
            if day not in pv_by_day:
                pv_by_day[day] = pv
            elif pv is None or pv_by_day[day] is None:
                pv_by_day[day] = None
            else:
                pv_by_day[day] = pv_by_day[day] + pv
        full_days = [
            day for day, position in first_of_day.items()
            if last_of_day[day] - position >= 22 and pv_by_day.get(day) is not None
        ]
        residuals = [pv_by_day[day] - cum_today[last_of_day[day]] for day in full_days]
        anchor_today = statistics.median(residuals) if residuals else 0.0
        anchor_today = max(anchor_today, -min(cum_today))
        tomorrow_residuals = []
        for day in full_days:
            following = day + dt.timedelta(days=1)
            if following in first_of_day:
                tomorrow_residuals.append(
                    anchor_today + cum_today[first_of_day[following]] - cum_tomorrow[last_of_day[day]]
                )
        anchor_tomorrow = statistics.median(tomorrow_residuals) if tomorrow_residuals else 0.0
        anchor_tomorrow = max(anchor_tomorrow, -min(cum_tomorrow))
        errors = []
        for position, index in enumerate(block):
            hour = hours[index]
            hour.forecast_today_kwh = max(0.0, anchor_today + cum_today[position])
            hour.forecast_tomorrow_kwh = max(0.0, anchor_tomorrow + cum_tomorrow[position])
            hour.gaps.append("forecast_reconstructed")
        for day in full_days:
            errors.append(abs(hours[block[last_of_day[day]]].forecast_today_kwh - pv_by_day[day]))
        summary["blocks"].append({
            "from": hours[block[0]].ts_local.isoformat(),
            "to": hours[block[-1]].ts_local.isoformat(),
            "anchor_today_kwh": round(anchor_today, 3),
            "anchor_tomorrow_kwh": round(anchor_tomorrow, 3),
            "anchor_days": len(full_days),
            "median_abs_error_today_vs_actual_kwh": round(statistics.median(errors), 3) if errors else None,
        })
    return summary


def import_series(
    raw_paths: list[Path], mapping: dict, *, tz: str = "Europe/Berlin", price_gap_max_hours: int = 3,
    sun: SunModel | None = None,
) -> tuple[list[Hour], dict[str, Any]]:
    """Build the hourly series and a summary of gaps, anchors and balance checks.

    ``sun`` enables the night-time zero for a missing PV summand; without it every
    missing summand stays a gap.
    """
    zone = ZoneInfo(tz)
    raw = load_raw(raw_paths)
    starts: set[int] = set()
    for spec in mapping.values():
        for entity in spec.get("entities", []) + spec.get("fallback_ct", []):
            starts.update(raw.get(entity, {}).keys())
    hours: list[Hour] = []
    for start in sorted(starts):
        hour = Hour(ts_local=dt.datetime.fromtimestamp(start / 1000, tz=zone))
        for key, attribute in CHANGE_KEYS.items():
            value, missing = _sum_change(raw, mapping[key]["entities"], start)
            if (value is None and key == "pv_kwh" and len(missing) < len(mapping[key]["entities"])
                    and sun is not None and _night(hour, sun)
                    and all(_value(raw, entity, start, "change") == 0 for entity in mapping[key]["entities"]
                            if entity not in missing)):
                # A sleeping inverter at night: the other PV meters read zero and the
                # sun is down for the whole hour, so zero is physically certain.
                value = 0.0
                hour.gaps.append("pv_kwh_night_zero")
            setattr(hour, attribute, value)
            if value is None:
                hour.gaps.append(attribute if len(missing) == len(mapping[key]["entities"]) else f"{attribute}_partial")
        for key, attribute in (("soc_mean", "soc"), ("soc_min", "soc_min"), ("soc_max", "soc_max")):
            setattr(hour, attribute, _value(raw, mapping[key]["entities"][0], start, mapping[key]["stat"]))
        if hour.soc is None:
            hour.gaps.append("soc")
        price = _value(raw, mapping["price_eur_kwh"]["entities"][0], start, "mean")
        if price is not None:
            hour.price_ct, hour.price_src = price * 100, PRICE_MEASURED
        else:
            for entity in mapping["price_eur_kwh"].get("fallback_ct", []):
                fallback = _value(raw, entity, start, "mean")
                if fallback is not None:
                    hour.price_ct, hour.price_src = fallback, PRICE_FALLBACK
                    hour.gaps.append("price_fallback")
                    break
        if hour.pv_kwh is not None and None not in (
            hour.import_kwh, hour.export_kwh, hour.charge_kwh, hour.discharge_kwh
        ):
            load = hour.pv_kwh + hour.import_kwh - hour.export_kwh - hour.charge_kwh + hour.discharge_kwh
            if load < 0:
                hour.gaps.append("load_negative_clamped")
            hour.load_kwh = max(0.0, load)
        else:
            hour.gaps.append("load")
        hours.append(hour)

    interpolated = _interpolate_prices(hours, price_gap_max_hours)
    for hour in hours:
        if hour.price_ct is None:
            hour.price_src = PRICE_MISSING
            hour.gaps.append("price")
    forecast_summary = _reconstruct_forecasts(hours, raw, mapping)

    paired = [(h.load_kwh, h.house_meter_kwh) for h in hours if h.load_kwh is not None and h.house_meter_kwh is not None]
    balance = {
        "hours_compared": len(paired),
        "balance_total_kwh": round(sum(a for a, _ in paired), 1),
        "meter_total_kwh": round(sum(b for _, b in paired), 1),
        "mean_abs_hourly_diff_kwh": round(statistics.fmean(abs(a - b) for a, b in paired), 3) if paired else None,
    }
    summary = {
        "hours": len(hours),
        "from": hours[0].ts_local.isoformat() if hours else None,
        "to": hours[-1].ts_local.isoformat() if hours else None,
        "incomplete_hours": sum(1 for h in hours if not h.complete),
        "price_hours": {
            src: sum(1 for h in hours if h.price_src == src)
            for src in (PRICE_MEASURED, PRICE_FALLBACK, PRICE_INTERPOLATED, PRICE_MISSING)
        },
        "price_interpolated_hours": interpolated,
        "pv_night_zero_hours": sum(1 for h in hours if "pv_kwh_night_zero" in h.gaps),
        "load_balance_vs_meter": balance,
        "forecast": forecast_summary,
    }
    return hours, summary
