"""Arbitrage economics remain explicit and observational."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.arbitrage import (
    apply_discharge_hold,
    build_arbitrage_estimate,
    build_terminal_value_proxy,
    invalid_arbitrage_fields,
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
