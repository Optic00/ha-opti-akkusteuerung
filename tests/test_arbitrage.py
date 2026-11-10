"""Arbitrage economics remain explicit and observational."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.arbitrage import (
    apply_discharge_hold,
    build_arbitrage_estimate,
    build_terminal_value_proxy,
    PrechargeComparison,
    invalid_arbitrage_fields,
    precharge_assumptions,
    precharge_spread_check,
)
from custom_components.opti_akku.engine import Evaluation


CONFIG = {
    "enabled": True,
    "battery_price_eur": 5000,
    "degradation_percent": 20,
    "cycles": 5000,
    "usable_capacity_kwh": 10,
    "charge_efficiency_percent": 90,
    "discharge_efficiency_percent": 90,
    "margin_ct": 2,
}


def evaluation(decision_id="peak_l2", mode="Akku nur Entladen", soc=50):
    return Evaluation(
        {
            "sensor.opti_soc": soc,
            "sensor.opti_battery_capacity_kwh": 10,
            "input_number.minsoc": 10,
            "input_number.maxsoc": 90,
        },
        {},
        mode,
        "existing decision",
        {},
        decision_id=decision_id,
    )


def hold_report(now, **overrides):
    return {
        "status": "ready",
        "hold_enabled": True,
        "terminal_value_status": "ready",
        "terminal_value_issued_at": now.isoformat(),
        "terminal_value_current_soc": 50,
        "terminal_value_battery_capacity_kwh": 10,
        "terminal_value_minimum_soc": 10,
        "terminal_value_maximum_soc": 90,
        "terminal_value_current_marginal_ct_kwh": 30,
        "throughput_cost_ct_kwh": 1,
        "battery_price_eur": 5000,
        "degradation_percent": 20,
        "cycles": 5000,
        "usable_capacity_kwh": 10,
        "charge_efficiency": 0.9,
        "discharge_efficiency": 0.9,
        "margin_ct": 2,
        **overrides,
    }


def test_unconfigured_estimate_has_no_silent_assumptions():
    result = build_arbitrage_estimate({}, 10)
    assert result == {
        "status": "not_configured",
        "informational_only": True,
        "controls_battery": False,
    }


def test_cost_and_efficiency_formula_use_cents_per_kwh():
    result = build_arbitrage_estimate(CONFIG, 10)
    assert result["status"] == "ready"
    assert result["throughput_cost_ct_kwh"] == 1
    assert result["cycle_cost_floor_ct_kwh"] == 4
    assert result["minimum_high_price_ct_kwh"] == pytest.approx(16.79, abs=0.001)
    assert result["minimum_spread_ct_kwh"] == pytest.approx(6.79, abs=0.001)
    assert result["informational_only"] is True
    assert result["controls_battery"] is False


def test_price_is_required_only_for_price_dependent_spread():
    result = build_arbitrage_estimate(CONFIG)
    assert result["status"] == "price_missing"
    assert result["minimum_spread_ct_kwh"] is None
    assert result["cycle_cost_floor_ct_kwh"] == 4


def test_disabled_strategy_is_reported_before_missing_price():
    result = build_arbitrage_estimate(CONFIG, strategy_enabled=False)
    assert result == {
        "status": "strategy_disabled",
        "informational_only": True,
        "controls_battery": False,
    }


def test_margin_is_battery_side_and_efficiencies_are_independent():
    result = build_arbitrage_estimate(
        {
            **CONFIG,
            "charge_efficiency_percent": 80,
            "discharge_efficiency_percent": 95,
            "margin_ct": 3,
        },
        10,
    )
    assert result["cycle_cost_floor_ct_kwh"] == 5
    assert result["minimum_high_price_ct_kwh"] == pytest.approx(18.421, abs=0.001)
    assert result["minimum_spread_ct_kwh"] == pytest.approx(8.421, abs=0.001)


def test_active_hold_uses_fresh_terminal_value_for_known_discharge_decision():
    now = datetime(2026, 1, 15, tzinfo=ZoneInfo("UTC"))
    result, report = apply_discharge_hold(
        evaluation(), hold_report(now), {**CONFIG, "hold_enabled": True}, 10, now
    )
    assert result.mode == "Akku nur Laden"
    assert result.decision_id == "arbitrage_hold"
    assert "Restwert halten" in result.reason
    assert report["hold_status"] == "holding"
    assert report["hold_controls_battery"] is True
    assert report["hold_current_net_ct_kwh"] == pytest.approx(8.889, abs=0.001)


@pytest.mark.parametrize(
    ("decision_id", "mode"),
    [
        ("minimum_soc", "Akku nur Laden"),
        ("negative_price", "Akku Netzladen"),
        ("unknown", "Akku nur Entladen"),
    ],
)
def test_active_hold_never_overrides_protected_or_unknown_decisions(decision_id, mode):
    now = datetime(2026, 1, 15, tzinfo=ZoneInfo("UTC"))
    before = evaluation(decision_id, mode)
    result, report = apply_discharge_hold(
        before, hold_report(now), {**CONFIG, "hold_enabled": True}, 10, now
    )
    assert result is before
    assert report["hold_status"] == "higher_priority"


def test_active_hold_releases_when_current_value_covers_future_and_margin():
    now = datetime(2026, 1, 15, tzinfo=ZoneInfo("UTC"))
    before = evaluation()
    result, report = apply_discharge_hold(
        before, hold_report(now), {**CONFIG, "hold_enabled": True}, 40, now
    )
    assert result is before
    assert report["hold_status"] == "release"


def test_active_hold_has_half_cent_hysteresis_and_never_holds_at_zero_value():
    now = datetime(2026, 1, 15, tzinfo=ZoneInfo("UTC"))
    # Current net value is 8.889 ct/kWh. An advantage of 2.3 ct is below the
    # 2.5 ct entry edge, but above the 1.5 ct release edge once already held.
    marginal = 11.189
    fresh = hold_report(now, terminal_value_current_marginal_ct_kwh=marginal)
    assert apply_discharge_hold(
        evaluation(), fresh, {**CONFIG, "hold_enabled": True}, 10, now
    )[1]["hold_status"] == "release"
    continuing = {**fresh, "hold_status": "holding"}
    assert apply_discharge_hold(
        evaluation(), continuing, {**CONFIG, "hold_enabled": True}, 10, now
    )[1]["hold_status"] == "holding"
    zero = hold_report(
        now,
        terminal_value_current_marginal_ct_kwh=0,
        throughput_cost_ct_kwh=0,
        margin_ct=0,
    )
    zero_config = {**CONFIG, "hold_enabled": True, "margin_ct": 0}
    assert apply_discharge_hold(
        evaluation(), zero, zero_config, 0, now
    )[1]["hold_status"] == "release"


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (None, "waiting_for_fresh_report"),
        ({"terminal_value_status": "learning"}, "waiting_for_fresh_report"),
    ],
)
def test_active_hold_fails_closed_without_fresh_complete_report(report, expected):
    now = datetime(2026, 1, 15, tzinfo=ZoneInfo("UTC"))
    before = evaluation()
    result, status = apply_discharge_hold(
        before, report, {**CONFIG, "hold_enabled": True}, 10, now
    )
    assert result is before
    assert status["hold_status"] == expected


def test_active_hold_rejects_stale_soc_or_changed_assumptions():
    now = datetime(2026, 1, 15, tzinfo=ZoneInfo("UTC"))
    stale = hold_report(now - timedelta(seconds=91))
    assert apply_discharge_hold(
        evaluation(), stale, {**CONFIG, "hold_enabled": True}, 10, now
    )[1]["hold_status"] == "waiting_for_fresh_report"
    mismatch = hold_report(now, battery_price_eur=4000)
    assert apply_discharge_hold(
        evaluation(), mismatch, {**CONFIG, "hold_enabled": True}, 10, now
    )[1]["hold_status"] == "waiting_for_matching_report"
    soc_changed = hold_report(now, terminal_value_current_soc=45)
    assert apply_discharge_hold(
        evaluation(), soc_changed, {**CONFIG, "hold_enabled": True}, 10, now
    )[1]["hold_status"] == "data_missing"


def test_terminal_proxy_values_most_expensive_residual_load_first():
    timezone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 1, 15, tzinfo=timezone)
    slots = [
        {
            "start": (now + timedelta(hours=offset)).isoformat(),
            "end": (now + timedelta(hours=offset + 1)).isoformat(),
            "load_w": 1000,
            "pv_p10_w": 0,
        }
        for offset in range(3)
    ]
    result = build_terminal_value_proxy(
        forecast_slots=slots,
        price_series={"today": [40, 30, 10] + [0] * 21, "tomorrow": []},
        now=now,
        timezone=timezone,
        refill_from=now + timedelta(hours=3),
        battery_capacity_kwh=10,
        discharge_efficiency=1,
        throughput_cost_ct_kwh=1,
        current_soc=50,
        minimum_soc=10,
        maximum_soc=90,
        profile_ready=True,
    )
    assert result["terminal_value_status"] == "ready"
    assert result["terminal_value_reason"] == "complete_profile_and_price_horizon"
    assert result["terminal_value_residual_load_kwh"] == 3
    assert result["terminal_value_curve_knee_kwh"] == 3
    assert result["terminal_value_first_marginal_ct_kwh"] == 39
    assert result["terminal_value_full_marginal_ct_kwh"] == 0
    assert result["terminal_value_full_battery_eur"] == pytest.approx(0.77)
    assert result["terminal_value_current_battery_eur"] == pytest.approx(0.77)
    assert result["terminal_value_current_marginal_ct_kwh"] == 0
    assert result["terminal_value_controls_battery"] is False


def test_terminal_proxy_splits_forecast_across_quarter_hour_prices():
    timezone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 1, 15, tzinfo=timezone)
    result = build_terminal_value_proxy(
        forecast_slots=[{
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "load_w": 1000,
            "pv_p10_w": 0,
        }],
        price_series={"today": [40, 30, 20, 10] + [0] * 92, "tomorrow": []},
        now=now,
        timezone=timezone,
        refill_from=now + timedelta(hours=1),
        battery_capacity_kwh=1,
        discharge_efficiency=1,
        throughput_cost_ct_kwh=0,
        current_soc=50,
        minimum_soc=0,
        maximum_soc=100,
        profile_ready=False,
    )
    assert result["terminal_value_status"] == "learning"
    assert result["terminal_value_reason"] == "profile_not_ready"
    assert result["terminal_value_price_coverage_percent"] == 100
    assert result["terminal_value_full_battery_eur"] == pytest.approx(0.25)


def test_terminal_proxy_keeps_missing_inputs_explicit():
    result = build_terminal_value_proxy(
        forecast_slots=[], price_series={}, now=None, timezone=None,
        refill_from=None, battery_capacity_kwh=10, discharge_efficiency=0.9,
        throughput_cost_ct_kwh=1,
    )
    assert result == {
        "terminal_value_status": "data_missing",
        "terminal_value_reason": "forecast_missing",
        "terminal_value_controls_battery": False,
        "terminal_value_method": "concave_residual_load_proxy",
    }


def test_terminal_proxy_is_additive_to_existing_arbitrage_result():
    timezone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 1, 15, tzinfo=timezone)
    result = build_arbitrage_estimate(
        CONFIG,
        10,
        terminal_context={
            "forecast_slots": [{
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "load_w": 1000,
                "pv_p10_w": 0,
            }],
            "price_series": {"today": [30] * 24, "tomorrow": []},
            "now": now,
            "timezone": timezone,
            "refill_from": now + timedelta(hours=1),
            "battery_capacity_kwh": 10,
            "current_soc": 50,
            "minimum_soc": 10,
            "maximum_soc": 90,
            "profile_ready": True,
        },
    )
    assert result["minimum_spread_ct_kwh"] == pytest.approx(6.79, abs=0.001)
    assert result["terminal_value_status"] == "ready"
    assert result["controls_battery"] is False
    assert result["terminal_value_controls_battery"] is False


def test_terminal_proxy_stops_before_pv_refill():
    timezone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 1, 15, tzinfo=timezone)
    result = build_terminal_value_proxy(
        forecast_slots=[
            {
                "start": (now + timedelta(hours=offset)).isoformat(),
                "end": (now + timedelta(hours=offset + 1)).isoformat(),
                "load_w": 1000,
                "pv_p10_w": 0,
            }
            for offset in range(3)
        ],
        price_series={"today": [10, 20, 100] + [0] * 21, "tomorrow": []},
        now=now,
        timezone=timezone,
        refill_from=now + timedelta(hours=2),
        battery_capacity_kwh=10,
        discharge_efficiency=1,
        throughput_cost_ct_kwh=0,
        current_soc=100,
        minimum_soc=0,
        maximum_soc=100,
        profile_ready=True,
    )
    assert result["terminal_value_horizon_end"] == (
        now + timedelta(hours=2)
    ).astimezone(ZoneInfo("UTC")).isoformat()
    assert result["terminal_value_residual_load_kwh"] == 2
    assert result["terminal_value_first_marginal_ct_kwh"] == 20
    assert result["terminal_value_full_battery_eur"] == pytest.approx(0.3)


def test_terminal_proxy_marks_forecast_gap_as_learning():
    timezone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 1, 15, tzinfo=timezone)
    result = build_terminal_value_proxy(
        forecast_slots=[{
            "start": (now + timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
            "load_w": 1000,
            "pv_p10_w": 0,
        }],
        price_series={"today": [30] * 24, "tomorrow": []},
        now=now,
        timezone=timezone,
        refill_from=now + timedelta(hours=2),
        battery_capacity_kwh=10,
        discharge_efficiency=1,
        throughput_cost_ct_kwh=0,
        current_soc=50,
        minimum_soc=10,
        maximum_soc=90,
        profile_ready=True,
    )
    assert result["terminal_value_status"] == "learning"
    assert result["terminal_value_reason"] == "forecast_horizon_incomplete"
    assert result["terminal_value_forecast_coverage_percent"] == 50


@pytest.mark.parametrize(
    ("day", "hours"),
    [(datetime(2026, 3, 29).date(), 23), (datetime(2026, 10, 25).date(), 25)],
)
def test_terminal_proxy_uses_real_dst_day_length(day, hours):
    timezone = ZoneInfo("Europe/Berlin")
    local_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone)
    local_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone)
    start = local_start.astimezone(ZoneInfo("UTC"))
    end = local_end.astimezone(ZoneInfo("UTC"))
    slots = [
        {
            "start": (start + timedelta(hours=offset)).isoformat(),
            "end": (start + timedelta(hours=offset + 1)).isoformat(),
            "load_w": 1000,
            "pv_p10_w": 0,
        }
        for offset in range(hours)
    ]
    result = build_terminal_value_proxy(
        forecast_slots=slots,
        price_series={"today": [30] * hours, "tomorrow": []},
        now=start,
        timezone=timezone,
        refill_from=end,
        battery_capacity_kwh=10,
        discharge_efficiency=1,
        throughput_cost_ct_kwh=0,
        current_soc=50,
        minimum_soc=10,
        maximum_soc=90,
        profile_ready=True,
    )
    assert result["terminal_value_status"] == "ready"
    assert result["terminal_value_window_hours"] == hours
    assert result["terminal_value_price_coverage_percent"] == 100


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"refill_from": None}, "refill_boundary_missing"),
        ({"price_series": None}, "price_horizon_missing"),
        ({"price_series": {"today": [30] * 10}}, "price_horizon_missing"),
        ({"price_series": {"today": [30] * 23 + [None]}}, "price_horizon_missing"),
        ({"battery_capacity_kwh": 0}, "battery_capacity_invalid"),
    ],
)
def test_terminal_proxy_rejects_invalid_context(overrides, reason):
    timezone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 1, 15, tzinfo=timezone)
    arguments = {
        "forecast_slots": [{
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "load_w": 1000,
            "pv_p10_w": 0,
        }],
        "price_series": {"today": [30] * 24, "tomorrow": []},
        "now": now,
        "timezone": timezone,
        "refill_from": now + timedelta(hours=1),
        "battery_capacity_kwh": 10,
        "discharge_efficiency": 0.9,
        "throughput_cost_ct_kwh": 1,
        "current_soc": 50,
        "minimum_soc": 10,
        "maximum_soc": 90,
        "profile_ready": True,
    }
    result = build_terminal_value_proxy(**{**arguments, **overrides})
    assert result["terminal_value_status"] == "data_missing"
    assert result["terminal_value_reason"] == reason


@pytest.mark.parametrize(
    "bad_row",
    [
        "not-a-row",
        {"start": "not-a-time", "end": "not-a-time", "load_w": 1000, "pv_p10_w": 0},
    ],
)
def test_terminal_proxy_keeps_malformed_or_out_of_order_rows_visible(bad_row):
    timezone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 1, 15, tzinfo=timezone)
    result = build_terminal_value_proxy(
        forecast_slots=[
            bad_row,
            {
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "load_w": 1000,
                "pv_p10_w": 0,
            },
        ],
        price_series={"today": [30] * 24, "tomorrow": []},
        now=now,
        timezone=timezone,
        refill_from=now + timedelta(hours=1),
        battery_capacity_kwh=10,
        discharge_efficiency=1,
        throughput_cost_ct_kwh=0,
        current_soc=50,
        minimum_soc=10,
        maximum_soc=90,
        profile_ready=True,
    )
    assert result["terminal_value_status"] == "learning"
    assert result["terminal_value_reason"] == "forecast_horizon_incomplete"


def test_terminal_proxy_rejects_empty_or_unpriced_horizon():
    timezone = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 1, 15, tzinfo=timezone)
    common = {
        "now": now,
        "timezone": timezone,
        "battery_capacity_kwh": 10,
        "discharge_efficiency": 1,
        "throughput_cost_ct_kwh": 0,
        "current_soc": 50,
        "minimum_soc": 10,
        "maximum_soc": 90,
        "profile_ready": True,
    }
    empty = build_terminal_value_proxy(
        forecast_slots=[{
            "start": (now + timedelta(hours=2)).isoformat(),
            "end": (now + timedelta(hours=3)).isoformat(),
            "load_w": 1000,
            "pv_p10_w": 0,
        }],
        price_series={"today": [30] * 24},
        refill_from=now + timedelta(hours=1),
        **common,
    )
    assert empty["terminal_value_reason"] == "forecast_missing"

    tomorrow = datetime(2026, 1, 16, tzinfo=timezone)
    unpriced = build_terminal_value_proxy(
        forecast_slots=[{
            "start": tomorrow.isoformat(),
            "end": (tomorrow + timedelta(hours=1)).isoformat(),
            "load_w": 1000,
            "pv_p10_w": 0,
        }],
        price_series={"today": [30] * 24, "tomorrow": []},
        refill_from=tomorrow + timedelta(hours=1),
        **common,
    )
    assert unpriced["terminal_value_reason"] == "price_horizon_missing"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("battery_price_eur", -1),
        ("degradation_percent", 101),
        ("cycles", 0),
        ("usable_capacity_kwh", 0),
        ("charge_efficiency_percent", 0),
        ("discharge_efficiency_percent", 101),
        ("margin_ct", -1),
    ],
)
def test_invalid_assumptions_are_named(field, value):
    config = {**CONFIG, field: value}
    assert invalid_arbitrage_fields(config) == {field}
    assert build_arbitrage_estimate(config, 10)["status"] == "invalid"


def test_invalid_assumption_helper_rejects_missing_config_and_boolean():
    assert invalid_arbitrage_fields({}) == set(CONFIG) - {"enabled"}
    assert "cycles" in invalid_arbitrage_fields({**CONFIG, "cycles": True})


# --- Peak pre-charge shadow comparison -------------------------------------

READY = {"status": "ready", "minimum_high_price_ct_kwh": 20.0}
ACTIVE = {
    "binary_sensor.opti_peak_reserve_aktiv": "on",
    "sensor.opti_price_current_ct_kwh": "5",
}
PEAK_ATTRS = {"sensor.opti_peak_reserve_soc": {"peak_preis_avg_ct": 18.0}}
SETTINGS = {
    "input_boolean.opti_prognose_netzladen": True,
    "input_number.opti_netzlade_spread_ct": 10,
}


def check(estimate=READY, states=ACTIVE, attrs=PEAK_ATTRS, settings=SETTINGS, decision="peak_l1"):
    return precharge_spread_check(estimate, states, attrs, settings, decision)


@pytest.mark.parametrize(
    ("states", "attrs", "settings"),
    [
        ({**ACTIVE, "binary_sensor.opti_peak_reserve_aktiv": "off"}, PEAK_ATTRS, SETTINGS),
        (ACTIVE, PEAK_ATTRS, {**SETTINGS, "input_boolean.opti_prognose_netzladen": False}),
        (ACTIVE, {}, SETTINGS),
        (ACTIVE, {"sensor.opti_peak_reserve_soc": {"peak_preis_avg_ct": "nan"}}, SETTINGS),
        ({**ACTIVE, "sensor.opti_price_current_ct_kwh": "unavailable"}, PEAK_ATTRS, SETTINGS),
        ({**ACTIVE, "sensor.opti_price_current_ct_kwh": True}, PEAK_ATTRS, SETTINGS),
    ],
)
def test_precharge_check_not_applicable(states, attrs, settings):
    result = check(states=states, attrs=attrs, settings=settings)
    assert result["status"] == "not_applicable"
    assert result["compares"] == "spread_criterion_only"
    assert result["controls_battery"] is False
    assert "verdict" not in result


@pytest.mark.parametrize(
    "estimate",
    [
        {"status": "price_missing", "minimum_high_price_ct_kwh": None},
        {"status": "ready", "minimum_high_price_ct_kwh": None},
        {"status": "ready", "minimum_high_price_ct_kwh": float("inf")},
        None,
    ],
)
def test_precharge_check_estimate_unavailable(estimate):
    result = check(estimate=estimate)
    assert result["status"] == "estimate_unavailable"
    assert "verdict" not in result


@pytest.mark.parametrize(
    ("avg", "cur", "minimum", "verdict"),
    [
        (30.0, 5.0, 20.0, "both_allow"),
        (12.0, 1.0, 20.0, "fixed_only"),
        (22.0, 15.0, 20.0, "derived_only"),
        (10.0, 5.0, 20.0, "both_block"),
    ],
)
def test_precharge_check_verdicts(avg, cur, minimum, verdict):
    result = check(
        estimate={"status": "ready", "minimum_high_price_ct_kwh": minimum},
        states={**ACTIVE, "sensor.opti_price_current_ct_kwh": str(cur)},
        attrs={"sensor.opti_peak_reserve_soc": {"peak_preis_avg_ct": avg}},
    )
    assert result["status"] == "ready"
    assert result["verdict"] == verdict
    assert result["actual_spread_ct"] == round(avg - cur, 3)
    assert result["derived_min_spread_ct"] == round(minimum - cur, 3)
    assert result["fixed_min_spread_ct"] == 10
    assert result["precharge_active"] is False


def test_precharge_check_boundaries_are_inclusive():
    result = check(
        estimate={"status": "ready", "minimum_high_price_ct_kwh": 15.0},
        attrs={"sensor.opti_peak_reserve_soc": {"peak_preis_avg_ct": 15.0}},
    )
    assert result["fixed_allows"] is True  # spread 10 >= 10
    assert result["derived_allows"] is True  # 15 >= 15
    assert result["verdict"] == "both_allow"


def test_precharge_check_flags_active_decision_and_default_spread():
    result = check(decision="peak_precharge", settings={"input_boolean.opti_prognose_netzladen": "on"})
    assert result["precharge_active"] is True
    assert result["fixed_min_spread_ct"] == 10.0
    bad = check(settings={**SETTINGS, "input_number.opti_netzlade_spread_ct": "x"})
    assert bad["fixed_min_spread_ct"] == 10.0


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("UTC"))
ASSUME = precharge_assumptions(CONFIG, SETTINGS)


def ready_check(verdict="both_allow", active=False, derived=True):
    return {"status": "ready", "verdict": verdict, "precharge_active": active, "derived_allows": derived}


def test_precharge_assumptions_fingerprint():
    assert ASSUME == [5000, 20, 5000, 10, 90, 90, 2, 10.0]
    assert precharge_assumptions(None, None)[-1] == 10.0


def test_precharge_comparison_holds_previous_check():
    acc = PrechargeComparison()
    first = acc.observe(NOW, ready_check("fixed_only", True, False), ASSUME)
    assert first["ready_seconds"] == 0
    assert first["derived_disagreement_percent"] is None
    totals = acc.observe(NOW + timedelta(seconds=30), ready_check("both_allow"), ASSUME)
    assert totals["fixed_only_seconds"] == 30
    assert totals["precharge_seconds"] == 30
    assert totals["precharge_derived_blocks_seconds"] == 30
    totals = acc.observe(NOW + timedelta(seconds=60), {"status": "not_applicable"}, ASSUME)
    assert totals["both_allow_seconds"] == 30
    assert totals["ready_seconds"] == 60
    assert totals["derived_disagreement_percent"] == 50.0
    totals = acc.observe(NOW + timedelta(seconds=90), ready_check(), ASSUME)
    assert totals["ready_seconds"] == 60  # held not_applicable interval adds nothing


def test_precharge_comparison_interrupt_never_counts_the_failed_interval():
    acc = PrechargeComparison()
    acc.observe(NOW, ready_check(), ASSUME)
    acc.interrupt()
    totals = acc.observe(NOW + timedelta(seconds=60), ready_check(), ASSUME)
    assert totals["ready_seconds"] == 0
    totals = acc.observe(NOW + timedelta(seconds=90), ready_check(), ASSUME)
    assert totals["ready_seconds"] == 30


def test_precharge_comparison_ignores_gaps_and_backwards_clock():
    acc = PrechargeComparison()
    acc.observe(NOW, ready_check(), ASSUME)
    assert acc.observe(NOW + timedelta(seconds=91), ready_check(), ASSUME)["ready_seconds"] == 0
    assert acc.observe(NOW + timedelta(seconds=151), ready_check(), ASSUME)["ready_seconds"] == 60
    assert acc.observe(NOW, ready_check(), ASSUME)["ready_seconds"] == 60
    assert acc.observe(NOW + timedelta(seconds=10), ready_check(), ASSUME)["ready_seconds"] == 70


def test_precharge_comparison_resets_on_changed_assumptions():
    acc = PrechargeComparison()
    acc.observe(NOW, ready_check(), ASSUME)
    acc.observe(NOW + timedelta(seconds=30), ready_check(), ASSUME)
    later = NOW + timedelta(seconds=60)
    totals = acc.observe(later, ready_check(), [*ASSUME[:-1], 12.0])
    assert totals["ready_seconds"] == 0
    assert totals["started_at"] == later.isoformat()
    assert acc.observe(later + timedelta(seconds=10), ready_check(), [*ASSUME[:-1], 12.0])["ready_seconds"] == 10


def test_precharge_comparison_roundtrip_skips_restart_interval():
    acc = PrechargeComparison()
    acc.observe(NOW, ready_check("derived_only"), ASSUME)
    acc.observe(NOW + timedelta(seconds=20), ready_check(), ASSUME)
    saved = acc.snapshot()
    restored = PrechargeComparison()
    restored.restore(saved)
    totals = restored.observe(NOW + timedelta(seconds=30), ready_check(), ASSUME)
    assert totals["derived_only_seconds"] == 20
    assert totals["ready_seconds"] == 20  # first check after restart has no hold
    assert restored.observe(NOW + timedelta(seconds=40), ready_check(), ASSUME)["ready_seconds"] == 30


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(version=2),
        lambda s: s.update(started_at="2026-10-10T12:00:00"),
        lambda s: s.update(started_at="garbage"),
        lambda s: s.update(last_observation=5),
        lambda s: s.update(assumptions=[1, 2]),
        lambda s: s.update(assumptions="x"),
        lambda s: s.update(ready_seconds=-1),
        lambda s: s.update(ready_seconds="nan"),
        lambda s: s.pop("both_allow_seconds"),
    ],
)
def test_precharge_comparison_restore_rejects_broken_data(mutate):
    acc = PrechargeComparison()
    acc.observe(NOW, ready_check(), ASSUME)
    acc.observe(NOW + timedelta(seconds=10), ready_check(), ASSUME)
    saved = acc.snapshot()
    mutate(saved)
    fresh = PrechargeComparison()
    fresh.restore(saved)
    assert fresh.snapshot() == PrechargeComparison().snapshot()


@pytest.mark.parametrize("saved", [None, "x", 5, [], {}])
def test_precharge_comparison_restore_survives_non_mapping(saved):
    acc = PrechargeComparison()
    acc.restore(saved)
    assert acc.snapshot() == PrechargeComparison().snapshot()


def test_estimate_from_battery_model_is_unchanged():
    """Golden values of the display before the shared battery model (B4)."""
    config = {
        "enabled": True,
        "battery_price_eur": 6000,
        "degradation_percent": 20,
        "cycles": 6000,
        "usable_capacity_kwh": 12.8,
        "charge_efficiency_percent": 95,
        "discharge_efficiency_percent": 95,
        "margin_ct": 1,
    }
    assert build_arbitrage_estimate(config, 27) == {
        "battery_price_eur": 6000.0,
        "charge_efficiency": 0.95,
        "charge_price_ct_kwh": 27.0,
        "controls_battery": False,
        "cycle_cost_floor_ct_kwh": 2.562,
        "cycles": 6000.0,
        "degradation_percent": 20.0,
        "degradation_value_eur": 1200.0,
        "discharge_efficiency": 0.95,
        "formula": (
            "p_high_min = (p_low / eta_charge + 2 * c_throughput + margin) / eta_discharge"
        ),
        "informational_only": True,
        "margin_ct": 1.0,
        "minimum_high_price_ct_kwh": 32.614,
        "minimum_spread_ct_kwh": 5.614,
        "status": "ready",
        "throughput_cost_ct_kwh": 0.781,
        "usable_capacity_kwh": 12.8,
    }
