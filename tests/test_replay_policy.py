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
    assert len(summary["strategy_sha256"]) == 64 and summary["strategy_origin"] == "injected resources"
    assert summary["engine_path"].endswith("engine.py") and len(summary["engine_sha256"]) == 64
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


def test_dst_fall_back_day_keeps_unique_increasing_sub_steps():
    date = dt.date(2025, 10, 26)
    hours = []
    start = dt.datetime.combine(date, dt.time(), tzinfo=TZ)
    current = start
    while current.date() == date:
        hours.append(Hour(current, pv_kwh=0.2, load_kwh=0.3, soc=50, price_ct=25.0, price_src="measured",
                          forecast_today_kwh=5.0, forecast_tomorrow_kwh=5.0))
        current = (current.astimezone(dt.UTC) + dt.timedelta(hours=1)).astimezone(TZ)
    assert len(hours) == 25
    for records in (simulate.run(hours, SelfConsumptionPolicy(), BatteryParams.from_plant(PLANT)),
                    simulate.run_actual([h for h in hours if setattr(h, "charge_kwh", 0.0) is None
                                         and setattr(h, "discharge_kwh", 0.0) is None
                                         and setattr(h, "export_kwh", 0.0) is None
                                         and setattr(h, "import_kwh", 0.1) is None])):
        assert len(records) == 100
        instants = [r.ts_local.astimezone(dt.UTC) for r in records]
        assert len(set(instants)) == 100
        assert all(b - a == dt.timedelta(minutes=15) for a, b in zip(instants, instants[1:], strict=False))
        assert sum(1 for r in records if r.ts_local.hour == 2) == 8


def test_measurement_blocks_restart_from_measured_soc_with_warmup(head_resources):
    first = peak_day(dt.date(2026, 4, 30))
    second = peak_day(dt.date(2026, 9, 1)) + peak_day(dt.date(2026, 9, 2))
    for hour in second:
        hour.soc = 20
    policy = StrategyPolicy(PLANT, resources=head_resources, name="test")
    records = simulate.run(first + second, policy, BatteryParams.from_plant(PLANT), start_soc=80)
    assert policy.resets == 1 and policy.summary()["block_restarts"] == 1
    assert records[0].soc_before == 80
    restart = next(r for r in records if r.ts_local.date() == dt.date(2026, 9, 1))
    assert restart.soc_before == 20 and restart.warmup
    assert all(r.warmup for r in records if r.ts_local.date() == dt.date(2026, 9, 1))
    assert not any(r.warmup for r in records if r.ts_local.date() != dt.date(2026, 9, 1))


def test_current_price_reaches_the_engine_even_without_a_complete_day_series(head_resources):
    hours = peak_day()
    for hour in hours:
        hour.price_ct, hour.price_src = None, "missing"
    hours[14].price_ct, hours[14].price_src = -5.0, "measured"  # below the 8 ct feed-in tariff
    policy, records = run_day(head_resources, hours)
    assert policy.error_steps == 0
    assert {r.decision_id for r in records if r.ts_local.hour == 14} == {"negative_price"}
    assert "peak_l1" not in {r.decision_id for r in records}  # no series -> no price level


def test_unknown_charge_limit_means_zero_watts_like_the_coordinator(head_resources):
    policy = StrategyPolicy(PLANT, resources=head_resources, name="test", battery_temp_c=float("nan"))
    records = simulate.run(peak_day(), policy, BatteryParams.from_plant(PLANT), start_soc=50)
    assert policy.charge_limit_unknown_steps == 96
    assert all(r.charge_limit_w == 0 for r in records)
    assert sum(r.charge_kwh for r in records) == 0
    assert policy.summary()["charge_limit_unknown_steps"] == 96


def test_naive_forecast_mode_uses_previous_month_climatology(head_resources):
    hours = peak_day(dt.date(2026, 10, 18)) + peak_day(dt.date(2026, 10, 19))
    params = BatteryParams.from_plant(PLANT)
    calibrated = simulate.run(hours, StrategyPolicy(PLANT, resources=head_resources, name="c"), params,
                              start_soc=50, forecast_mode="calibrated")
    naive = simulate.run(hours, StrategyPolicy(PLANT, resources=head_resources, name="n"), params,
                         start_soc=50, forecast_mode="naive")
    assert len(calibrated) == len(naive) == 192
    profile = simulate.climatology(hours)[(2026, 10)]
    assert sum(profile) == pytest.approx(1.0)
    assert simulate.previous_month((2026, 1)) == (2025, 12)
    with pytest.raises(ValueError):
        simulate.run(hours, SelfConsumptionPolicy(), params, forecast_mode="perfect")


def test_incomplete_hours_are_skipped_and_soc_is_carried():
    hours = peak_day()
    hours[12].pv_kwh = None
    records = simulate.run(hours, SelfConsumptionPolicy(), BatteryParams.from_plant(PLANT), start_soc=50)
    assert len(records) == 92
    assert records[48].ts_local.hour == 13
    assert records[48].soc_before == pytest.approx(records[47].soc_after)


def test_observed_scores_and_horizon_reach_the_records(head_resources):
    policy, records = run_day(head_resources)
    scored = [item for item in records if item.score is not None]
    assert scored and all(0 <= item.score <= 10 for item in scored)
    assert any(item.night for item in records) and any(not item.night for item in records)
    assert {item.horizon_long for item in records} <= {True, False, None}
    assert policy.summary()["house_daily"] == "engine"


def test_lastprofil_variant_injects_the_previous_months_daily_load(head_resources):
    hours = peak_day(dt.date(2026, 9, 30)) + peak_day(dt.date(2026, 10, 1))
    policy = StrategyPolicy(PLANT, resources=head_resources, name="profil", house_daily="lastprofil")
    seen = {}
    original = policy.engine.evaluate

    def spy(states, attributes, now):
        seen[now.date()] = (states.get("sensor.opti_house_consumption_24h_w"),
                            attributes.get("sensor.opti_house_consumption_24h_w"))
        return original(states, attributes, now)

    policy.engine.evaluate = spy
    simulate.run(hours, policy, BatteryParams.from_plant(PLANT), start_soc=50)
    # 0.5 kWh per hour -> 12 kWh per day -> 500 W, with full coverage so the templates accept it.
    assert seen[dt.date(2026, 10, 1)][0] == 500
    assert seen[dt.date(2026, 10, 1)][1]["coverage_ok"] is True
    # Without a previous month nothing is injected: the engine statistic (no future knowledge) stands.
    assert seen[dt.date(2026, 9, 30)] == (None, None)
    assert simulate.load_day_climatology(hours)[(2026, 9)] == pytest.approx(12.0)
    assert policy._engine_resources is not head_resources
    assert all(definition["sample_interval"] == {"seconds": 900.0}
               for definition in policy._engine_resources["statistics"] if "sample_interval" in definition)
    with pytest.raises(ValueError):
        StrategyPolicy(PLANT, resources=head_resources, house_daily="perfect")


def test_build_policy_rasters_the_daily_statistics_to_the_simulation_step(head_resources, monkeypatch):
    from tools.replay.__main__ import build_policy
    from tools.replay.sun import SunModel

    monkeypatch.setattr("tools.replay.policies.StrategyPolicy.__post_init__", lambda self: None)
    hourly = build_policy("strategy@HEAD", PLANT, SunModel(), steps_per_hour=1)
    quarter = build_policy("strategy@HEAD", PLANT, SunModel())
    assert hourly.sample_interval_s == 3600.0
    assert quarter.sample_interval_s == 900.0
