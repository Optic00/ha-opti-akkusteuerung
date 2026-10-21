"""Planner policies of the replay harness: oracle bound and receding horizon."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tools.replay import metrics, simulate
from tools.replay.__main__ import build_policy
from tools.replay.battery import BatteryParams
from tools.replay.planner_policies import OraclePolicy, RecedingHorizonPolicy
from tools.replay.policies import SelfConsumptionPolicy
from tools.replay.report import render
from tools.replay.sun import SunModel
from tools.replay.timeseries import Hour

TZ = ZoneInfo("Europe/Berlin")
PLANT = {"capacity_kwh": 10, "minsoc": 5, "maxsoc": 95, "feed_in_ct": 8, "max_charge_w": 3000,
         "max_discharge_w": 5000, "grid_precharge": True}
PARAMS = BatteryParams.from_plant(PLANT)


def winter_days(days=3, start=dt.date(2026, 1, 10), missing_price_hours=()):
    """Cheap nights, expensive evenings, a little PV at noon; prices known as in the harness."""
    hours = []
    for day in range(days):
        for hour in range(24):
            ts = dt.datetime.combine(start + dt.timedelta(days=day), dt.time(hour), tzinfo=TZ)
            pv = {11: 0.8, 12: 1.5, 13: 1.5, 14: 0.8}.get(hour, 0.0)
            price = 15.0 if 1 <= hour <= 4 else 45.0 if 17 <= hour <= 20 else 28.0
            missing = (day, hour) in missing_price_hours
            hours.append(Hour(ts, pv_kwh=pv, load_kwh=0.6, soc=20,
                              price_ct=None if missing else price, price_src="missing" if missing else "measured",
                              forecast_today_kwh=4.6, forecast_tomorrow_kwh=4.6))
    return hours


def net_cost(records):
    return metrics.summarize(records, params=PARAMS, feed_in_ct=8)["gesamt"]["net_cost_eur"]


def run(policy, hours):
    return simulate.run(hours, policy, PARAMS, start_soc=20, forecast_mode="naive")


def test_oracle_beats_self_consumption_and_buys_in_the_cheap_night():
    hours = winter_days()
    oracle = OraclePolicy(hours, PLANT, PARAMS)
    records = run(oracle, hours)
    baseline = run(SelfConsumptionPolicy(), hours)
    assert len(records) == len(baseline) == 24 * 3 * 4
    assert net_cost(records) < net_cost(baseline)
    grid = [r for r in records if r.charge_kwh > max(0.0, r.pv_kwh - r.load_kwh) + 1e-9]
    assert grid and {r.ts_local.hour for r in grid} <= {1, 2, 3, 4}
    assert {r.mode for r in grid} == {"Akku Netzladen"}
    # hold: the block ends where it started, up to the grid rounding of the plan.
    assert records[-1].soc_after >= 20 - 0.5
    summary = oracle.summary()
    assert summary["blocks"] == 1 and summary["missing_steps"] == 0 and summary["price_filled_hours"] == 0


def test_oracle_fills_missing_prices_with_the_month_mean():
    hours = winter_days(missing_price_hours=((1, 10), (1, 11)))
    oracle = OraclePolicy(hours, PLANT, PARAMS, terminal="value")
    assert oracle.summary()["price_filled_hours"] == 2
    records = run(oracle, hours)
    assert all(r.engine_ok for r in records)


def test_receding_horizon_plans_hourly_and_falls_back_without_prices():
    hours = winter_days()
    for hour in hours:
        if hour.ts_local.date() == dt.date(2026, 1, 11) and hour.ts_local.hour == 5:
            hour.price_ct = None  # day 2 gets no price series (the hour itself keeps none)
    policy = RecedingHorizonPolicy(hours, PLANT, PARAMS, forecast_mode="naive")
    records = run(policy, hours)
    summary = policy.summary()
    assert summary["plans"] > 0 and summary["fallback_steps"] == 24 * 4
    assert {r.decision_id for r in records if r.ts_local.date() == dt.date(2026, 1, 11)} == {"rh_fallback"}
    assert "rh" in {r.decision_id for r in records}
    assert net_cost(records) <= net_cost(run(SelfConsumptionPolicy(), hours)) + 1.0


@pytest.mark.parametrize("mode", ["naive", "calibrated"])
def test_receding_horizon_modes_and_perfect_load(mode):
    hours = winter_days(2)
    policy = RecedingHorizonPolicy(hours, PLANT, PARAMS, forecast_mode=mode, load_mode="perfect")
    records = run(policy, hours)
    assert policy.summary()["fallback_steps"] == 0
    assert any(r.mode == "Akku Netzladen" for r in records)


def test_cli_specs_build_planner_policies_and_report_shows_the_gap():
    hours = winter_days(2)
    sun = SunModel()
    oracle = build_policy("oracle@value", PLANT, sun, hours, PARAMS, "naive", {"wear_ct": 1.0, "soc_step": 1.0})
    assert isinstance(oracle, OraclePolicy) and oracle.terminal == "value" and oracle.wear_ct == 1.0
    rh = build_policy("rh@calibrated", PLANT, sun, hours, PARAMS, "naive", {"load_mode": "perfect"})
    assert isinstance(rh, RecedingHorizonPolicy) and rh.forecast_mode == "calibrated" and rh.load_mode == "perfect"
    with pytest.raises(SystemExit):
        build_policy("mpc", PLANT, sun, hours, PARAMS)
    results = {}
    for name, policy in (("self_consumption", SelfConsumptionPolicy()), ("oracle", OraclePolicy(hours, PLANT, PARAMS)),
                         ("rh@naive", RecedingHorizonPolicy(hours, PLANT, PARAMS))):
        results[name] = metrics.summarize(run(policy, hours), params=PARAMS, feed_in_ct=8)
    report = render(results, {})
    assert "## Lücke zum Oracle" in report
    assert "| oracle | 0.00 |" in report
