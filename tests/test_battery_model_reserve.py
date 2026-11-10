"""The peak reserve reads the discharge efficiency of the battery model (B4)."""

import pytest

from custom_components.opti_akku.battery_model import BatteryModel
from tests.precharge_fixture import day_prices, evaluate

W1 = day_prices(33, 27, [((17, 18), 45), ((18, 19), 60), ((19, 20), 50)])


def reserve(model):
    result = evaluate(W1, current=27, model=model)
    attrs = result.attributes["sensor.opti_peak_reserve_soc"]
    return float(attrs["benoetigt_kwh"]), float(result.states["sensor.opti_peak_reserve_soc"])


def test_without_model_the_reserve_keeps_ninety_percent():
    # 3 h * 0.8 kW / 0.9 = 2.667 kWh; 5 + 2.667 / 12.8 * 100 = 25.8 %
    assert reserve(None) == (2.67, 25.8)


def test_default_model_matches_the_reserve_without_model():
    assert reserve(BatteryModel.from_options(None).engine_state()) == (2.67, 25.8)


def test_configured_discharge_efficiency_scales_the_reserve():
    model = BatteryModel(0.95, 0.95, 0.78125, 1.0, "configured")
    # 3 h * 0.8 kW / 0.95 = 2.526 kWh; 5 + 2.526 / 12.8 * 100 = 24.7 %
    assert reserve(model.engine_state()) == (2.53, 24.7)


@pytest.mark.parametrize("value", [0, -1, 1.5, "abc", None])
def test_implausible_discharge_efficiency_falls_back_to_ninety_percent(value):
    state, attributes = BatteryModel.from_options(None).engine_state()
    assert reserve(("configured", {**attributes, "discharge_efficiency": value})) == (2.67, 25.8)


def test_model_state_without_attributes_falls_back_to_ninety_percent():
    assert reserve(("unavailable", None)) == (2.67, 25.8)
