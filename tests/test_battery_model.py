"""One battery model for efficiencies and cycle costs (B4)."""

from math import isfinite

import pytest

from custom_components.opti_akku.battery_model import (
    ENGINE_ENTITY,
    FIELDS,
    BatteryModel,
    invalid_arbitrage_fields,
)

CONFIGURED = {
    "enabled": True,
    "battery_price_eur": 6000,
    "degradation_percent": 20,
    "cycles": 6000,
    "usable_capacity_kwh": 12.8,
    "charge_efficiency_percent": 95,
    "discharge_efficiency_percent": 95,
    "margin_ct": 1,
}


def assert_defaults(model):
    assert model.charge_efficiency == 0.9
    assert model.discharge_efficiency == 0.9
    assert model.throughput_cost_ct_kwh == 1.5
    assert model.margin_ct == 0
    assert model.cycle_cost_floor_ct_kwh == 3.0
    assert model.wear_ct_per_discharged_ac_kwh == pytest.approx(3.3333, abs=1e-4)


@pytest.mark.parametrize(
    "options",
    [None, {}, {**CONFIGURED, "enabled": False}, "not a mapping", {**CONFIGURED, "enabled": "true"}],
)
def test_without_enabled_arbitrage_the_defaults_apply(options):
    model = BatteryModel.from_options(options)
    assert model.source == "default"
    assert model.economics_valid is True
    assert_defaults(model)
    assert model.roundtrip_efficiency == pytest.approx(0.81)


def test_configured_options_derive_efficiencies_and_cycle_costs():
    model = BatteryModel.from_options(CONFIGURED)
    assert model.source == "configured"
    assert model.economics_valid is True
    assert model.charge_efficiency == 0.95
    assert model.discharge_efficiency == 0.95
    # 6000 EUR * 20 % / (2 * 6000 cycles * 12.8 kWh) * 100 = 0.78125 ct
    assert model.throughput_cost_ct_kwh == pytest.approx(0.78125)
    assert model.cycle_cost_floor_ct_kwh == pytest.approx(2.5625)
    # (27 / 0.95 + 2.5625) / 0.95
    assert model.minimum_high_price(27) == pytest.approx(32.6143, abs=1e-3)


@pytest.mark.parametrize(
    ("low", "high"),
    [(27, 36.667), (30, 40.370), (38, 50.247), (0, 3.333), (-5, -2.840)],
)
def test_default_minimum_high_price(low, high):
    assert BatteryModel.from_options(None).minimum_high_price(low) == pytest.approx(high, abs=1e-3)


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
        ("cycles", None),
        ("margin_ct", True),
        ("battery_price_eur", "abc"),
        ("usable_capacity_kwh", float("nan")),
    ],
)
def test_each_invalid_field_falls_back_to_defaults_without_exception(field, value):
    model = BatteryModel.from_options({**CONFIGURED, field: value})
    assert model.source == "invalid"
    assert model.economics_valid is False
    assert_defaults(model)


def test_missing_field_is_invalid():
    config = dict(CONFIGURED)
    del config["cycles"]
    assert BatteryModel.from_options(config).source == "invalid"


@pytest.mark.parametrize("field", ["degradation_percent", "battery_price_eur"])
def test_no_value_loss_is_valid_and_free(field):
    model = BatteryModel.from_options({**CONFIGURED, field: 0})
    assert model.source == "configured"
    assert model.throughput_cost_ct_kwh == 0
    assert model.cycle_cost_floor_ct_kwh == 1


def test_validation_is_the_shared_arbitrage_rule():
    from custom_components.opti_akku import arbitrage

    assert arbitrage.invalid_arbitrage_fields is invalid_arbitrage_fields
    assert arbitrage.FIELDS is FIELDS
    assert invalid_arbitrage_fields(CONFIGURED) == set()


@pytest.mark.parametrize("options", [None, CONFIGURED, {**CONFIGURED, "cycles": 0}])
def test_engine_state_reports_all_values(options):
    model = BatteryModel.from_options(options)
    state, attributes = model.engine_state()
    assert ENGINE_ENTITY == "sensor.opti_battery_model"
    assert state == model.source == attributes["source"]
    numbers = {
        "charge_efficiency",
        "discharge_efficiency",
        "roundtrip_efficiency",
        "throughput_cost_ct_kwh",
        "cycle_cost_floor_ct_kwh",
        "margin_ct",
    }
    assert numbers | {"source"} <= set(attributes)
    for key in numbers:
        assert isinstance(attributes[key], float | int) and not isinstance(attributes[key], bool)
        assert isfinite(attributes[key])
    assert attributes["discharge_efficiency"] == model.discharge_efficiency
    assert attributes["throughput_cost_ct_kwh"] == round(model.throughput_cost_ct_kwh, 3)

