"""The former SOC<20 and SOC<75 forecast branches were fully covered by SOC<80.

The removed branches are rebuilt from the remaining ``reserve_winter`` branch
exactly as they stood before (same gates, only the SoC bound and, for SOC<75,
the missing winter gate and the price set differ). Both action trees then run
through the real engine over the same state grid; the selected mode must never
differ.
"""
from __future__ import annotations

import copy
import itertools

import pytest

from tests.test_engine_parity import BASE, NOW, StrategyEngine, load_resources

SOC = (3, 14, 15, 19.9, 20, 44, 45, 74.9, 75, 79.9, 80, 90)
SCORES = ("1", "2.9", "3", "unavailable")
WINTER_GATE = "binary_sensor.opti_winter_charging_allowed"
PRICES = ("VERY_CHEAP", "CHEAP", "NORMAL", "EXPENSIVE", "VERY_EXPENSIVE", "unavailable")
SWITCH = ("on", "off")


def _decision_only(resource):
    preview = next(
        definition for block in resource["template_blocks"]
        for definition in block.get("sensor", [])
        if definition["unique_id"] == "opti_strategie_vorschau"
    )
    # Keep the real winter gate template: the overlap proof depends on it.
    winter_gate = next(
        definition for block in resource["template_blocks"]
        for definition in block.get("binary_sensor", [])
        if definition["unique_id"] == "opti_winter_charging_allowed"
    )
    resource["template_blocks"] = [{"binary_sensor": [winter_gate]}, {"sensor": [preview]}]
    resource["statistics"] = []
    resource["balancing_automations"] = []
    return resource


def _choose(resource):
    return resource["strategy"]["actions"][0]["choose"]


def _branch(choose, decision_id):
    return next(b for b in choose if b["sequence"][0]["decision_id"] == decision_id)


def _with_soc_bound(branch, bound):
    for condition in branch["conditions"]:
        if condition.get("condition") == "numeric_state" and condition.get("entity_id") == "sensor.opti_soc":
            condition["below"] = bound
            return
    raise AssertionError("SoC condition missing")


def _former_branches(winter):
    low = copy.deepcopy(winter)
    _with_soc_bound(low, 20)
    low["sequence"][0]["decision_id"] = "reserve_low"

    forecast = copy.deepcopy(winter)
    forecast["conditions"] = [
        c for c in forecast["conditions"]
        if c.get("entity_id") != WINTER_GATE
    ]
    _with_soc_bound(forecast, 75)
    price = next(c for c in forecast["conditions"] if c.get("condition") == "or")
    price["conditions"] = [c for c in price["conditions"] if c.get("state") != "EXPENSIVE"]
    assert [c["state"] for c in price["conditions"]] == ["VERY_CHEAP", "CHEAP", "NORMAL"]
    forecast["sequence"][0]["decision_id"] = "reserve_forecast"
    return low, forecast


@pytest.fixture(scope="module")
def engines():
    current = _decision_only(load_resources())
    former = copy.deepcopy(current)
    choose = _choose(former)
    winter = _branch(choose, "reserve_winter")
    index = choose.index(winter)
    choose[index:index] = list(_former_branches(winter))
    return StrategyEngine(current), StrategyEngine(former)


def _states(soc, today, tomorrow, price, switch, previous):
    base = {key: value for key, value in BASE.items() if key != WINTER_GATE}
    return {
        **base,
        "input_boolean.akku_opti_automatik": "on",
        "sensor.opti_soc": str(soc),
        "sensor.opti_forecast_score": today,
        "sensor.opti_forecast_score_tomorrow": tomorrow,
        "sensor.opti_price_level": price,
        "input_boolean.opti_prognose_netzladen": switch,
        "input_select.akkusteuerung_modus": previous,
    }


def test_removed_branches_never_change_the_mode(engines):
    current, former = engines
    renamed = 0
    for soc, today, tomorrow, price, switch, previous in itertools.product(
        SOC, SCORES, SCORES, PRICES, SWITCH, ("Akku Dynamisch", "Akku nur Laden")
    ):
        states = _states(soc, today, tomorrow, price, switch, previous)
        new = current.evaluate(states, {}, NOW)
        old = former.evaluate(states, {}, NOW)
        case = (soc, today, tomorrow, price, switch, previous)
        assert new.mode == old.mode, case
        assert new.states["sensor.opti_strategie_vorschau"] == new.mode, case
        assert new.states[WINTER_GATE] == "on", case
        if old.decision_id in ("reserve_low", "reserve_forecast"):
            assert new.decision_id == "reserve_winter", case
            renamed += 1
        else:
            assert new.decision_id == old.decision_id, case
    assert renamed > 0  # The grid really reaches the removed branches.


@pytest.mark.parametrize(
    "soc,today,tomorrow,price,expected",
    [
        (18, "1", "1", "NORMAL", "reserve_winter"),
        (50, "1", "1", "NORMAL", "reserve_winter"),
        (78, "1", "1", "EXPENSIVE", "reserve_winter"),
        (50, "1", "1", "EXPENSIVE", "reserve_winter"),
        (12, "1", "5", "NORMAL", "reserve_today_low"),
    ],
)
def test_forecast_reserve_cases(engines, soc, today, tomorrow, price, expected):
    result = engines[0].evaluate(_states(soc, today, tomorrow, price, "on", "Akku Dynamisch"), {}, NOW)
    assert result.mode == "Akku nur Laden"
    assert result.decision_id == expected
    if expected == "reserve_winter":
        assert "SOC<80 Wintermodus" in result.reason


@pytest.mark.parametrize("soc,price", [(50, "VERY_EXPENSIVE"), (80, "NORMAL")])
def test_forecast_reserve_bounds(engines, soc, price):
    result = engines[0].evaluate(_states(soc, "1", "1", price, "on", "Akku Dynamisch"), {}, NOW)
    assert result.decision_id != "reserve_winter"


def test_strategy_has_no_trace_of_the_removed_branches():
    resource = load_resources()
    ids = [b["sequence"][0]["decision_id"] for b in _choose(resource)]
    assert "reserve_low" not in ids and "reserve_forecast" not in ids
    preview = next(
        definition for block in resource["template_blocks"]
        for definition in block.get("sensor", [])
        if definition["unique_id"] == "opti_strategie_vorschau"
    )
    text = repr(preview)
    for needle in ("SOC<20", "SOC<75", "soc < 20", "soc < 75"):
        assert needle not in text


def test_overlap_depends_on_the_winter_gate(engines):
    """Guard: with the gate off the removed SOC<75 branch would have mattered."""
    current, former = engines
    states = {**_states(50, "1", "1", "NORMAL", "on", "Akku Dynamisch"), WINTER_GATE: "off"}
    assert former.evaluate(states, {}, NOW).decision_id == "reserve_forecast"
    assert current.evaluate(states, {}, NOW).mode != "Akku nur Laden"
