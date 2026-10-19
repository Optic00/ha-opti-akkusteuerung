"""Period metrics, end-of-day PV potential and raw export patterns of the harness."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tools.replay import report
from tools.replay.battery import BatteryParams
from tools.replay.metrics import end_of_day_potential, export_free_battery_raw, summarize
from tools.replay.simulate import StepRecord

TZ = ZoneInfo("Europe/Berlin")
PARAMS = BatteryParams(capacity_kwh=10, minsoc=5, maxsoc=95, max_charge_w=3000, max_discharge_w=4000)


def record(ts, mode="Akku Dynamisch", decision="below_target", *, pv=0.0, load=0.1, charge=0.0,
           discharge=0.0, export=0.0, imported=0.0, soc=50.0, soc_after=None, price=30.0, ok=True,
           warmup=False):
    return StepRecord(ts, "p", mode, decision, "", pv, load, charge, discharge, export, imported,
                      soc, soc if soc_after is None else soc_after, price, None, None, ok, warmup)


def test_raw_export_needs_room_and_unused_charge_power():
    ts = dt.datetime(2025, 10, 5, 12, tzinfo=TZ)
    assert export_free_battery_raw(record(ts, export=0.5, charge=0.0, soc=50), 95, 0.75) == pytest.approx(0.5)
    assert export_free_battery_raw(record(ts, export=0.5, charge=0.75, soc=50), 95, 0.75) == 0.0
    assert export_free_battery_raw(record(ts, export=2.0, charge=0.25, soc=50), 95, 0.75) == pytest.approx(0.5)
    assert export_free_battery_raw(record(ts, export=0.5, soc=94.5), 95, 0.75) == 0.0


def steps(start, items):
    """items: (pv, export, import, charge, soc_after, price) per quarter hour."""
    result = []
    for index, (pv, export, imported, charge, soc_after, price) in enumerate(items):
        ts = start + dt.timedelta(minutes=15 * index)
        soc_before = items[index - 1][4] if index else 50.0
        result.append(record(ts, pv=pv, export=export, imported=imported, charge=charge,
                             soc=soc_before, soc_after=soc_after, price=price))
    return result


def test_potential_counts_only_what_survives_until_the_last_pv_step():
    start = dt.datetime(2025, 10, 5, 12, tzinfo=TZ)
    # Two exporting steps with a free battery, then a night with import.
    records = steps(start, [
        (1.0, 0.5, 0.0, 0.0, 50.0, 10.0),   # 0.5 kWh export, free power 0.75 -> 0.475 stored
        (1.0, 0.5, 0.0, 0.0, 50.0, 10.0),   # another 0.475 stored
        (0.0, 0.0, 0.3, 0.0, 50.0, 40.0),   # night: 0.3 kWh import replaceable at 40 ct
        (0.0, 0.0, 2.0, 0.0, 50.0, None),   # unpriced: limited by discharge power 1.0 kWh/step
    ])
    result = end_of_day_potential(records, PARAMS, 0.25)
    day = result[dt.date(2025, 10, 5)]
    assert day["potential_kwh"] == pytest.approx(1.0)
    stored = 0.95  # 2 x 0.5 x 0.95
    assert day["replaced_import_kwh"] == pytest.approx(0.3 + min(1.0, (stored - 0.3 / 0.95) * 0.95))
    assert day["replaced_import_eur"] == pytest.approx(0.3 * 0.40)
    assert day["replaced_unpriced_kwh"] == pytest.approx(min(1.0, (stored - 0.3 / 0.95) * 0.95))


def test_potential_is_displaced_by_later_regular_charging():
    start = dt.datetime(2025, 10, 5, 12, tzinfo=TZ)
    records = steps(start, [
        (1.0, 0.5, 0.0, 0.0, 50.0, 10.0),   # chance of 0.475 stored
        (2.0, 1.0, 0.0, 0.75, 92.0, 10.0),  # real charging fills the battery up to 92 %
        (1.0, 0.5, 0.0, 0.0, 95.0, 10.0),   # battery full: every earlier chance vanishes
        (0.0, 0.0, 0.5, 0.0, 95.0, 40.0),
    ])
    day = end_of_day_potential(records, PARAMS, 0.25)[dt.date(2025, 10, 5)]
    assert day["potential_kwh"] == 0.0 and day["replaced_import_kwh"] == 0.0
    partial = steps(start, [
        (1.0, 0.5, 0.0, 0.0, 50.0, 10.0),
        (2.0, 1.0, 0.0, 0.75, 93.0, 10.0),  # 0.2 kWh of room left beside the real trajectory
        (0.0, 0.0, 0.5, 0.0, 93.0, 40.0),
    ])
    day = end_of_day_potential(partial, PARAMS, 0.25)[dt.date(2025, 10, 5)]
    assert day["potential_kwh"] == pytest.approx(0.2 / 0.95)


def test_periods_costs_cycles_warmup_and_patterns():
    october = dt.datetime(2025, 10, 5, 12, tzinfo=TZ)
    december = dt.datetime(2025, 12, 5, 18, tzinfo=TZ)
    records = [
        record(october - dt.timedelta(hours=1), export=5.0, soc=10, warmup=True),
        record(october, "Akku nur Entladen", "above_target", pv=1.0, load=0.2, export=0.8, soc=60),
        record(october + dt.timedelta(minutes=15), "Akku Dynamisch", "below_target", pv=1.0, load=0.2,
               charge=0.75, export=0.05, soc=60, soc_after=90),
        record(december, "Akku Netzladen", "negative_price", pv=0.0, load=0.2, charge=0.5, imported=0.7,
               soc=30, price=-2.0),
        record(december + dt.timedelta(minutes=15), "Akku Dynamisch", "default", pv=0.0, load=0.2,
               discharge=0.2, soc=86, price=None, ok=False),
    ]
    result = summarize(records, params=PARAMS, feed_in_ct=10, step_hours=0.25)
    assert set(result) == {"gesamt", "2025-10", "2025-12", "uebergang", "winter"}
    total = result["gesamt"]
    assert total["warmup_steps"] == 1 and total["steps"] == 4
    assert total["export_kwh"] == pytest.approx(0.85)
    assert total["export_free_battery_raw_kwh"] == pytest.approx(0.75)
    assert total["pv_potential_end_of_day_kwh"] == pytest.approx(0.5 / 0.95, abs=0.01)  # 0.5 kWh room at 90 %
    assert total["import_kwh"] == pytest.approx(0.7)
    assert total["grid_charge_kwh"] == pytest.approx(0.5)
    assert total["cycles"] == pytest.approx((1.25 + 0.2) / 2 / 10, abs=0.005)
    assert total["hours_soc_above_85"] == pytest.approx(0.25)
    assert total["mode_changes"] == 3
    assert total["engine_error_steps"] == 1
    assert total["priced_import_kwh"] == pytest.approx(0.7)
    assert total["import_cost_eur"] == pytest.approx(0.7 * -0.02, abs=0.005)
    assert total["feed_in_revenue_eur"] == pytest.approx(0.85 * 0.10, abs=0.01)
    assert total["self_consumption_pct"] == pytest.approx((2.0 - 0.85) / 2.0 * 100, abs=0.1)
    uebergang = result["uebergang"]
    assert uebergang["steps"] == 2 and result["winter"]["steps"] == 2
    assert uebergang["raw_by_decision"] == {"above_target": 0.75}
    assert uebergang["raw_by_mode"] == {"Akku nur Entladen": 0.75}
    assert uebergang["raw_by_hour"] == {"12": 0.75}
    assert uebergang["raw_by_soc_band"] == {"50-70": 0.75}
    assert uebergang["top_days_raw"][0]["day"] == "2025-10-05"
    assert uebergang["top_days_potential"][0]["day"] == "2025-10-05"
    assert result["winter"]["pv_potential_end_of_day_kwh"] == 0


def test_report_lists_policies_per_period_and_patterns():
    ts = dt.datetime(2026, 3, 10, 11, tzinfo=TZ)
    metrics = {
        name: summarize([record(ts, pv=1.0, export=0.5, soc=40), record(ts + dt.timedelta(minutes=15), imported=0.2, soc=40)],
                        params=PARAMS, feed_in_ct=8)
        for name in ("actual", "strategy@HEAD")
    }
    text = report.render(metrics, {"hours": 1, "assumptions": {"steps_per_hour": 4}})
    assert "### uebergang" in text and "### 2026-03" in text
    assert "| strategy@HEAD |" in text and "| actual |" in text
    assert "- assumptions:" in text and "  - steps_per_hour: 4" in text
    assert "roh nach decision_id: below_target 0.5" in text
    assert "| 2026-03-10 | 0.5 | 0.2 |" in text
