"""Strategy policy of the replay harness drives the real engine step by step."""
from __future__ import annotations

import copy
import datetime as dt
from collections import Counter
from zoneinfo import ZoneInfo

import pytest

from tools.replay import simulate
from tools.replay.battery import BatteryParams
from tools.replay.policies import SelfConsumptionPolicy, StrategyPolicy
from tools.replay.resources import load_strategy
from tools.replay.timeseries import Hour

TZ = ZoneInfo("Europe/Berlin")
PLANT = {"capacity_kwh": 10, "minsoc": 5, "maxsoc": 95, "feed_in_ct": 8, "netzlade_spread_ct": 10,
         "halte_spread_ct": 5, "max_charge_w": 3000, "max_discharge_w": 5000,
         "pv_surplus_charging": True, "grid_precharge": True}
PEAK_HOURS = range(16, 20)


def peak_day(date: dt.date = dt.date(2026, 10, 19)) -> list[Hour]:
    """Poor forecast (score <= 2), late PV surplus and a very expensive evening."""
    hours = []
    for hour in range(24):
        pv = {13: 1.0, 14: 1.6, 15: 2.0, 16: 1.8, 17: 1.2}.get(hour, 0.3 if 9 <= hour <= 18 else 0.0)
        hours.append(Hour(
            dt.datetime.combine(date, dt.time(hour), tzinfo=TZ), pv_kwh=pv, load_kwh=0.5, soc=50,
            price_ct=60.0 if hour in PEAK_HOURS else 20.0, price_src="measured",
            forecast_today_kwh=2.0, forecast_tomorrow_kwh=2.0,
        ))
    return hours


def without_peak_pv_fix(resources: dict) -> dict:
    """Main before 188b644: L1/L2 always selected 'Akku nur Entladen'."""
    patched = copy.deepcopy(resources)
    for option in patched["strategy"]["actions"][0]["choose"]:
        for action in option.get("sequence", []):
            if action.get("decision_id") in ("peak_l1", "peak_l2"):
                action["data"]["option"] = "Akku nur Entladen"
    return patched


@pytest.fixture(scope="module")
def head_resources():
    return load_strategy(None)


def run_day(resources, hours=None):
    policy = StrategyPolicy(PLANT, resources=resources, name="test")
    records = simulate.run(hours or peak_day(), policy, BatteryParams.from_plant(PLANT), start_soc=50)
    return policy, records


def test_peak_pv_fix_charges_surplus_instead_of_exporting(head_resources):
    head_policy, head_records = run_day(head_resources)
    main_policy, main_records = run_day(without_peak_pv_fix(head_resources))
    assert head_policy.error_steps == 0 and main_policy.error_steps == 0
    assert len(head_records) == 96
    head_peak = [r for r in head_records if r.ts_local.hour in PEAK_HOURS]
    main_peak = [r for r in main_records if r.ts_local.hour in PEAK_HOURS]
    assert {r.decision_id for r in head_peak} == {"peak_l1"} == {r.decision_id for r in main_peak}
    assert {r.mode for r in main_peak} == {"Akku nur Entladen"}
    assert "Akku Dynamisch" in {r.mode for r in head_peak}
    head_export = sum(r.export_kwh for r in head_records)
    main_export = sum(r.export_kwh for r in main_records)
    assert main_export > 1.0
    assert head_export < main_export
    assert head_export == pytest.approx(0.0)


def test_engine_state_persists_across_steps_and_summary_reports_revision(head_resources):
    policy, records = run_day(head_resources)
    assert policy.steps == 96
    summary = policy.summary()
    assert summary["engine_error_steps"] == 0 and summary["engine_steps"] == 96
    assert summary["source_revision"] == head_resources["source_revision"]
    assert summary["sun_source"] in ("astral", "table")
    # The reserve-holding branch latches 'nur Laden' through the afternoon before the peak.
    afternoon = Counter(r.mode for r in records if 12 <= r.ts_local.hour <= 15)
    assert afternoon == {"Akku nur Laden": 16}
    assert all(r.engine_ok for r in records)
    # Surplus during the peak charged the battery instead of being exported.
    assert records[71].soc_after > records[64].soc_before


def test_missing_price_day_keeps_engine_running_without_price_level(head_resources):
    hours = peak_day()
    for hour in hours:
        hour.price_ct, hour.price_src = None, "missing"
    policy, records = run_day(head_resources, hours)
    assert policy.error_steps == 0
    assert "peak_l1" not in {r.decision_id for r in records}


def test_self_consumption_and_actual_baselines():
    hours = peak_day()
    params = BatteryParams.from_plant(PLANT)
    records = simulate.run(hours, SelfConsumptionPolicy(), params, start_soc=50)
    assert {r.mode for r in records} == {"Akku Dynamisch"}
    assert sum(r.export_kwh for r in records) == pytest.approx(0.0)
    for hour in hours:
        hour.charge_kwh, hour.discharge_kwh, hour.export_kwh, hour.import_kwh = 0.2, 0.1, 0.3, 0.4
        hour.soc_max = 60
    actual = simulate.run_actual(hours)
    assert len(actual) == 96
    assert sum(r.export_kwh for r in actual) == pytest.approx(24 * 0.3)
    assert actual[0].soc_before == 60 and actual[0].mode == "actual"


def test_incomplete_hours_are_skipped_and_soc_is_carried():
    hours = peak_day()
    hours[12].pv_kwh = None
    records = simulate.run(hours, SelfConsumptionPolicy(), BatteryParams.from_plant(PLANT), start_soc=50)
    assert len(records) == 92
    assert records[48].ts_local.hour == 13
    assert records[48].soc_before == pytest.approx(records[47].soc_after)
