from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.opti_akku.sources import build_inputs, finite
from tests.test_price_sources import dated_prices

NOW = datetime(2026, 9, 12, 6, tzinfo=UTC)


def state(value, unit="W", age=0, **attrs):
    return SimpleNamespace(state=str(value), attributes={"unit_of_measurement": unit, **attrs},
                           last_reported=NOW-timedelta(seconds=age))


def test_house_balance_requires_explicit_topology():
    measurements = {"sensor.opti_house_balance_w": 1200}
    states, _, _ = build_inputs(measurements, {}, {}, NOW)
    assert states["sensor.opti_house_consumption_w"] == "unavailable"
    states, _, _ = build_inputs(measurements, {"single_inverter": True}, {}, NOW)
    assert states["sensor.opti_house_consumption_w"] == 1200


def test_selected_source_overrides_balance_and_converts_units():
    states, _, errors = build_inputs({"sensor.opti_house_balance_w": 1200},
        {"single_inverter": True, "sources": {"house_consumption": "sensor.house"}},
        {"sensor.house": state(2.5, "kW")}, NOW)
    assert states["sensor.opti_house_consumption_w"] == 2500
    assert not errors


@pytest.mark.parametrize("source", [state("unknown"), state(100, age=901), state(-100), state(1, "A")])
def test_invalid_override_never_falls_back_to_plausible_balance(source):
    states, _, errors = build_inputs({"sensor.opti_house_balance_w": 1200},
        {"single_inverter": True, "sources": {"house_consumption": "sensor.house"}},
        {"sensor.house": source}, NOW)
    assert states["sensor.opti_house_consumption_w"] == "unavailable"
    assert errors


def test_price_slots_keep_position_and_negative_prices():
    prices = dated_prices(dt_util.as_local(NOW).date(), timezone=dt_util.DEFAULT_TIME_ZONE)
    prices[0]["price"] = -.1
    options = {"sources": {"price_series": "sensor.prices"}}
    values = {"sensor.prices": state(-.1, "EUR/kWh", today=prices)}
    states, attrs, errors = build_inputs({}, options, values, NOW)
    assert states["sensor.opti_price_series"] == -10
    assert attrs["sensor.opti_price_series"]["today"] == [-10] + [30] * 23
    assert not errors
    prices[3]["price"] = None
    states, attrs, errors = build_inputs({}, options, values, NOW)
    assert states["sensor.opti_price_series"] == "unavailable"
    assert "sensor.opti_price_series" not in attrs
    assert errors["price_series"] == "invalid_price_series"


def test_forecast_p10_normalized_with_state():
    states, attrs, errors = build_inputs({}, {"sources": {"forecast_today": "sensor.forecast"}},
        {"sensor.forecast": state(5000, "Wh", estimate10=2500)}, NOW)
    assert states["sensor.opti_forecast_today_kwh"] == 5
    assert attrs["sensor.opti_forecast_today_kwh"]["estimate10"] == 2.5
    assert not errors


def test_price_unit_conversion_overflow_is_invalid():
    states, _, errors = build_inputs({}, {"sources": {"price_current": "sensor.price"}},
        {"sensor.price": state(1e308, "EUR/kWh")}, NOW)
    assert states["sensor.opti_price_current_ct_kwh"] == "unavailable"
    assert errors == {"price_current": "invalid_value"}


@pytest.mark.parametrize("fields", [{"ev1_mode": "select.ev"}, {"ev1_power": "sensor.ev"}])
def test_configured_ev_missing_is_not_off(fields):
    states, attrs, _ = build_inputs({}, {"sources": fields}, {}, NOW)
    assert states["binary_sensor.opti_ev_lp1_schnell"] == "unavailable"
    assert attrs["binary_sensor.opti_ev_lp1_schnell"]["valide"] is False


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize(
    ("mode", "smart_cost", "expected"),
    [("pv", "on", "on"), ("pv", "off", "off"), ("now", "off", "on"),
     ("minpv", "off", "on")],
)
def test_ev_smart_cost_extends_only_pv_grid_charging(index, mode, smart_cost, expected):
    options = {"sources": {
        f"ev{index}_mode": "select.ev", f"ev{index}_charging": "binary_sensor.ev",
        f"ev{index}_smart_cost": "binary_sensor.smart_cost",
    }}
    external = {
        "select.ev": state(mode),
        "binary_sensor.ev": state("on"),
        "binary_sensor.smart_cost": state(smart_cost),
    }
    states, attrs, errors = build_inputs({}, options, external, NOW)
    key = f"binary_sensor.opti_ev_lp{index}_schnell"
    assert states[key] == expected
    assert attrs[key]["smart_cost_active"] == smart_cost
    assert not errors


def test_ev_pv_without_smart_cost_keeps_existing_behavior():
    options = {"sources": {
        "ev1_mode": "select.ev", "ev1_charging": "binary_sensor.ev",
    }}
    external = {"select.ev": state("pv"), "binary_sensor.ev": state("on")}
    states, _, errors = build_inputs({}, options, external, NOW)
    assert states["binary_sensor.opti_ev_lp1_schnell"] == "off"
    assert not errors


@pytest.mark.parametrize("smart_cost", [None, state("unknown"), state("on", age=901)])
def test_ev_pv_with_invalid_smart_cost_is_unavailable(smart_cost):
    options = {"sources": {
        "ev1_mode": "select.ev", "ev1_charging": "binary_sensor.ev",
        "ev1_smart_cost": "binary_sensor.smart_cost",
    }}
    external = {"select.ev": state("pv"), "binary_sensor.ev": state("on")}
    if smart_cost is not None:
        external["binary_sensor.smart_cost"] = smart_cost
    states, attrs, errors = build_inputs({}, options, external, NOW)
    key = "binary_sensor.opti_ev_lp1_schnell"
    assert states[key] == "unavailable"
    assert attrs[key]["valide"] is False
    assert errors["ev1_smart_cost"] == "missing_or_stale"


def test_ev_now_lock_survives_missing_configured_smart_cost():
    options = {"sources": {
        "ev1_mode": "select.ev", "ev1_charging": "binary_sensor.ev",
        "ev1_smart_cost": "binary_sensor.smart_cost",
    }}
    external = {"select.ev": state("now"), "binary_sensor.ev": state("on")}
    states, attrs, errors = build_inputs({}, options, external, NOW)
    key = "binary_sensor.opti_ev_lp1_schnell"
    assert states[key] == "on"
    assert attrs[key]["valide"] is True
    assert not errors


@pytest.mark.parametrize("smart_cost", [state("on"), None, state("on", age=901)])
def test_ev_not_charging_never_starts_lock(smart_cost):
    options = {"sources": {
        "ev1_mode": "select.ev", "ev1_charging": "binary_sensor.ev",
        "ev1_smart_cost": "binary_sensor.smart_cost",
    }}
    external = {"select.ev": state("pv"), "binary_sensor.ev": state("off")}
    if smart_cost is not None:
        external["binary_sensor.smart_cost"] = smart_cost
    states, attrs, errors = build_inputs({}, options, external, NOW)
    key = "binary_sensor.opti_ev_lp1_schnell"
    assert states[key] == "off"
    assert attrs[key]["valide"] is True
    assert not errors


def test_ev_smart_cost_unavailable_holds_and_then_releases_restored_latch():
    from tests.test_engine import StrategyEngine, measurements, solar_attrs

    engine = StrategyEngine()
    options = {"single_inverter": True, "sources": {
        "ev1_mode": "select.ev", "ev1_charging": "binary_sensor.ev",
        "ev1_smart_cost": "binary_sensor.smart_cost",
    }}

    def evaluate(seconds, charging, smart_cost):
        now = NOW + timedelta(seconds=seconds)
        external = {"select.ev": state("pv"), "binary_sensor.ev": state(charging)}
        if smart_cost is not None:
            external["binary_sensor.smart_cost"] = state(smart_cost)
        for value in external.values():
            value.last_reported = now
        native = measurements(**{"sensor.opti_house_balance_w": 400,
                                 "input_boolean.opti_ev_akku_pause": "on"})
        states, attrs, _ = build_inputs(native, options, external, now)
        attrs.update(solar_attrs(now))
        return engine.evaluate(states, attrs, now).states["binary_sensor.opti_ev_schnellladung"]

    assert evaluate(0, "on", "on") == "on"
    snapshot = engine.snapshot()
    engine = StrategyEngine()
    engine.restore(snapshot)
    assert evaluate(1, "on", None) == "on"
    assert evaluate(2, "off", None) == "on"
    assert evaluate(301, "off", None) == "on"
    assert evaluate(302, "off", None) == "off"


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("restore", [False, True])
def test_single_ev_source_pipeline_releases_after_five_minutes(index, restore):
    from tests.test_engine import StrategyEngine, measurements, solar_attrs

    engine = StrategyEngine()
    options = {"single_inverter": True, "sources": {
        f"ev{index}_mode": "select.ev", f"ev{index}_charging": "binary_sensor.ev"}}

    def evaluate(seconds, charging):
        now = NOW + timedelta(seconds=seconds)
        external = {"select.ev": state("now"), "binary_sensor.ev": state(charging)}
        for value in external.values():
            value.last_reported = now
        native = measurements(**{"sensor.opti_house_balance_w": 400,
                                 "input_boolean.opti_ev_akku_pause": "on"})
        states, attrs, _ = build_inputs(native, options, external, now)
        attrs.update(solar_attrs(now))
        return engine.evaluate(states, attrs, now).states["binary_sensor.opti_ev_schnellladung"]

    assert evaluate(0, "on") == "on"
    if restore:
        snapshot = engine.snapshot()
        engine = StrategyEngine()
        engine.restore(snapshot)
    assert evaluate(1, "off") == "on"
    assert evaluate(300, "off") == "on"
    assert evaluate(301, "off") == "off"


def test_unconfigured_ev_is_valid_off():
    states, attrs, _ = build_inputs({}, {}, {}, NOW)
    for index in (1, 2):
        key = f"binary_sensor.opti_ev_lp{index}_schnell"
        assert states[key] == "off"
        assert attrs[key]["valide"] is True


@pytest.mark.parametrize("value", [None, True, "unavailable", "NaN", "inf", -float("inf")])
def test_only_finite_numbers(value):
    assert finite(value) is None


def test_external_hybrid_ac_can_be_negative_while_house_cannot():
    states, _, errors = build_inputs({}, {"sources": {"pv_power": "sensor.ac", "house_consumption": "sensor.house"}},
        {"sensor.ac": state(-1, "kW"), "sensor.house": state(-100)}, NOW)
    assert states["sensor.opti_pv_power_w"] == -1000
    assert "pv_power" not in errors
    assert errors["house_consumption"] == "negative_value"
