"""Shadow price-rank allocation for the peak ladder (L1/L2)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.engine import StrategyEngine
from custom_components.opti_akku.peak_rank import (
    PeakRankShadow,
    _grid_charge_possible,
    peak_rank_check,
)

TZ = ZoneInfo("Europe/Berlin")


def local(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ).astimezone(UTC)


def day_prices(default, *ranges):
    """96 quarter-hour prices; ranges are ((h, m), (h, m), price), end exclusive."""
    values = [default] * 96
    for (h1, m1), (h2, m2), price in ranges:
        for index in range((h1 * 60 + m1) // 15, (h2 * 60 + m2) // 15):
            values[index] = price
    return values


def series(night=42.0):
    """Evening peak 16:45-19:00 at 44 ct (18:00 at 44.7), morning peak 07:30-08:45 at 51-53 ct."""
    today = day_prices(30.0, ((16, 45), (19, 0), 44.0), ((18, 0), (18, 15), 44.7),
                       ((19, 0), (24, 0), night))
    tomorrow = day_prices(30.0, ((0, 0), (7, 30), night), ((7, 30), (8, 45), 51.0),
                          ((7, 45), (8, 0), 53.0))
    return {"today": today, "tomorrow": tomorrow}


# Learned morning load including the 20 % profile margin.
MORNING_LOAD = {str(int(local(19, hour).timestamp())): 1600.0 for hour in (7, 8)}


def check(**overrides):
    args = dict(now=local(18, 17, 0), timezone=TZ, price_series=series(),
                horizon_end=local(19, 11, 1).isoformat(), load_hours=MORNING_LOAD,
                fallback_load_kw=0.8, surcharge_ct=5, soc=23, capacity_kwh=12.8, minsoc=5,
                decision_id="peak_l1", peak_reserve_active=True, grid_charging=True,
                spread_ct=10, feed_in_ct=7, score=1)
    args.update(overrides)
    return peak_rank_check(**args)


def test_evening_peak_holds_for_more_expensive_morning_peak():
    result = check()
    assert result["status"] == "ready"
    assert result["verdict"] == "hold"
    assert result["rank_discharges"] is False
    # 2.07 kWh usable: 2.0 kWh morning peak, the rest serves the 44.7 ct slot.
    assert result["usable_kwh"] == 2.074
    assert result["threshold_price_ct"] == 44.7
    assert result["current_price_ct"] == 44.0
    assert result["refill_kind"] == "pv_horizon"
    assert result["refill_at"] == local(19, 11, 1).isoformat()
    assert result["covers_all"] is False
    assert result["reason"].startswith("Halten:")
    assert result["observation_only"] is True and result["controls_battery"] is False


def test_highest_evening_slot_is_still_served():
    result = check(now=local(18, 18, 0))
    assert result["verdict"] == "discharge"
    assert result["current_price_ct"] == 44.7


def test_enough_energy_for_both_peaks_discharges():
    # Night at 33 ct stays below the 35 ct peak filter; without grid charging
    # only the evening and the morning peak draw on the battery.
    result = check(soc=60, price_series=series(night=33.0), grid_charging=False)
    assert result["verdict"] == "discharge"
    assert result["covers_all"] is True
    assert result["reason"].startswith("Entladen:")


@pytest.mark.parametrize("soc,usable,covers_all", [
    pytest.param(10, 0.45, False, id="exact_exhaustion"),
    pytest.param(5, 0, False, id="zero_usable"),
    pytest.param(15, 0.9, True, id="exact_coverage"),
])
def test_coverage_uses_total_demand(soc, usable, covers_all):
    prices = {"today": day_prices(30, ((17, 30), (17, 45), 53),
                                  ((17, 45), (18, 0), 51))}
    result = check(now=local(18, 17, 30), horizon_end=local(18, 18).isoformat(),
                   price_series=prices, load_hours={}, fallback_load_kw=1.8,
                   capacity_kwh=10, soc=soc, grid_charging=False)
    assert result["status"] == "ready"
    assert result["peak_slots"] == 2
    assert result["usable_kwh"] == pytest.approx(usable)
    assert result["demand_kwh"] == pytest.approx(0.9)
    assert result["covers_all"] is covers_all
    assert ("reichen für alle 2 Peak-Slots" in result["reason"]) is covers_all


def test_cheap_refill_without_future_soc_is_uncertain():
    result = check(price_series=series(night=30.0))
    assert result["status"] == result["verdict"] == "uncertain"
    assert result["possible_refill_kind"] == "grid_precharge"
    assert result["possible_refill_at"] == local(18, 19, 0).isoformat()
    assert result["detail"] == "future_charge_conditions"
    assert result["reason"].startswith("Unsicher:")
    for key in ("refill_at", "refill_kind", "covers_all", "rank_discharges"):
        assert key not in result


def test_cheap_night_without_grid_charging_is_no_refill():
    result = check(price_series=series(night=30.0), grid_charging=False)
    assert result["verdict"] == "hold"
    assert result["refill_kind"] == "pv_horizon"


def test_refill_needs_the_full_spread_to_the_peak_average():
    # From 19:00 the peak average is 51.4 ct (4 x 51, 1 x 53), the night 30 ct.
    assert check(price_series=series(night=30.0), spread_ct=21)["possible_refill_kind"] == "grid_precharge"
    assert check(price_series=series(night=30.0), spread_ct=22)["refill_kind"] == "pv_horizon"


def test_night_peak_slots_lower_the_spread_average():
    # At 42 ct the night counts as peak itself. Like the template, the average
    # starts at the full hour: from 07:00 it is (2 x 42, 4 x 51, 53) / 7 = 48.71 ct.
    assert check(spread_ct=10)["refill_kind"] == "pv_horizon"
    assert check(spread_ct=7)["refill_kind"] == "pv_horizon"
    result = check(spread_ct=6.5)
    assert (result["possible_refill_kind"], result["possible_refill_at"]) == ("grid_precharge", local(19, 7, 0).isoformat())


@pytest.mark.parametrize(
    ("spread_ct", "feed_in_ct", "kind"),
    [(100, 7, "grid_below_feed_in"), (10, 0, "grid_precharge")],
)
@pytest.mark.parametrize("minute", [0, 15])
def test_refill_window_uses_only_remaining_slots_after_an_earlier_peak(spread_ct, feed_in_ct, kind, minute):
    # The 17:00 peak stays in the hourly average after it expires, while
    # the remaining charge window has its lowest price at 17:30.
    prices = series()
    prices["today"][69] = 5.0  # 17:15
    prices["today"][70] = 3.0  # 17:30
    result = check(now=local(18, 17, minute), price_series=prices,
                   spread_ct=spread_ct, feed_in_ct=feed_in_ct)
    assert result["status"] == result["verdict"] == "uncertain"
    assert (result["possible_refill_kind"], result["possible_refill_at"]) == (
        kind, local(18, 17, 30).isoformat())


@pytest.mark.parametrize(
    ("spread_ct", "feed_in_ct", "kind"),
    [(100, 7, "grid_below_feed_in"), (10, 0, "grid_precharge")],
)
def test_refill_window_ignores_expired_cheaper_slot(spread_ct, feed_in_ct, kind):
    # At 17:15 the 17:00 slot has ended. The remaining 5 ct slots form the
    # entry window before the 17:45 peak despite the earlier 3 ct price.
    prices = series()
    prices["today"][68] = 3.0  # 17:00
    prices["today"][69] = 5.0  # 17:15
    prices["today"][70] = 5.0  # 17:30
    result = check(price_series=prices, spread_ct=spread_ct, feed_in_ct=feed_in_ct)
    assert result["status"] == result["verdict"] == "uncertain"
    assert (result["possible_refill_kind"], result["possible_refill_at"]) == (
        kind, local(18, 17, 15).isoformat())


def test_later_slot_counts_only_up_to_the_horizon():
    # The 18:00 peak lasts one minute before the horizon: 0.0133 kWh, so most
    # of the 0.15 kWh still serves the current slot.
    result = check(soc=6.3, horizon_end=local(18, 18, 1).isoformat(), load_hours={}, surcharge_ct=0)
    assert result["usable_kwh"] == 0.15
    # 17:00 to 17:45 at 44 ct plus one minute of the 44.7 ct slot.
    assert result["peak_slots"] == 5
    assert result["demand_kwh"] == pytest.approx(4 * 0.2 + 0.8 / 60, abs=1e-3)
    assert result["verdict"] == "discharge"


def test_refill_in_the_last_peak_slot_still_counts():
    today = day_prices(30.0, ((17, 0), (17, 15), 100.0), ((17, 30), (17, 45), 44.0),
                       ((17, 45), (18, 0), 50.0))
    result = check(now=local(18, 17, 30), price_series={"today": today, "tomorrow": []},
                   horizon_end=local(18, 18, 0).isoformat(), load_hours={}, soc=6.3)
    # From 17:00 the peak average is 64.67 ct, 14.67 ct above the 50 ct slot.
    assert (result["possible_refill_kind"], result["possible_refill_at"]) == ("grid_precharge", local(18, 17, 45).isoformat())
    assert result["verdict"] == "uncertain"


def test_window_starts_at_the_local_full_hour():
    kolkata = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 10, 18, 17, 45, tzinfo=kolkata).astimezone(UTC)
    today = day_prices(44.0, ((17, 0), (17, 30), 30.0), ((17, 45), (18, 0), 44.0),
                       ((18, 0), (18, 15), 46.0))
    result = check(now=now, timezone=kolkata, price_series={"today": today, "tomorrow": []},
                   horizon_end=datetime(2026, 10, 18, 19, 0, tzinfo=kolkata).isoformat(),
                   load_hours={}, soc=6.3, grid_charging=False)
    # Window low 30 ct from 17:00 keeps the 46 ct slot a peak, which outranks now.
    assert result["peak_slots"] > 1
    assert result["verdict"] == "hold"


@pytest.mark.parametrize("score", [1, 4, None])
def test_price_below_feed_in_needs_future_soc_and_score(score):
    # Auch ein heutiger Score >= 3 belegt den Score im künftigen Slot nicht.
    result = check(price_series=series(night=5.0), spread_ct=100, score=score)
    assert result["status"] == result["verdict"] == "uncertain"
    assert result["possible_refill_kind"] == "grid_below_feed_in"


def test_small_morning_reserve_does_not_invent_a_refill():
    # 0,2 kWh / 0,9 / 12,8 kWh + MinSOC = 6,7 % Reserve. Die echte
    # Strategie startet erst unter 3,7 %, also nicht bei SoC >= MinSOC.
    prices = {"today": day_prices(30, ((17, 0), (17, 15), 44)),
              "tomorrow": day_prices(30, ((7, 30), (7, 45), 53))}
    args = dict(price_series=prices, soc=6.3, load_hours={})
    assert check(**args, grid_charging=False)["verdict"] == "hold"
    result = check(**args)
    assert result["status"] == result["verdict"] == "uncertain"
    assert result["detail"] == "future_charge_conditions"
    assert "SoC" in result["reason"]
    assert "covers_all" not in result


@pytest.mark.parametrize("tomorrow", [None, [], series()["tomorrow"][:32]])
def test_missing_or_partial_tomorrow_is_incomplete(tomorrow):
    prices = series()
    if tomorrow is None:
        del prices["tomorrow"]
    else:
        prices["tomorrow"] = tomorrow
    result = check(price_series=prices)
    assert result["status"] == "incomplete"
    assert result["verdict"] == "uncertain"
    assert result["detail"] == "price_coverage"
    assert result["price_coverage_end"] == local(19, 0).isoformat()
    assert result["horizon_end"] == local(19, 11, 1).isoformat()
    assert "covers_all" not in result and "rank_discharges" not in result


def test_coverage_exactly_to_boundary_is_sufficient():
    prices = {"today": series()["today"]}
    args = dict(price_series=prices, grid_charging=False)
    complete = check(**args, horizon_end=local(19, 0).isoformat())
    assert complete["status"] == "ready"
    assert complete["refill_at"] == local(19, 0).isoformat()
    incomplete = check(**args, horizon_end=local(19, 0, 1).isoformat())
    assert incomplete["status"] == "incomplete"


def test_early_possible_refill_does_not_hide_missing_horizon_prices():
    # Auch der Peak-Durchschnitt eines frühen Ladefensters braucht den Horizont.
    prices = {"today": series(night=5)["today"]}
    result = check(price_series=prices)
    assert result["status"] == "incomplete"
    assert "refill_at" not in result


@pytest.fixture(scope="module")
def strategy():
    return StrategyEngine()


@pytest.mark.parametrize("kind,overrides,expected", [
    ("grid_precharge", {}, True),
    ("grid_precharge", {"soc": 3.7}, False),
    ("grid_precharge", {"soc": 5}, False),
    ("grid_precharge", {"soc": 5, "charging": True}, True),
    ("grid_precharge", {"soc": 6.7, "charging": True}, False),
    ("grid_precharge", {"average": 39.99}, False),
    ("grid_precharge", {"price": 30.5, "average": 40.5}, True),
    ("grid_precharge", {"price": 30.51, "average": 40.51}, False),
    ("grid_precharge", {"price": 31.5, "average": 41.5, "charging": True}, True),
    ("grid_precharge", {"price": 31.51, "average": 41.51, "charging": True}, False),
    ("grid_below_feed_in", {"soc": 91.9}, True),
    ("grid_below_feed_in", {"soc": 92}, False),
    ("grid_below_feed_in", {"soc": 94.9, "charging": True}, True),
    ("grid_below_feed_in", {"soc": 95, "charging": True}, False),
    ("grid_below_feed_in", {"score": 3}, False),
    ("grid_below_feed_in", {"feed_in": 30}, False),
    ("grid_below_feed_in", {"price": 30.51}, False),
    ("grid_below_feed_in", {"price": 31.5, "charging": True}, True),
    ("grid_below_feed_in", {"price": 31.51, "charging": True}, False),
])
def test_charge_entry_and_hysteresis_match_actual_strategy(strategy, kind, overrides, expected):
    args = dict(price=30, minimum=30, average=40, spread=10, feed_in=7,
                soc=3.6, reserve_soc=6.7, maxsoc=95, score=1, charging=False)
    if kind == "grid_below_feed_in":
        args.update(feed_in=35, average=30)
    args.update(overrides)
    strategy._states = {
        "input_boolean.opti_prognose_netzladen": "on",
        "binary_sensor.opti_peak_reserve_aktiv": "on",
        "input_select.akkusteuerung_modus": "Akku Netzladen" if args["charging"] else "Akku nur Laden",
        "sensor.opti_price_current_ct_kwh": str(args["price"]),
        "sensor.opti_soc": str(args["soc"]),
        "sensor.opti_peak_reserve_soc": str(args["reserve_soc"]),
        "sensor.opti_forecast_score": str(args["score"]),
        "input_number.maxsoc": str(args["maxsoc"]),
        "input_number.opti_netzlade_spread_ct": str(args["spread"]),
        "input_number.opti_einspeiseverguetung_ct": str(args["feed_in"]),
    }
    strategy._attributes = {"sensor.opti_peak_reserve_soc": {
        "min_preis_vor_peak_ct": args["minimum"], "peak_preis_avg_ct": args["average"]}}
    decision = "peak_precharge" if kind == "grid_precharge" else "negative_price"
    branches = strategy.resources["strategy"]["actions"][0]["choose"]
    branch = next(b for b in branches if b["sequence"][0].get("decision_id") == decision)
    assert strategy._all_conditions(branch["conditions"]) is expected
    assert (_grid_charge_possible(**args) == kind) is expected


def test_other_decision_reports_rank_without_peak_verdict():
    result = check(decision_id="reserve_low")
    assert result["verdict"] == "not_peak"
    assert result["strategy_decision"] is None
    assert result["rank_discharges"] is False


def test_l2_is_judged_like_l1():
    assert check(decision_id="peak_l2")["verdict"] == "hold"


def test_daytime_dynamic_mode_keeps_the_verdict_and_reports_the_mode():
    result = check(strategy_mode="Akku Dynamisch")
    assert (result["verdict"], result["strategy_mode"]) == ("hold", "Akku Dynamisch")
    assert check(decision_id="reserve_low", strategy_mode="Akku nur Laden")["strategy_mode"] is None


def test_no_usable_energy_holds():
    result = check(soc=5)
    assert result["verdict"] == "hold"
    assert result["threshold_price_ct"] is None


@pytest.mark.parametrize("overrides,status,detail", [
    ({"peak_reserve_active": False}, "not_applicable", "peak_reserve_inactive"),
    ({"price_series": {"today": [], "tomorrow": []}}, "data_missing", "price_series"),
    ({"horizon_end": None}, "data_missing", "horizon"),
    ({"horizon_end": "2026-10-18T17:00:00"}, "data_missing", "horizon"),
    ({"soc": None}, "data_missing", "soc"),
    ({"soc": 101}, "data_missing", "battery"),
    ({"capacity_kwh": 0}, "data_missing", "battery"),
    ({"fallback_load_kw": 0}, "data_missing", "battery"),
    ({"horizon_end": local(18, 16).isoformat()}, "not_applicable", "no_horizon"),
])
def test_unusable_inputs(overrides, status, detail):
    result = check(**overrides)
    assert (result["status"], result["detail"]) == (status, detail)
    assert result["controls_battery"] is False


def test_invalid_profile_values_fall_back_to_fixed_load():
    bad = {key: -5 for key in MORNING_LOAD}
    # 0.8 kW for the morning peak needs only 1.0 kWh, the evening is served.
    result = check(load_hours=bad)
    assert result["verdict"] == "discharge"
    # Same demand as without any profile, not a zero load.
    assert result["demand_kwh"] == check(load_hours={})["demand_kwh"] > check(fallback_load_kw=0.4, load_hours={})["demand_kwh"]


ASSUMPTIONS = [5, 10, 5, 7, True, 0.8]


def test_shadow_counts_hold_time_and_discharged_energy():
    shadow = PeakRankShadow()
    start = local(18, 17, 0)
    hold = check()
    agree = check(soc=60)
    shadow.observe(start, hold, ASSUMPTIONS, -2000)
    shadow.observe(start + timedelta(seconds=60), agree, ASSUMPTIONS, -1000)
    totals = shadow.observe(start + timedelta(seconds=120), {"status": "ready", "verdict": "not_peak"},
                            ASSUMPTIONS, 0)
    assert totals["peak_seconds"] == 120
    assert totals["hold_seconds"] == 60
    assert totals["agree_seconds"] == 60
    assert totals["discharged_while_hold_kwh"] == pytest.approx(2000 * 60 / 3600 / 1000, abs=1e-3)
    assert totals["discharged_while_agree_kwh"] == pytest.approx(1000 * 60 / 3600 / 1000, abs=1e-3)
    assert totals["hold_percent"] == 50
    totals = shadow.observe(start + timedelta(seconds=180), hold, ASSUMPTIONS, 0)
    assert totals["ready_seconds"] == 180 and totals["peak_seconds"] == 120


def test_shadow_skips_gaps_and_restarts_on_new_assumptions():
    shadow = PeakRankShadow()
    start = local(18, 17, 0)
    shadow.observe(start, check(), ASSUMPTIONS, -2000)
    totals = shadow.observe(start + timedelta(seconds=200), check(), ASSUMPTIONS, -2000)
    assert totals["ready_seconds"] == 0
    shadow.observe(start + timedelta(seconds=260), check(), ASSUMPTIONS, -2000)
    assert shadow.data["hold_seconds"] == 60
    totals = shadow.observe(start + timedelta(seconds=320), check(), [5, 12, 5, 7, True, 0.8], -2000)
    assert totals["ready_seconds"] == 0 and totals["started_at"] == (start + timedelta(seconds=320)).isoformat()


def test_shadow_restore_round_trip_and_rejects_inconsistent_data():
    shadow = PeakRankShadow()
    start = local(18, 17, 0)
    shadow.observe(start, check(), ASSUMPTIONS, -2000)
    shadow.observe(start + timedelta(seconds=60), check(), ASSUMPTIONS, -2000)
    saved = shadow.snapshot()
    restored = PeakRankShadow()
    restored.restore(saved)
    assert restored.data == saved
    for broken in ({**saved, "hold_seconds": -1}, {**saved, "peak_seconds": 999},
                   {**saved, "version": 1}, {**saved, "version": 3},
                   {**saved, "assumptions": [1, 2]},
                   {**saved, "started_at": "2026-10-18T17:00:00"}, None):
        restored.restore(broken)
        assert restored.data["ready_seconds"] == 0


@pytest.mark.parametrize("uncertain", [
    check(price_series=series(night=30)),
    check(price_series={"today": series()["today"]}),
])
def test_uncertain_intervals_never_enter_persistent_totals(uncertain):
    shadow = PeakRankShadow()
    start = local(18, 17)
    for seconds in (0, 60, 120):
        shadow.observe(start + timedelta(seconds=seconds), uncertain, ASSUMPTIONS, -2000)
    saved = shadow.snapshot()
    restored = PeakRankShadow()
    restored.restore(saved)
    assert restored.snapshot() == saved
    for key in ("ready_seconds", "peak_seconds", "agree_seconds", "hold_seconds",
                "discharged_while_agree_kwh", "discharged_while_hold_kwh"):
        assert saved[key] == 0
    # Auch beim Wechsel zurück zu einem gültigen Urteil zählt das unsichere Intervall nicht.
    totals = shadow.observe(start + timedelta(seconds=180), check(), ASSUMPTIONS, -2000)
    assert totals["ready_seconds"] == totals["peak_seconds"] == 0
    totals = shadow.observe(start + timedelta(seconds=240), check(), ASSUMPTIONS, -2000)
    assert totals["hold_seconds"] == 60
