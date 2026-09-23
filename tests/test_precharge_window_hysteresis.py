"""Issue #111: the pre-peak charging window needs hysteresis.

Quarter-hour prices fluctuate by 0.3-1 ct around min_preis_vor_peak_ct + 0.5.
Without a hold band the mode flipped Netzladen <-> L4 every quarter hour
(live night 2026-09-22/23: 01:04 L4, 01:45 Vorladen, 02:15 L4, 02:45 Vorladen).
Entry stays at +0.5 ct; once in 'Akku Netzladen' the window holds until +1.5 ct.
The real action tree (decisions.yaml) and the preview (templates.yaml) must agree.
"""
from __future__ import annotations

import pytest

from tests.test_engine_parity import BASE, NOW, StrategyEngine, load_resources

MODE = "input_select.akkusteuerung_modus"
PRICE = "sensor.opti_price_current_ct_kwh"
PREVIEW = "sensor.opti_strategie_vorschau"
MIN_BEFORE_PEAK = 36.72
# Live quarter-hour sequence from the issue, then a clear exit and a re-test inside the band.
ISSUE_PRICES = [37.98, 37.17, 37.02, 37.66, 37.12, 36.72, 37.45]

PEAK_STATES = {
    **BASE,
    "input_boolean.akku_opti_automatik": "on",
    "sensor.opti_soc": "25",
    "sensor.opti_price_level": "NORMAL",
    "sensor.opti_peak_reserve_soc": "32.8",
    "binary_sensor.opti_peak_reserve_aktiv": "on",
}
PEAK_ATTRS = {
    "sensor.opti_peak_reserve_soc": {
        "reserve_ve_soc": 20.0,
        "min_preis_vor_peak_ct": MIN_BEFORE_PEAK,
        "peak_preis_avg_ct": 60.0,
        "peak_preis_ve_avg_ct": 60.0,
    }
}


@pytest.fixture
def engine():
    resource = load_resources()
    preview = next(
        definition for block in resource["template_blocks"]
        for definition in block.get("sensor", [])
        if definition["unique_id"] == "opti_strategie_vorschau"
    )
    resource["template_blocks"] = [{"sensor": [preview]}]
    resource["statistics"] = []
    resource["balancing_automations"] = []
    return StrategyEngine(resource)


def run(engine, states, attrs, mode, price, **changes):
    result = engine.evaluate({**states, MODE: mode, PRICE: str(price), **changes}, attrs, NOW)
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    # Action tree and preview must take the same hysteresis decision.
    assert result.mode == result.states[PREVIEW]
    return result


def test_issue_sequence_holds_precharge_inside_band(engine):
    mode = "Akku Dynamisch"
    seen = []
    for price in ISSUE_PRICES:
        result = run(engine, PEAK_STATES, PEAK_ATTRS, mode, price)
        seen.append((price, result.decision_id))
        mode = result.mode
    # First slot is above the entry edge (36.72 + 0.5 = 37.22): reserve is held via L4.
    assert seen[0] == (37.98, "peak_l4")
    # 37.17 enters the window; every later slot stays below the 38.22 hold edge.
    assert seen[1] == (37.17, "peak_precharge")
    assert [decision for _, decision in seen[2:]] == ["peak_precharge"] * 5
    assert result.mode == "Akku Netzladen"
    assert result.reason.startswith("Peak-Vorladen")


def test_price_above_hold_band_leaves_precharge(engine):
    held = run(engine, PEAK_STATES, PEAK_ATTRS, "Akku Netzladen", MIN_BEFORE_PEAK + 1.5)
    assert held.decision_id == "peak_precharge"
    left = run(engine, PEAK_STATES, PEAK_ATTRS, "Akku Netzladen", MIN_BEFORE_PEAK + 1.51)
    assert left.decision_id == "peak_l4"
    assert left.mode == "Akku nur Laden"
    # Once out, a price inside the band but above the entry edge does not re-enter.
    again = run(engine, PEAK_STATES, PEAK_ATTRS, left.mode, 37.45)
    assert again.decision_id == "peak_l4"


@pytest.mark.parametrize("mode", ["Akku Dynamisch", "Akku nur Laden", "Akku Pause", "Akku nur Entladen"])
def test_entry_edge_stays_at_half_cent_without_prior_grid_charging(engine, mode):
    inside = run(engine, PEAK_STATES, PEAK_ATTRS, mode, MIN_BEFORE_PEAK + 0.5)
    assert inside.decision_id == "peak_precharge"
    outside = run(engine, PEAK_STATES, PEAK_ATTRS, mode, 37.66)
    assert outside.decision_id == "peak_l4"
    assert outside.mode == "Akku nur Laden"


def test_missing_window_minimum_keeps_charging_regardless_of_mode(engine):
    attrs = {"sensor.opti_peak_reserve_soc": {**PEAK_ATTRS["sensor.opti_peak_reserve_soc"], "min_preis_vor_peak_ct": None}}
    for mode in ("Akku Dynamisch", "Akku Netzladen"):
        assert run(engine, PEAK_STATES, attrs, mode, 45.0).decision_id == "peak_precharge"


@pytest.mark.parametrize("level, expected_id, expected_mode", [
    ("VERY_EXPENSIVE", "peak_l1", "Akku nur Entladen"),
    ("EXPENSIVE", "peak_l2", "Akku nur Entladen"),
])
def test_expensive_slot_still_discharges_after_precharge(engine, level, expected_mode, expected_id):
    # SoC above the VE reserve so L2 may discharge; spread collapses at the peak price.
    states = {**PEAK_STATES, "sensor.opti_soc": "31", "sensor.opti_price_level": level}
    result = run(engine, states, PEAK_ATTRS, "Akku Netzladen", 60.0)
    assert result.decision_id == expected_id
    assert result.mode == expected_mode


def test_negative_price_window_uses_same_hysteresis(engine):
    states = {
        **BASE,
        "input_boolean.akku_opti_automatik": "on",
        "sensor.opti_forecast_score": "1",
        "input_number.opti_einspeiseverguetung_ct": "8",
    }
    attrs = {"sensor.opti_peak_reserve_soc": {"min_preis_vor_peak_ct": 3.0}}
    assert run(engine, states, attrs, "Akku Dynamisch", 3.5).decision_id == "negative_price"
    assert run(engine, states, attrs, "Akku Dynamisch", 4.2).decision_id != "negative_price"
    assert run(engine, states, attrs, "Akku Netzladen", 4.2).decision_id == "negative_price"
    assert run(engine, states, attrs, "Akku Netzladen", 4.6).decision_id != "negative_price"
