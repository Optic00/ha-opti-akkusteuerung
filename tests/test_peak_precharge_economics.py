"""Peak precharging from the grid must cover losses and cycle costs (A3/B2).

Fixture F (tests/precharge_fixture.py): 14.01.2026 13:05, valley 12-15 h is the
current price. The "today" behaviour in the docstrings was measured on the code
before this change: every profile W1 to W7 charged from the grid.
"""

from __future__ import annotations

import pytest

from custom_components.opti_akku.battery_model import BatteryModel
from custom_components.opti_akku.reporting import precharge_economics
from tests.precharge_fixture import MODEL_ENTITY, day_prices, evaluate

DEFAULT = BatteryModel.from_options(None)
K1_OPTIONS = {
    "enabled": True, "battery_price_eur": 6000, "degradation_percent": 20, "cycles": 6000,
    "usable_capacity_kwh": 12.8, "charge_efficiency_percent": 95,
    "discharge_efficiency_percent": 95, "margin_ct": 1,
}
K1 = BatteryModel.from_options(K1_OPTIONS)
K2 = BatteryModel.from_options({
    "enabled": True, "battery_price_eur": 8000, "degradation_percent": 30, "cycles": 4000,
    "usable_capacity_kwh": 12.8, "charge_efficiency_percent": 85,
    "discharge_efficiency_percent": 85, "margin_ct": 3,
})
INVALID = BatteryModel.from_options({**K1_OPTIONS, "cycles": 0})

# (base, valley, peaks, extra quarter-hours, minute)
PROFILES = {
    "W1": (33, 27, [((17, 18), 45), ((18, 19), 60), ((19, 20), 50)], (), 5),
    "W2": (44, 38, [((17, 20), 49)], (), 5),
    "W3": (34, 30, [((16, 17), 40.2), ((17, 20), 58)], (), 5),
    "W4": (34, 30, [((17, 20), 41)], (), 5),
    "W5": (31, 27, [((17, 20), 37.5)], (), 5),
    "W6": (34, 30, [((17, 20), 40.1)], (), 5),
    # 13:00-13:15 is an expired 55 ct peak slot of the running hour; 13:20.
    "W7": (34, 30, [((17, 20), 40.0)], ((52, 55),), 20),
}
FUTURE = {"W1": 51.67, "W2": 49.0, "W3": 53.55, "W4": 41.0, "W5": 37.5, "W6": 40.1, "W7": 40.0}


def run(profile, *, model=DEFAULT, mode="Akku Dynamisch", overrides=None, raw_model=None):
    base, valley, peaks, extra, minute = PROFILES[profile]
    values = {"input_select.akkusteuerung_modus": mode, **(overrides or {})}
    state = raw_model if raw_model is not None else (
        model.engine_state() if model is not None else None)
    return evaluate(day_prices(base, valley, peaks, extra), current=valley, minute=minute,
                    model=state, overrides=values)


def precharges(result):
    reason = result.attributes["sensor.opti_strategie_vorschau"]["grund"]
    charged = result.decision_id == "peak_precharge"
    # I3: preview reason and decision tell the same story.
    assert reason.startswith("Peak-Vorladen") is charged
    if charged:
        assert result.mode == "Akku Netzladen"
    return charged


def reserve(result):
    return result.attributes["sensor.opti_peak_reserve_soc"]


# -- default model -------------------------------------------------------------

def test_w1_charges_with_reason_and_unchanged_reserve():
    result = run("W1")
    assert precharges(result)
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == 25.8
    # p_min(27) = 27 / 0.81 + 3.333 = 36.667
    assert result.attributes["sensor.opti_strategie_vorschau"]["grund"] == (
        "Peak-Vorladen (51.67 ct Spitze, Mindestpreis 36.7 ct, bis 25.8%)")


def test_w2_high_valley_no_longer_charges():
    # today: Netzladen (spread 11). p_min(38) = 50.247 > 49.0
    result = run("W2")
    assert not precharges(result)
    assert result.mode != "Akku Netzladen"


def test_w3_shoulder_slot_below_threshold_does_not_block():
    result = run("W3")
    assert precharges(result)
    assert reserve(result)["peak_preis_min_kuenftig_ct"] == 40.2  # below p_min(30) = 40.37


@pytest.mark.parametrize(("profile", "expected"), [("W4", True), ("W5", True), ("W6", False)])
def test_threshold_edges(profile, expected):
    # W4 41.0 >= 40.370, W5 37.5 >= 36.667, W6 40.1 < 40.370 (today: Netzladen)
    assert precharges(run(profile)) is expected


def test_w6_hold_band_keeps_running_precharge():
    # 40.1 + 1.0 >= 40.370, fixed spread 10.1, window ok
    assert precharges(run("W6", mode="Akku Netzladen"))


def test_w7_expired_peak_slot_no_longer_counts():
    result = run("W7")
    attrs = reserve(result)
    assert attrs["peak_preis_avg_ct"] == 41.15  # old value includes the expired 55 ct
    assert attrs["peak_preis_kuenftig_ct"] == 40.0
    assert not precharges(result)  # today: Netzladen because of spread 11.15


@pytest.mark.parametrize("overrides", [
    {"sensor.opti_soc": 40},  # SoC above the reserve
    {"sensor.opti_price_level": "EXPENSIVE"},  # precharge lock
    {"input_number.opti_netzlade_spread_ct": 30},  # fixed spread still applies: 24.67 < 30
])
def test_existing_conditions_still_block_w1(overrides):
    assert not precharges(run("W1", overrides=overrides))


# -- missing and invalid inputs (fail-safe) --------------------------------------

STATE, ATTRS = DEFAULT.engine_state()


@pytest.mark.parametrize("raw", [
    pytest.param(None, id="no_model"),
    pytest.param(("unavailable", None), id="unavailable"),
    pytest.param(("unknown", ATTRS), id="unknown"),
    pytest.param(INVALID.engine_state(), id="invalid"),
    pytest.param((STATE, {k: v for k, v in ATTRS.items() if k != "precharge_price_factor"}),
                 id="factor_missing"),
    pytest.param((STATE, {**ATTRS, "precharge_price_factor": 0.5}), id="factor_below_one"),
    pytest.param((STATE, {**ATTRS, "precharge_price_factor": "x"}), id="factor_text"),
    pytest.param((STATE, {k: v for k, v in ATTRS.items() if k != "precharge_price_offset_ct"}),
                 id="offset_missing"),
    pytest.param((STATE, {**ATTRS, "precharge_price_offset_ct": -1}), id="offset_negative"),
])
def test_missing_or_invalid_model_blocks_precharge(raw):
    # evaluate() asserts engine diagnostics "ok" and preview == mode.
    result = run("W1", model=None, raw_model=raw) if raw is not None else run("W1", model=None)
    assert not precharges(result)


def test_invalid_model_keeps_the_reserve():
    result = run("W1", model=INVALID)
    assert not precharges(result)
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == 25.8


def test_negative_price_branch_ignores_a_missing_model():
    prices = day_prices(33, 27, [((17, 18), 45), ((18, 19), 60), ((19, 20), 50)])
    result = evaluate(prices, current=4, model=None, overrides={
        "input_number.opti_einspeiseverguetung_ct": 7, "sensor.opti_forecast_score": 1})
    assert result.decision_id == "negative_price"
    assert result.mode == "Akku Netzladen"


# -- configured models -------------------------------------------------------------

def test_k1_efficient_model_charges_w6_and_w2():
    assert K1.minimum_high_price(30) == pytest.approx(35.94, abs=0.01)
    assert K1.minimum_high_price(38) == pytest.approx(44.80, abs=0.01)
    assert precharges(run("W6", model=K1))
    assert precharges(run("W2", model=K1))


def test_k2_pessimistic_model_needs_a_bigger_gap():
    assert K2.throughput_cost_ct_kwh == pytest.approx(2.34375)
    assert K2.precharge_price_offset_ct == pytest.approx(9.044, abs=1e-3)
    assert K2.minimum_high_price(27) == pytest.approx(46.41, abs=0.01)
    assert precharges(run("W1", model=K2))
    assert not precharges(run("W5", model=K2))


# -- attributes ------------------------------------------------------------------

def test_future_peak_price_is_energy_weighted_across_mixed_rasters():
    # 22:05: today's quarter-hours 22:00-24:00 are 50 ct (8 slots, the running one
    # 22:00-22:15 counts with 10 of 15 minutes), tomorrow hourly 07-09 h at 70 ct.
    # Valley 20 ct elsewhere, fixed load 0.8 kW over the 36 h horizon:
    # energy today = 0.8 * (10/60 + 7 * 0.25) = 1.5333 kWh at 50 ct,
    # energy tomorrow = 0.8 * 2 = 1.6 kWh at 70 ct.
    # (50 * 1.5333 + 70 * 1.6) / 3.1333 = 60.21 ct; the slot mean would be
    # (8 * 50 + 2 * 70) / 10 = 54.0 ct.
    today = [20.0] * 96
    today[88:] = [50.0] * 8
    tomorrow = [20.0] * 24
    tomorrow[7:9] = [70.0, 70.0]
    result = evaluate(today, current=50, hour=22, minute=5, tomorrow=tomorrow,
                      model=DEFAULT.engine_state())
    attrs = reserve(result)
    assert attrs["peak_preis_kuenftig_ct"] == 60.21
    assert attrs["peak_preis_min_kuenftig_ct"] == 50.0


def test_running_peak_slot_counts_with_its_remaining_time():
    # 17:10 inside the 45 ct slot 17:00-17:15: 5/15 of it (0.8 kW * 5 min =
    # 0.0667 kWh), then 3 slots at 45, 4 at 60 and 4 at 50 (0.2 kWh each).
    # (45 * 0.6667 + 60 * 0.8 + 50 * 0.8) / (0.6667 + 1.6) = 118 / 2.2667 = 52.06 ct;
    # counting the whole running slot would give 51.75 ct.
    result = evaluate(day_prices(33, 27, [((17, 18), 45), ((18, 19), 60), ((19, 20), 50)]),
                      current=45, hour=17, minute=10, model=DEFAULT.engine_state())
    assert reserve(result)["peak_preis_kuenftig_ct"] == 52.06
    assert result.decision_id != "peak_precharge"


def test_no_peak_slots_leave_both_attributes_empty():
    result = evaluate([30.0] * 96, current=30, model=DEFAULT.engine_state())
    attrs = reserve(result)
    assert attrs["peak_preis_kuenftig_ct"] is None
    assert attrs["peak_preis_min_kuenftig_ct"] is None


# -- Python side -----------------------------------------------------------------

def test_model_attributes_reproduce_the_minimum_high_price():
    _, attrs = DEFAULT.engine_state()
    assert attrs["precharge_price_factor"] == pytest.approx(1 / 0.81)
    assert attrs["precharge_price_offset_ct"] == pytest.approx(3.0 / 0.9)
    for model in (DEFAULT, K1, K2):
        _, attrs = model.engine_state()
        for cur in (-5, 0, 27, 60):
            assert cur * attrs["precharge_price_factor"] + attrs["precharge_price_offset_ct"] == (
                pytest.approx(model.minimum_high_price(cur)))


def plan_data(result):
    return {"states": result.states, "attributes": result.attributes}


SETTINGS = {"input_boolean.opti_prognose_netzladen": True}


def test_reserve_plan_precharge_diagnosis():
    w1 = precharge_economics(plan_data(run("W1")), SETTINGS, DEFAULT)
    assert w1["status"] == "allowed"
    assert w1["minimum_peak_price_ct"] == 36.667
    assert w1["future_peak_price_ct"] == 51.67 and w1["cheapest_future_peak_price_ct"] == 45.0
    assert w1["current_price_ct"] == 27 and w1["model_source"] == "default"
    assert w1["compares"] == "economic_criterion_only"
    assert precharge_economics(plan_data(run("W2")), SETTINGS, DEFAULT)["status"] == "blocked_price"
    assert precharge_economics(plan_data(run("W1", model=INVALID)), SETTINGS, INVALID)["status"] == (
        "blocked_model")
    off = precharge_economics(plan_data(run("W1")), {"input_boolean.opti_prognose_netzladen": False},
                              DEFAULT)
    assert off["status"] == "not_applicable"
    inactive = plan_data(run("W1"))
    inactive["states"] = {**inactive["states"], "binary_sensor.opti_peak_reserve_aktiv": "off"}
    assert precharge_economics(inactive, SETTINGS, DEFAULT)["status"] == "not_applicable"
    missing = plan_data(evaluate([30.0] * 96, current=30, model=DEFAULT.engine_state()))
    assert precharge_economics(missing, SETTINGS, DEFAULT)["status"] == "not_applicable"


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_python_and_jinja_agree_on_the_economic_criterion(profile):
    result = run(profile)
    assert reserve(result)["peak_preis_kuenftig_ct"] == FUTURE[profile]
    status = precharge_economics(plan_data(result), SETTINGS, DEFAULT)["status"]
    assert (status == "allowed") is precharges(result)


# -- invariants ------------------------------------------------------------------

GRID = [(valley, valley + gap) for valley in (20, 25, 27, 30, 35, 40, 48)
        for gap in range(8, 31, 2)]


@pytest.mark.parametrize(("valley", "peak"), GRID)
def test_monotonic_against_the_old_rule(valley, peak):
    # I1: precharge only if the old fixed spread AND the model threshold allow it.
    base = valley + 4
    result = evaluate(day_prices(base, valley, [((17, 20), peak)]), current=valley,
                      model=DEFAULT.engine_state())
    expected = peak - valley >= 10 and peak >= valley / 0.81 + 3.3333
    assert precharges(result) is expected


def test_repeated_evaluation_is_stable_and_running_precharge_latches():
    # I2: constant inputs give a constant result; switching to Netzladen keeps it.
    first = run("W4")
    assert precharges(first) and precharges(run("W4"))
    assert precharges(run("W4", mode="Akku Netzladen"))


def test_model_entity_name_matches_the_template():
    assert MODEL_ENTITY == "sensor.opti_battery_model"
