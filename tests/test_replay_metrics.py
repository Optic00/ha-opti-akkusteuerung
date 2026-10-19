"""Period metrics and avoidable-export patterns of the replay harness."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tools.replay import report
from tools.replay.metrics import avoidable_export, summarize
from tools.replay.simulate import StepRecord

TZ = ZoneInfo("Europe/Berlin")


def record(ts, mode="Akku Dynamisch", decision="below_target", *, pv=0.0, load=0.1, charge=0.0,
           discharge=0.0, export=0.0, imported=0.0, soc=50.0, price=30.0, ok=True):
    return StepRecord(ts, "p", mode, decision, "", pv, load, charge, discharge, export, imported,
                      soc, soc, price, None, None, ok)


def test_avoidable_export_needs_room_and_unused_charge_power():
    ts = dt.datetime(2025, 10, 5, 12, tzinfo=TZ)
    assert avoidable_export(record(ts, export=0.5, charge=0.0, soc=50), 95, 0.75) == pytest.approx(0.5)
    assert avoidable_export(record(ts, export=0.5, charge=0.75, soc=50), 95, 0.75) == 0.0
    assert avoidable_export(record(ts, export=2.0, charge=0.25, soc=50), 95, 0.75) == pytest.approx(0.5)
    assert avoidable_export(record(ts, export=0.5, soc=94.5), 95, 0.75) == 0.0


def test_periods_costs_cycles_and_patterns():
    october = dt.datetime(2025, 10, 5, 12, tzinfo=TZ)
    december = dt.datetime(2025, 12, 5, 18, tzinfo=TZ)
    records = [
        record(october, "Akku nur Entladen", "above_target", pv=1.0, load=0.2, export=0.8, soc=60),
        record(october + dt.timedelta(minutes=15), "Akku Dynamisch", "below_target", pv=1.0, load=0.2,
               charge=0.75, export=0.05, soc=60),
        record(december, "Akku Netzladen", "negative_price", pv=0.0, load=0.2, charge=0.5, imported=0.7,
               soc=30, price=-2.0),
        record(december + dt.timedelta(minutes=15), "Akku Dynamisch", "default", pv=0.0, load=0.2,
               discharge=0.2, soc=35, price=None, ok=False),
    ]
    result = summarize(records, capacity_kwh=10, maxsoc=95, feed_in_ct=10, max_charge_w=3000, step_hours=0.25)
    assert set(result) == {"gesamt", "2025-10", "2025-12", "uebergang", "winter"}
    total = result["gesamt"]
    assert total["export_kwh"] == pytest.approx(0.85)
    assert total["avoidable_export_kwh"] == pytest.approx(0.75)
    assert total["import_kwh"] == pytest.approx(0.7)
    assert total["grid_charge_kwh"] == pytest.approx(0.5)
    assert total["cycles"] == pytest.approx((1.25 + 0.2) / 2 / 10, abs=0.005)
    assert total["mode_changes"] == 3
    assert total["engine_error_steps"] == 1
    assert total["priced_import_kwh"] == pytest.approx(0.7)
    assert total["import_cost_eur"] == pytest.approx(0.7 * -0.02, abs=0.005)
    assert total["feed_in_revenue_eur"] == pytest.approx(0.85 * 0.10, abs=0.01)
    assert total["self_consumption_pct"] == pytest.approx((2.0 - 0.85) / 2.0 * 100, abs=0.1)
    uebergang = result["uebergang"]
    assert uebergang["steps"] == 2 and result["winter"]["steps"] == 2
    assert uebergang["avoidable_by_decision"] == {"above_target": 0.75}
    assert uebergang["avoidable_by_mode"] == {"Akku nur Entladen": 0.75}
    assert uebergang["avoidable_by_hour"] == {"12": 0.75}
    assert uebergang["avoidable_by_soc_band"] == {"50-70": 0.75}
    assert uebergang["top_days"][0]["day"] == "2025-10-05"
    assert result["winter"]["avoidable_export_kwh"] == 0


def test_report_lists_policies_per_period_and_patterns():
    ts = dt.datetime(2026, 3, 10, 11, tzinfo=TZ)
    metrics = {
        name: summarize([record(ts, export=0.5, soc=40)], capacity_kwh=10, maxsoc=95, feed_in_ct=8,
                        max_charge_w=3000)
        for name in ("actual", "strategy@HEAD")
    }
    text = report.render(metrics, {"hours": 1})
    assert "### uebergang" in text and "### 2026-03" in text
    assert "| strategy@HEAD |" in text and "| actual |" in text
    assert "nach decision_id: below_target 0.5" in text
    assert "| 2026-03-10 | 0.5 |" in text
