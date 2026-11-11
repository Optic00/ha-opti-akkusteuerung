"""Learned peak load profile in the replay: integration logic, no future knowledge."""
from __future__ import annotations

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tools.replay import simulate
from tools.replay.battery import BatteryParams
from tools.replay.peak_profile import PEAK_LOAD_PROFILE, PeakProfileFeed
from tools.replay.policies import StrategyPolicy
from tools.replay.resources import load_strategy
from tools.replay.timeseries import Hour

TZ = ZoneInfo("Europe/Berlin")
PLANT = {"capacity_kwh": 10, "minsoc": 5, "maxsoc": 95, "feed_in_ct": 8, "netzlade_spread_ct": 10,
         "halte_spread_ct": 5, "max_charge_w": 3000, "max_discharge_w": 5000,
         "pv_surplus_charging": True, "grid_precharge": True,
         "settings": {"input_number.opti_peak_verbrauch_kw": 1.1}}
MONDAY = dt.date(2026, 10, 5)


@pytest.fixture(scope="module")
def head_resources():
    return load_strategy(None)


def day(date: dt.date, load_kwh: float) -> list[Hour]:
    """Constant load, an expensive evening and poor PV."""
    return [Hour(dt.datetime.combine(date, dt.time(hour), tzinfo=TZ), pv_kwh=0.3 if 9 <= hour <= 16 else 0.0,
                 load_kwh=load_kwh, soc=50, price_ct=60.0 if 17 <= hour <= 20 else 20.0, price_src="measured",
                 forecast_today_kwh=2.0, forecast_tomorrow_kwh=2.0) for hour in range(24)]


def spy_profiles(policy: StrategyPolicy) -> dict[dt.datetime, dict]:
    seen: dict[dt.datetime, dict] = {}
    original = policy.engine.evaluate

    def spy(states, attributes, now):
        seen[now] = {"state": states.get(PEAK_LOAD_PROFILE), "attributes": attributes.get(PEAK_LOAD_PROFILE)}
        return original(states, attributes, now)

    policy.engine.evaluate = spy
    return seen


def key(at: dt.datetime) -> str:
    return str(int(at.timestamp()))


def test_profile_learns_only_from_earlier_days_and_falls_back_without_coverage(head_resources):
    loads = (0.4, 0.6, 0.8, 2.0)  # Mon..Thu, kWh per hour
    hours = [hour for offset, load in enumerate(loads) for hour in day(MONDAY + dt.timedelta(days=offset), load)]
    policy = StrategyPolicy(PLANT, resources=head_resources, name="peak", peak_profile=True)
    seen = spy_profiles(policy)
    simulate.run(hours, policy, BatteryParams.from_plant(PLANT), start_soc=50)

    # First day: nothing learned yet, every hour is the fixed value.
    first = seen[dt.datetime.combine(MONDAY, dt.time(12), tzinfo=TZ)]
    assert first["state"] == "fallback"
    assert set(first["attributes"]["methods"].values()) == {"fixed_setting"}
    assert set(first["attributes"]["hours"].values()) == {1100.0}

    # Wednesday: two earlier weekdays are not enough (similar needs 3), still fixed.
    wednesday = dt.datetime.combine(MONDAY + dt.timedelta(days=2), dt.time(0, 15), tzinfo=TZ)
    later = wednesday.replace(hour=10, minute=0)
    assert seen[wednesday]["attributes"]["methods"][key(later)] == "fixed_setting"
    assert seen[wednesday]["attributes"]["hours"][key(later)] == 1100.0

    # Thursday: median of Mon..Wed (600 W) plus the 20 % margin. Thursday's own
    # 2 kW would raise the median to 700 W if it leaked into the profile.
    thursday = wednesday + dt.timedelta(days=1, minutes=15)  # recent 30 min all Thursday
    later = thursday.replace(hour=10, minute=0)
    profile = seen[thursday]["attributes"]
    assert seen[thursday]["state"] == "profile"
    assert profile["methods"][key(later)] == "measured_profile"
    assert profile["hours"][key(later)] == pytest.approx(720.0)
    # The short-term uplift (recent 30 min above the profile) decays within 4 h.
    assert profile["extra_base_load_w"] == pytest.approx(2000.0 - 600.0)
    assert profile["hours"][key(thursday.replace(minute=0))] > 720.0

    summary = policy.summary()
    assert summary["peak_profile_status_steps"]["fallback"] > 0
    assert summary["peak_profile_status_steps"]["profile"] > 0
    assert summary["peak_reserve_steps_with_peak_hours"] > 0
    assert 0 < summary["peak_reserve_profile_hour_share"] < 1


def test_engine_reserve_reads_the_learned_profile(head_resources):
    hours = [hour for offset in range(7) for hour in day(MONDAY + dt.timedelta(days=offset), 0.4)]
    fixed = StrategyPolicy(PLANT, resources=head_resources, name="fixed")
    learned = StrategyPolicy(PLANT, resources=head_resources, name="peak", peak_profile=True)
    for policy in (fixed, learned):
        simulate.run(hours, policy, BatteryParams.from_plant(PLANT), start_soc=50)
    assert learned.summary()["peak_reserve_mean_assumed_load_w"] < 1100.0
    assert learned.summary()["peak_reserve_profile_hour_share"] > 0


def test_measurement_gap_keeps_cells_but_learns_nothing_across_it():
    feed = PeakProfileFeed(900.0)
    start = dt.datetime(2026, 10, 5, 10, tzinfo=TZ)
    settings = {"input_number.opti_peak_verbrauch_kw": 1.1}
    feed.attributes(start, 0.0, settings)  # first step: no previous step to learn
    assert feed.model.cells == {}
    feed.attributes(start + dt.timedelta(minutes=15), 500.0, settings)
    assert feed.model.cells == {"2026-10-05|10|unknown": [450000.0, 0.0, 0.0, 900.0]}
    feed.reset()
    feed.attributes(start + dt.timedelta(hours=8), 0.0, settings)
    feed.attributes(start + dt.timedelta(hours=9), 700.0, settings)  # gap != one step
    assert feed.model.cells == {"2026-10-05|10|unknown": [450000.0, 0.0, 0.0, 900.0]}


def test_dst_fold_hour_accumulates_like_the_integration():
    feed = PeakProfileFeed(900.0)
    settings = {"input_number.opti_peak_verbrauch_kw": 1.1}
    now = dt.datetime(2026, 10, 25, 0, 45, tzinfo=dt.UTC)  # 02:45 CEST, then 02:00 CET
    feed.attributes(now.astimezone(TZ), 0.0, settings)
    for _ in range(8):
        now += dt.timedelta(minutes=15)
        feed.attributes(now.astimezone(TZ), 400.0, settings)
    # 02:45-03:00 CEST plus 02:00-03:00 CET share the local cell of hour 2.
    assert feed.model.cells["2026-10-25|2|unknown"][3] == 4500.0


def test_without_the_variant_nothing_reaches_the_engine_or_the_summary(head_resources):
    hours = day(MONDAY, 0.5)
    policy = StrategyPolicy(PLANT, resources=head_resources, name="strategy")
    seen = spy_profiles(policy)
    records = simulate.run(hours, policy, BatteryParams.from_plant(PLANT), start_soc=50)
    assert all(value == {"state": None, "attributes": None} for value in seen.values())
    assert policy.peak_feed is None
    assert not any(name.startswith("peak") for name in policy.summary())
    again = StrategyPolicy(PLANT, resources=head_resources, name="strategy")
    assert [dataclasses.astuple(r) for r in simulate.run(hours, again, BatteryParams.from_plant(PLANT),
                                                         start_soc=50)] == [dataclasses.astuple(r) for r in records]


def test_build_policy_parses_the_profile_modifiers(monkeypatch):
    from tools.replay.__main__ import build_policy
    from tools.replay.sun import SunModel

    monkeypatch.setattr("tools.replay.policies.StrategyPolicy.__post_init__", lambda self: None)
    assert build_policy("strategy+peakprofil@HEAD", PLANT, SunModel()).peak_profile is True
    both = build_policy("strategy+lastprofil+peakprofil", PLANT, SunModel())
    assert (both.peak_profile, both.house_daily) == (True, "lastprofil")
    plain = build_policy("strategy+lastprofil@main", PLANT, SunModel())
    assert (plain.peak_profile, plain.house_daily, plain.strategy_ref) == (False, "lastprofil", "main")
    for spec in ("strategy+peakprofil+peakprofil", "strategy+unbekannt", "strategyx"):
        with pytest.raises(SystemExit):
            build_policy(spec, PLANT, SunModel())


def test_fixed_value_falls_back_to_the_strategy_default_like_the_coordinator(head_resources):
    plant = {key: value for key, value in PLANT.items() if key != "settings"}
    hours = [hour for offset in range(4) for hour in day(MONDAY + dt.timedelta(days=offset), 0.4)]
    policy = StrategyPolicy(plant, resources=head_resources, name="peak", peak_profile=True)
    seen = spy_profiles(policy)
    simulate.run(hours, policy, BatteryParams.from_plant(plant), start_soc=50)
    first = seen[dt.datetime.combine(MONDAY, dt.time(12), tzinfo=TZ)]["attributes"]
    default_kw = head_resources["helper_defaults"]["input_number.opti_peak_verbrauch_kw"]
    assert set(first["hours"].values()) == {default_kw * 1000}
    assert policy.summary()["peak_profile_status_steps"]["profile"] > 0


def test_uplift_averages_exactly_the_last_30_minutes_for_any_step():
    feed = PeakProfileFeed(1200.0)  # --steps 3
    for offset in range(1, 4):  # three earlier Mondays..Wednesdays at 10:00 with 500 W
        date = (MONDAY - dt.timedelta(days=7 * offset)).isoformat()
        feed.model.cells[f"{date}|10|unknown"] = [500.0 * 3600, 0.0, 0.0, 3600.0]
    settings = {"input_number.opti_peak_verbrauch_kw": 1.1}
    start = dt.datetime.combine(MONDAY, dt.time(9, 20), tzinfo=TZ)
    feed.attributes(start, 0.0, settings)
    feed.attributes(start + dt.timedelta(minutes=20), 400.0, settings)
    profile = feed.attributes(start + dt.timedelta(minutes=40), 2000.0, settings)
    # 10 min of 400 W and 20 min of 2000 W in the window, minus the 500 W profile.
    assert profile["extra_base_load_w"] == pytest.approx((400 * 10 + 2000 * 20) / 30 - 500, abs=0.1)


def test_rounded_sub_steps_still_count_as_contiguous():
    feed = PeakProfileFeed(3600 / 11)  # --steps 11: gaps alternate by one microsecond
    settings = {"input_number.opti_peak_verbrauch_kw": 1.1}
    start = dt.datetime.combine(MONDAY, dt.time(10), tzinfo=TZ).astimezone(dt.UTC)
    for sub in range(12):
        feed.attributes((start + dt.timedelta(hours=sub / 11)).astimezone(TZ), 0.0 if sub == 0 else 600.0, settings)
    assert feed.model.cells[f"{MONDAY.isoformat()}|10|unknown"][3] == pytest.approx(3600.0, abs=1e-3)
