"""Ambitious grid precharge before sunrise (issue #222)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku import ambitious_precharge as ap
from custom_components.opti_akku.battery_model import BatteryModel
from custom_components.opti_akku.engine import Evaluation

TZ = ZoneInfo("Europe/Berlin")
MODEL = BatteryModel(0.95, 0.95, 1.0, 0.5, "configured")


def local(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).astimezone(UTC)


def day_prices(default, *ranges):
    values = [default] * 96
    for (h1, m1), (h2, m2), price in ranges:
        for index in range((h1 * 60 + m1) // 15, (h2 * 60 + m2) // 15):
            values[index] = price
    return values


def evening_prices(day=34.0, cheap=20.0):
    """Seen at 23:00 on the 14th: today's evening, tomorrow with a cheap night."""
    return {"today": day_prices(32.0), "tomorrow": day_prices(day, ((1, 0), (5, 0), cheap))}


def night_prices(day=34.0, cheap=20.0):
    """Seen after midnight on the 15th: the cheap night is today."""
    return {"today": day_prices(day, ((1, 0), (5, 0), cheap))}


def pv(peak, day=15, missing=()):
    """Hourly (P10, P50) kWh: ``peak`` 10-14 h, a third of it 8-16 h."""
    hours = {}
    for hour in range(24):
        if hour in missing:
            continue
        kw = peak if 10 <= hour < 14 else (peak / 3 if 8 <= hour < 16 else 0.0)
        hours[local(day, hour)] = (kw, kw * 3)
    return hours


def run(**overrides):
    args = dict(now=local(14, 23), timezone=TZ, price_series=evening_prices(), pv_hours=pv(1.0),
                quantile="p10", load_hours={}, fallback_load_kw=0.5, soc=20, capacity_kwh=10,
                minsoc=10, maxsoc=95, max_charge_w=5000, feed_in_ct=7,
                sunrise=local(15, 7, 40).isoformat(), sunset=local(15, 16, 30).isoformat(),
                model=MODEL, min_gain_ct=2.0)
    args.update(overrides)
    return ap.plan(**args)


def evaluation(decision="default", mode="Akku Automatisch"):
    return Evaluation(
        states={"sensor.opti_strategie_vorschau": mode}, mode=mode, reason="Standard",
        attributes={"sensor.opti_strategie_vorschau": {"grund": "Standard", "x": 1}},
        helper_updates={}, decision_id=decision)


ACTIVE = {"active": True, "action": "charge", "target_soc": 70.5, "charge_price_ct": 20.0}
HOLD = {**ACTIVE, "action": "hold"}


# -- option -------------------------------------------------------------------

@pytest.mark.parametrize(("options", "expected"), [
    ({}, None),
    (None, None),
    ({"ambitious_precharge": "on"}, None),
    ({"ambitious_precharge": {"enabled": True}}, None),
    ({"ambitious_precharge": {"enabled": False, "risk_accepted": True}}, None),
    ({"ambitious_precharge": {"enabled": True, "risk_accepted": True}},
     {"quantile": "p10", "min_gain_ct": 2.0}),
    ({"ambitious_precharge": {"enabled": True, "risk_accepted": True, "quantile": "p50",
                              "min_gain_ct": 0}}, {"quantile": "p50", "min_gain_ct": 0.0}),
    ({"ambitious_precharge": {"enabled": True, "risk_accepted": True, "quantile": "p90"}}, None),
    ({"ambitious_precharge": {"enabled": True, "risk_accepted": True, "min_gain_ct": 99}}, None),
    ({"ambitious_precharge": {"enabled": True, "risk_accepted": True, "min_gain_ct": "x"}}, None),
])
def test_option_needs_enabled_and_risk_accepted(options, expected):
    assert ap.option_config(options) == expected


# -- plan ---------------------------------------------------------------------

def test_dull_day_charges_tonight_in_the_cheapest_slots():
    report = run()
    assert report["status"] == "planned"
    assert report["active"] is False
    assert report["action"] is None
    # The day deficit after the window plus the load from 5 h to sunrise.
    assert report["target_soc"] == 56.0
    assert report["first_at"] == local(15, 1).isoformat()
    assert report["window_end"] == local(15, 5).isoformat()
    assert report["charge_price_ct"] == 20.0
    assert report["expected_grid_kwh"] == pytest.approx(4.84)
    assert report["expected_export_kwh"] == 0.0
    assert report["expected_saving_eur"] > 0
    assert all(local(15, 1) <= datetime.fromisoformat(s) < local(15, 5) for s in report["slots"])


def test_plan_is_active_inside_the_window_and_below_the_target():
    report = run(now=local(15, 1, 5), price_series=night_prices())
    assert report["status"] == "active"
    assert report["active"] is True
    assert report["action"] == "charge"
    assert report["target_soc"] == 56.0


def test_window_holds_the_battery_at_the_target():
    report = run(now=local(15, 3), price_series=night_prices(), soc=60)
    assert report["action"] == "hold"
    assert report["status"] == "active"
    # After the window the strategy decides again.
    assert run(now=local(15, 5, 30), price_series=night_prices(), soc=60).get("active") is not True


def test_start_band_and_latch_near_the_target():
    soc = 54
    report = run(now=local(15, 1, 5), price_series=night_prices(), soc=soc)
    target = report["target_soc"]
    assert target - ap.START_BAND < soc < target
    assert report["action"] == "hold"
    latched = run(now=local(15, 1, 5), price_series=night_prices(), soc=soc, charging=True)
    assert latched["action"] == "charge"
    assert run(now=local(15, 1, 5), price_series=night_prices(), soc=target,
               charging=True)["action"] == "hold"


def test_running_window_keeps_a_slot_within_the_price_band():
    prices = {"today": day_prices(34.0, ((1, 0), (1, 15), 20.0), ((1, 15), (5, 0), 20.3))}
    # The 20 ct slot is over; a 20.3 ct slot is inside the band of the 20.3 ct window.
    fresh = run(now=local(15, 1, 20), price_series=prices, soc=30)
    assert fresh["action"] == "charge"
    band = {"today": day_prices(34.0, ((1, 0), (1, 30), 20.0), ((1, 30), (1, 45), 20.4),
                                ((1, 45), (5, 0), 20.0))}
    outside = run(now=local(15, 1, 35), price_series=band, soc=30)
    assert outside["active"] is False
    assert run(now=local(15, 1, 35), price_series=band, soc=30, charging=True)["action"] == "charge"


def test_sunny_day_only_charges_for_the_morning():
    sunny, dull = run(pv_hours=pv(6.0)), run()
    assert sunny["status"] == "planned"
    assert sunny["target_soc"] < dull["target_soc"]
    # The PV overflow is exported anyway; the night charge adds nothing to it.
    assert sunny["expected_export_kwh"] == pytest.approx(sunny["expected_export_kwh_without"], abs=0.1)


def test_median_quantile_sees_more_pv():
    assert run(quantile="p50", pv_hours=pv(1.0))["target_soc"] < run(pv_hours=pv(1.0))["target_soc"]


def test_expensive_night_is_not_worth_it():
    flat = {"today": day_prices(34.0), "tomorrow": day_prices(34.0)}
    assert run(price_series=flat)["status"] == "not_worth_it"


def test_small_spread_is_not_worth_it():
    assert run(price_series=evening_prices(day=24.0))["status"] == "not_worth_it"
    # Without the minimum gain the same spread pays off.
    assert run(price_series=evening_prices(day=26.0), min_gain_ct=0.0)["status"] == "planned"
    assert run(price_series=evening_prices(day=26.0), min_gain_ct=3.0)["status"] == "not_worth_it"


def test_full_battery_needs_nothing():
    assert run(soc=95)["status"] == "not_worth_it"


def test_target_never_exceeds_maxsoc():
    report = run(pv_hours=pv(0.0), fallback_load_kw=2.0, maxsoc=80)
    assert report["status"] == "planned"
    assert report["target_soc"] == 80.0


def test_load_profile_hours_override_the_fallback():
    heavy = {str(int(local(15, hour).timestamp())): 3000 for hour in range(17, 22)}
    assert run(load_hours=heavy)["target_soc"] > run()["target_soc"]
    # Implausible values fall back to the setting.
    junk = {str(int(local(15, hour).timestamp())): -5 for hour in range(24)}
    assert run(load_hours=junk)["target_soc"] == run()["target_soc"]


def test_invalid_settings_fall_back_to_defaults():
    report = run(fallback_load_kw="x", max_charge_w=None, feed_in_ct=None)
    assert report["charge_kw"] == 5.0
    assert report["status"] == "planned"


def test_pv_hours_after_sunset_may_be_missing():
    assert run(pv_hours=pv(1.0, missing=range(17, 24)))["status"] == "planned"


@pytest.mark.parametrize(("overrides", "status"), [
    ({"soc": None}, "data_missing"),
    ({"capacity_kwh": 0}, "data_missing"),
    ({"minsoc": 96}, "data_missing"),
    ({"soc": 120}, "data_missing"),
    ({"model": BatteryModel(0.95, 0.95, 1.0, 0.5, "invalid")}, "battery_model_invalid"),
    ({"sunrise": None}, "data_missing"),
    ({"now": local(15, 12), "sunrise": local(16, 7, 40).isoformat()}, "daylight"),
    ({"price_series": {"today": day_prices(32.0)}}, "price_horizon_short"),
    ({"price_series": None}, "price_horizon_short"),
    ({"pv_hours": {}}, "pv_forecast_missing"),
    ({"quantile": "p90"}, "pv_forecast_missing"),
    ({"pv_hours": pv(1.0, missing=(12,))}, "pv_forecast_missing"),
    ({"pv_hours": {local(15, 9): (-1, 0)}}, "pv_forecast_missing"),
])
def test_missing_inputs_plan_nothing(overrides, status):
    report = run(**overrides)
    assert report["status"] == status
    assert report.get("active") is not True


def test_window_limits_are_spread_over_many_prices():
    night = [(local(15, 1), local(15, 1, 15), float(price)) for price in range(30)]
    limits = ap._thresholds(night)
    assert len(limits) == ap.MAX_THRESHOLDS
    assert limits[0] == 0.0 and limits[-1] == 29.0
    assert ap._thresholds(night[:3]) == [0.0, 1.0, 2.0]


# -- apply --------------------------------------------------------------------

@pytest.mark.parametrize("decision", sorted(ap.OVERRIDABLE_DECISIONS))
def test_low_priority_decisions_become_grid_charging(decision):
    result = ap.apply(evaluation(decision), ACTIVE, eligible=True, timezone=TZ)
    assert result.mode == "Akku Netzladen"
    assert result.decision_id == "ambitious_precharge"
    assert result.states["sensor.opti_strategie_vorschau"] == "Akku Netzladen"
    grund = result.attributes["sensor.opti_strategie_vorschau"]["grund"]
    assert grund == "Ehrgeiziges Vorladen (bis 70.5 %, Ø 20 ct)"
    assert result.attributes["sensor.opti_strategie_vorschau"]["x"] == 1


@pytest.mark.parametrize("decision", ["balancing_grid", "ev_lock", "negative_price", "maximum_soc",
                                      "peak_l1", "surplus_70", "standby_reserve", "unknown"])
def test_protective_and_charging_decisions_stay(decision):
    original = evaluation(decision)
    assert ap.apply(original, ACTIVE, eligible=True, timezone=TZ) is original


def test_hold_blocks_discharging():
    result = ap.apply(evaluation("peak_l3", "Akku Pause"), HOLD, eligible=True, timezone=TZ)
    assert result.mode == "Akku nur Laden"
    assert result.reason == "Ehrgeiziges Vorladen: Akku hält 70.5 % für den Tag"
    holding = evaluation("default", "Akku nur Laden")
    assert ap.apply(holding, HOLD, eligible=True, timezone=TZ) is holding


def test_inactive_plan_or_ineligible_keeps_the_decision():
    original = evaluation()
    assert ap.apply(original, {"active": False}, eligible=True, timezone=TZ) is original
    assert ap.apply(original, ACTIVE, eligible=False, timezone=TZ) is original
    charging = evaluation("default", "Akku Netzladen")
    assert ap.apply(charging, ACTIVE, eligible=True, timezone=TZ) is charging


# -- standby reserve ----------------------------------------------------------

def test_standby_window_adds_the_planned_charge():
    report = {"status": "planned", "first_at": local(15, 1).isoformat(),
              "sunrise": local(15, 7, 40).isoformat()}
    window = ap.standby_window(report, {"status": "none"})
    assert window == {"status": "planned", "at": local(15, 1).isoformat(),
                      "kind": "ambitious_precharge", "sunrise": local(15, 7, 40).isoformat()}
    planned = {"status": "planned", "at": "x"}
    assert ap.standby_window(report, planned) is planned
    assert ap.standby_window({"status": "not_worth_it"}, {"status": "none"}) == {"status": "none"}
    late = {**report, "first_at": local(15, 8).isoformat()}
    assert ap.standby_window(late, {"status": "none"}) == {"status": "none"}
