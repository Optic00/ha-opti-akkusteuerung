"""Arbitrage economics remain explicit and observational."""

import pytest

from custom_components.opti_akku.arbitrage import (
    build_arbitrage_estimate,
    invalid_arbitrage_fields,
)


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
