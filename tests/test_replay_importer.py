"""Importer of the replay harness: DST days, gaps, balance and forecast anchors."""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from tools.replay.importer import import_series
from tools.replay.sun import SunModel
from tools.replay.timeseries import PRICE_INTERPOLATED, PRICE_MISSING, read_csv, write_csv

TZ = ZoneInfo("Europe/Berlin")
MAPPING = {
    "pv_kwh": {"stat": "change", "entities": ["sensor.pv_a", "sensor.pv_b"]},
    "export_kwh": {"stat": "change", "entities": ["sensor.export"]},
    "import_kwh": {"stat": "change", "entities": ["sensor.import"]},
    "battery_charge_kwh": {"stat": "change", "entities": ["sensor.charge"]},
    "battery_discharge_kwh": {"stat": "change", "entities": ["sensor.discharge"]},
    "house_kwh_meter": {"stat": "change", "entities": ["sensor.house"]},
    "soc_mean": {"stat": "mean", "entities": ["sensor.soc"]},
    "soc_min": {"stat": "min", "entities": ["sensor.soc"]},
    "soc_max": {"stat": "max", "entities": ["sensor.soc"]},
    "price_eur_kwh": {"stat": "mean", "entities": ["sensor.price"], "fallback_ct": ["sensor.price_ct"]},
    "forecast_today_kwh": {"stat": "mean", "entities": ["sensor.fc_today"]},
    "forecast_tomorrow_kwh": {"stat": "mean", "entities": ["sensor.fc_tomorrow"]},
}


def local_hours(first_day: dt.date, days: int) -> list[dt.datetime]:
    start = dt.datetime.combine(first_day, dt.time(), tzinfo=TZ)
    end = dt.datetime.combine(first_day + dt.timedelta(days=days), dt.time(), tzinfo=TZ)
    hours, current = [], start
    while current < end:
        hours.append(current)
        current = (current.astimezone(dt.UTC) + dt.timedelta(hours=1)).astimezone(TZ)
    return hours


def pv_profile(ts: dt.datetime) -> float:
    return {10: 1.0, 11: 2.0, 12: 3.0, 13: 2.0, 14: 1.0}.get(ts.hour, 0.0) * (1 + ts.day % 3)


def build_raw(hours: list[dt.datetime], *, drop: dict[str, set[dt.datetime]] | None = None) -> dict:
    """Synthetic export; ``drop`` removes rows per entity to create gaps."""
    drop = drop or {}
    day_pv: dict[dt.date, float] = {}
    for ts in hours:
        day_pv[ts.date()] = day_pv.get(ts.date(), 0.0) + pv_profile(ts)
    days = sorted(day_pv)
    level_today = {day: day_pv[day] for day in days}
    level_tomorrow = {day: day_pv.get(day + dt.timedelta(days=1), 5.0) for day in days}
    rows: dict[str, list[dict]] = {entity: [] for entity in (
        "sensor.pv_a", "sensor.pv_b", "sensor.export", "sensor.import", "sensor.charge", "sensor.discharge",
        "sensor.house", "sensor.soc", "sensor.price", "sensor.fc_today", "sensor.fc_tomorrow")}
    previous_day = None
    for ts in hours:
        start = int(ts.timestamp() * 1000)
        end = start + 3_600_000
        pv = pv_profile(ts)
        export, imported = (max(0.0, pv - 1.5), 0.0) if pv > 1.5 else (0.0, 0.5)
        charge, discharge = (0.4, 0.0) if pv > 0.5 else (0.0, 0.2)
        load = pv + imported - export - charge + discharge
        values = {
            "sensor.pv_a": {"change": pv * 0.6}, "sensor.pv_b": {"change": pv * 0.4},
            "sensor.export": {"change": export}, "sensor.import": {"change": imported},
            "sensor.charge": {"change": charge}, "sensor.discharge": {"change": discharge},
            "sensor.house": {"change": load + 0.01}, "sensor.soc": {"mean": 50.0, "min": 45.0, "max": 55.0},
            "sensor.price": {"mean": 0.20 + ts.hour / 100},
        }
        day = ts.date()
        if previous_day is None:
            values["sensor.fc_today"] = {"change": 0.0}
            values["sensor.fc_tomorrow"] = {"change": 0.0}
        elif day != previous_day:
            values["sensor.fc_today"] = {"change": level_today[day] - level_today[previous_day]}
            values["sensor.fc_tomorrow"] = {"change": level_tomorrow[day] - level_tomorrow[previous_day]}
        else:
            values["sensor.fc_today"] = {"change": 0.0}
            values["sensor.fc_tomorrow"] = {"change": 0.0}
        previous_day = day
        for entity, fields in values.items():
            if ts in drop.get(entity, set()):
                continue
            rows[entity].append({"start": start, "end": end, "change": None, "mean": None, "min": None, "max": None, **fields})
    return {"data": {"entities": [{"entity_id": entity, "statistics": stats} for entity, stats in rows.items()]}}


def import_raw(tmp_path, raw: dict, **kwargs):
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return import_series([path], MAPPING, **kwargs)


def test_dst_days_keep_25_and_23_local_hours(tmp_path):
    hours = local_hours(dt.date(2025, 10, 25), 3) + local_hours(dt.date(2026, 3, 28), 3)
    series, summary = import_raw(tmp_path, build_raw(hours))
    per_day = {}
    for hour in series:
        per_day[hour.day] = per_day.get(hour.day, 0) + 1
    assert per_day[dt.date(2025, 10, 26)] == 25
    assert per_day[dt.date(2026, 3, 29)] == 23
    assert per_day[dt.date(2025, 10, 25)] == 24
    assert summary["hours"] == len(hours) and summary["incomplete_hours"] == 0
    # The missing months between the two blocks produce separate forecast anchors.
    assert len(summary["forecast"]["blocks"]) == 2


def test_round_trip_through_csv_preserves_values_and_zone(tmp_path):
    hours = local_hours(dt.date(2025, 10, 25), 2)
    series, _ = import_raw(tmp_path, build_raw(hours))
    write_csv(series, tmp_path / "series.csv")
    restored = read_csv(tmp_path / "series.csv")
    assert [h.ts_local for h in restored] == [h.ts_local for h in series]
    assert restored[12].pv_kwh == pytest.approx(series[12].pv_kwh)
    assert restored[12].load_kwh == pytest.approx(series[12].load_kwh)
    assert restored[0].gaps == series[0].gaps
    assert restored[5].ts_local.tzinfo.key == "Europe/Berlin"


def test_load_follows_the_energy_balance_and_meter_check_is_reported(tmp_path):
    hours = local_hours(dt.date(2025, 11, 1), 2)
    series, summary = import_raw(tmp_path, build_raw(hours))
    noon = next(h for h in series if h.ts_local.hour == 12)
    assert noon.load_kwh == pytest.approx(noon.pv_kwh + noon.import_kwh - noon.export_kwh - noon.charge_kwh + noon.discharge_kwh)
    balance = summary["load_balance_vs_meter"]
    assert balance["hours_compared"] == 48
    assert balance["mean_abs_hourly_diff_kwh"] == pytest.approx(0.01, abs=1e-6)


def test_gaps_are_marked_not_invented(tmp_path):
    hours = local_hours(dt.date(2025, 11, 1), 3)
    night = hours[2]  # pv_b missing at night: a missing summand is a gap, never zero
    noon = hours[12]  # pv_b missing at noon: the hour's PV is unknown
    no_charge = hours[30]
    short_gap = {hours[40], hours[41]}  # 2 h price gap -> interpolated
    long_gap = set(hours[50:58])  # 8 h price gap -> missing
    raw = build_raw(hours, drop={
        "sensor.pv_b": {night, noon}, "sensor.charge": {no_charge},
        "sensor.price": short_gap | long_gap,
    })
    series, summary = import_raw(tmp_path, raw)
    by_ts = {h.ts_local: h for h in series}
    assert by_ts[night].pv_kwh is None and "pv_kwh_partial" in by_ts[night].gaps
    assert by_ts[noon].pv_kwh is None and not by_ts[noon].complete
    # With a sun model the night-time gap of a sleeping inverter becomes a certain zero.
    series_sun, summary_sun = import_raw(tmp_path, raw, sun=SunModel())
    by_ts_sun = {h.ts_local: h for h in series_sun}
    assert by_ts_sun[night].pv_kwh == 0 and "pv_kwh_night_zero" in by_ts_sun[night].gaps
    assert by_ts_sun[noon].pv_kwh is None
    assert summary_sun["pv_night_zero_hours"] == 1 and summary_sun["incomplete_hours"] == 2
    assert by_ts[no_charge].charge_kwh is None and by_ts[no_charge].load_kwh is None
    assert by_ts[hours[40]].price_src == PRICE_INTERPOLATED
    assert by_ts[hours[40]].price_ct == pytest.approx((by_ts[hours[39]].price_ct * 2 + by_ts[hours[42]].price_ct) / 3)
    assert by_ts[hours[52]].price_src == PRICE_MISSING and by_ts[hours[52]].price_ct is None
    assert summary["incomplete_hours"] == 3
    assert summary["price_hours"] == {"measured": 72 - 10, "fallback": 0, "interpolated": 2, "missing": 8}


def test_forecast_levels_are_reconstructed_from_changes(tmp_path):
    hours = local_hours(dt.date(2025, 11, 1), 5)
    series, summary = import_raw(tmp_path, build_raw(hours))
    day_pv: dict[dt.date, float] = {}
    for hour in series:
        day_pv[hour.day] = day_pv.get(hour.day, 0.0) + hour.pv_kwh
    for hour in series:
        assert hour.forecast_today_kwh == pytest.approx(day_pv[hour.day])
        following = hour.day + dt.timedelta(days=1)
        assert hour.forecast_tomorrow_kwh == pytest.approx(day_pv.get(following, 5.0))
        assert "forecast_reconstructed" in hour.gaps
    block = summary["forecast"]["blocks"][0]
    assert block["anchor_days"] == 5
    assert block["median_abs_error_today_vs_actual_kwh"] == pytest.approx(0.0, abs=1e-6)
