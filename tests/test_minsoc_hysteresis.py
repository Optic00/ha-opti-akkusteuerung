"""MinSOC-Eintritt, Freigabe und Ladeprioritäten bei springenden BMS-Werten."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.test_engine import NOW, SOC, StrategyEngine, evaluate, load_resources, measurements
from tests.test_engine_parity import BASE
from custom_components.opti_akku.ev_preparation import apply_preparation

LATCH = "binary_sensor.opti_minsoc_schutz_aktiv"
PREVIEW = "sensor.opti_strategie_vorschau"


def decision_engine(snapshot=None):
    resources = load_resources()
    for block in resources["template_blocks"]:
        for domain in ("sensor", "binary_sensor"):
            block[domain] = [item for item in block.get(domain, [])
                             if f"{domain}.{item['unique_id']}" in (LATCH, PREVIEW)]
    resources["statistics"] = {}
    resources["balancing_automations"] = []
    engine = StrategyEngine(resources)
    if snapshot is not None:
        engine.restore(snapshot)
    return engine


def inputs(soc, **overrides):
    return {**BASE, SOC: soc, "input_boolean.akku_opti_automatik": "on",
            "binary_sensor.opti_peak_reserve_aktiv": "on",
            "sensor.opti_price_level": "VERY_EXPENSIVE", **overrides}


def run(engine, soc, **overrides):
    result = engine.evaluate(inputs(soc, **overrides), {}, NOW)
    assert result.states[PREVIEW] == result.mode
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    return result


def test_reported_bms_jumps_hold_until_exact_release_boundary():
    engine = decision_engine()
    assert run(engine, 11).decision_id == "peak_l1"
    for soc in (9, 11, 9, 11, 12, 12.9):
        result = run(engine, soc)
        assert result.mode == "Akku nur Laden"
        assert result.decision_id == "minimum_soc"
        assert result.reason.startswith("MinSOC")
        if soc > 10:
            assert result.reason == "MinSOC-Schutz (Halten bis 13%)"
    assert run(engine, 13).decision_id == "peak_l1"


@pytest.mark.parametrize("previous", ["Akku nur Laden", "Akku Pause", "Akku Netzladen"])
def test_other_previous_modes_do_not_activate_minsoc(previous):
    result = run(decision_engine(), 11, **{"input_select.akkusteuerung_modus": previous})
    assert result.decision_id == "peak_l1"
    assert result.states[LATCH] == "off"


def test_latch_survives_restore_with_reset_coordinator_mode_and_sensor_gap():
    engine = StrategyEngine()
    before = StrategyEngine()
    evaluate(before, measurements(**{SOC: 9, "input_number.minsoc": 10}))
    engine.restore(json.loads(json.dumps(before.snapshot())))
    result = evaluate(engine, measurements(**{SOC: "unavailable", "input_number.minsoc": 10}),
                      now=NOW + dt.timedelta(seconds=15))
    assert result.mode == "Akku Pause"
    assert result.states[LATCH] == "on"
    result = evaluate(engine, measurements(**{SOC: 11, "input_number.minsoc": 10,
                                            "input_select.akkusteuerung_modus": "Akku Pause"}),
                      now=NOW + dt.timedelta(seconds=30))
    assert result.decision_id == "minimum_soc"


@pytest.mark.parametrize("new_minimum", [5, 9, 12])
def test_changed_minimum_recomputes_protection(new_minimum):
    engine = decision_engine()
    run(engine, 9)
    result = run(engine, 11, **{"input_number.minsoc": new_minimum})
    assert result.decision_id == ("minimum_soc" if new_minimum >= 11 else "peak_l1")


def test_band_is_limited_by_maximum_and_fractional_soc_is_supported():
    engine = decision_engine()
    limits = {"input_number.minsoc": 94, "input_number.maxsoc": 95}
    run(engine, 94, **limits)
    held = run(engine, 94.49, **limits)
    assert held.decision_id == "minimum_soc"
    assert held.reason == "MinSOC-Schutz (Halten bis 94.5%)"
    assert run(engine, 94.5, **limits).states[LATCH] == "off"
    assert run(engine, 95, **limits).states[LATCH] == "off"


@pytest.mark.parametrize("soc,changed_limit", [
    (11, {"input_number.maxsoc": 90}),
    (12, {"input_number.minsoc": 11}),
])
def test_limits_changed_above_new_floor_reset_latch(soc, changed_limit):
    engine = decision_engine()
    run(engine, 9)
    result = run(engine, soc, **changed_limit)
    assert result.states[LATCH] == "off"
    assert result.decision_id == "peak_l1"


def test_held_minsoc_keeps_priority_over_ev_preparation_and_pv_balancing():
    engine = decision_engine()
    run(engine, 9)
    result = run(engine, 11, **{"sensor.opti_balancing_watchdog": "pv"})
    assert result.decision_id == "minimum_soc"
    after, _ = apply_preparation(result, {"charging_guard": True})
    assert after is result
    assert after.mode == "Akku nur Laden"


@pytest.mark.parametrize("charging", ["negative_price", "peak_precharge", "balancing_grid"])
def test_held_band_does_not_block_authorized_grid_charging(charging):
    engine = decision_engine()
    run(engine, 9)
    overrides = {}
    attributes = {}
    if charging == "negative_price":
        overrides = {"sensor.opti_price_current_ct_kwh": 3, "sensor.opti_forecast_score": 1}
    elif charging == "peak_precharge":
        overrides = {"sensor.opti_peak_reserve_soc": 35, "sensor.opti_price_current_ct_kwh": 50}
        attributes = {"sensor.opti_peak_reserve_soc": {
            "reserve_ve_soc": 25, "min_preis_vor_peak_ct": 50, "peak_preis_avg_ct": 200}}
    else:
        overrides = {"sensor.opti_balancing_watchdog": "netz"}
    result = engine.evaluate(inputs(11, **overrides), attributes, NOW)
    assert result.decision_id == charging
    assert result.mode == "Akku Netzladen"
    assert result.states[PREVIEW] == result.mode
    if charging == "balancing_grid":
        assert result.reason == "Balancing-Watchdog (Netz-Vollladung)"
    # Issue #154: at or below the floor, price-driven grid charging still wins
    # because it never discharges. Grid balancing keeps the floor's priority.
    for soc in (10, 0):
        result = engine.evaluate(inputs(soc, **overrides), attributes, NOW)
        assert result.states[PREVIEW] == result.mode
        if charging == "balancing_grid":
            assert result.decision_id == "minimum_soc"
            assert result.mode == "Akku nur Laden"
            assert result.reason == "MinSOC-Schutz (SoC<=10.0)"
        else:
            assert result.decision_id == charging
            assert result.mode == "Akku Netzladen"
            assert not result.reason.startswith("MinSOC")


def test_master_off_still_pauses_with_active_protection():
    engine = decision_engine()
    run(engine, 9)
    result = engine.evaluate(inputs(11, **{"input_boolean.akku_opti_automatik": "off"}), {}, NOW)
    assert result.mode == "Akku Pause"


@pytest.mark.parametrize("minimum", [0, 5, 10])
def test_all_modes_at_or_below_floor_remain_discharge_locked(minimum):
    for soc in (minimum, max(0, minimum - 1)):
        for previous in ("Akku Dynamisch", "Akku nur Entladen", "Akku schnell Entladen"):
            result = run(decision_engine(), soc, **{
                "input_number.minsoc": minimum, "input_select.akkusteuerung_modus": previous})
            assert result.mode == "Akku nur Laden"
            assert result.decision_id == "minimum_soc"


@pytest.mark.parametrize("price,expected,mode", [
    (37.31, "peak_precharge", "Akku Netzladen"),
    (37.85, "minimum_soc", "Akku nur Laden"),  # spread met, outside the window
])
def test_empty_battery_precharges_before_morning_peak(price, expected, mode):
    """Issue #154: empty battery overnight, cheap window before a morning peak."""
    states = {"input_number.minsoc": 5, "input_select.akkusteuerung_modus": "Akku nur Laden",
              "input_boolean.opti_prognose_netzladen": "on",
              "sensor.opti_price_level": "NORMAL", "sensor.opti_forecast_score": 10,
              "sensor.opti_peak_reserve_soc": 39.7, "sensor.opti_price_current_ct_kwh": price}
    attributes = {"sensor.opti_peak_reserve_soc": {
        "reserve_ve_soc": 36.2, "min_preis_vor_peak_ct": 37.31, "peak_preis_avg_ct": 48.0}}
    for soc in (5, 4):
        result = decision_engine().evaluate(inputs(soc, **states), attributes, NOW)
        assert result.decision_id == expected
        assert result.mode == result.states[PREVIEW] == mode


CELL = "sensor.opti_cell_voltage_min_v"
RESERVE = "input_number.opti_zellspannung_reserve_v"


def test_cell_voltage_reserve_triggers_and_holds_through_relaxation():
    engine = decision_engine()
    result = run(engine, 12, **{RESERVE: 3.0, CELL: 2.99})
    assert result.decision_id == "minimum_soc"
    assert result.mode == "Akku nur Laden"
    assert result.reason.startswith("Zellspannungs-Reserve")
    assert result.reason == "Zellspannungs-Reserve (Zelle 2.99 V, Halten bis 3.2 V)"
    attributes = result.attributes[LATCH]
    assert attributes["ausloeser"] == "zellspannung"
    assert attributes["reserve_v"] == 3.0
    assert attributes["release_zell_v"] == 3.2
    # Relaxation in rest: voltage recovers, latch holds until reserve + 0.20 V
    for volt in (3.10, 3.19):
        held = run(engine, 40, **{RESERVE: 3.0, CELL: volt})
        assert held.decision_id == "minimum_soc"
        assert held.states[LATCH] == "on"
    released = run(engine, 40, **{RESERVE: 3.0, CELL: 3.20})
    assert released.states[LATCH] == "off"
    assert released.decision_id != "minimum_soc"
    assert released.attributes[LATCH]["ausloeser"] == "aus"


def test_cell_voltage_hold_requires_prior_trigger():
    result = run(decision_engine(), 40, **{RESERVE: 3.0, CELL: 3.10})
    assert result.states[LATCH] == "off"


def test_cell_voltage_missing_source_keeps_legacy_behaviour():
    engine = decision_engine()
    assert run(engine, 12, **{RESERVE: 3.0, CELL: "unavailable"}).states[LATCH] == "off"
    run(engine, 12, **{RESERVE: 3.0, CELL: 2.9})
    # source disappears while held: only the SoC logic decides, fail-open
    result = run(engine, 40, **{RESERVE: 3.0, CELL: "unavailable"})
    assert result.states[LATCH] == "off"
    run(engine, 9)
    result = run(engine, 11, **{RESERVE: 3.0, CELL: "unavailable"})
    assert result.decision_id == "minimum_soc"
    assert result.reason == "MinSOC-Schutz (Halten bis 13%)"
    assert result.attributes[LATCH]["ausloeser"] == "soc"


def test_cell_voltage_with_reserve_zero_matches_legacy():
    engine = decision_engine()
    result = run(engine, 40, **{RESERVE: 0, CELL: 2.5})
    assert result.states[LATCH] == "off"
    assert result.attributes[LATCH]["ausloeser"] == "aus"
    assert result.attributes[LATCH]["release_zell_v"] is None
    run(engine, 9, **{RESERVE: 0, CELL: 2.5})
    result = run(engine, 11, **{RESERVE: 0, CELL: 2.5})
    assert result.reason == "MinSOC-Schutz (Halten bis 13%)"


def test_changed_reserve_ends_cell_voltage_hold():
    engine = decision_engine()
    run(engine, 40, **{RESERVE: 3.0, CELL: 2.95})
    assert run(engine, 40, **{RESERVE: 3.0, CELL: 3.1}).states[LATCH] == "on"
    result = run(engine, 40, **{RESERVE: 3.05, CELL: 3.1})
    assert result.states[LATCH] == "off"
    # a new reserve above the cell voltage triggers afresh
    assert run(engine, 40, **{RESERVE: 3.2, CELL: 3.1}).states[LATCH] == "on"


def test_cell_voltage_latch_yields_to_balancing_grid_charging():
    engine = decision_engine()
    run(engine, 40, **{RESERVE: 3.0, CELL: 2.95})
    result = run(engine, 40, **{RESERVE: 3.0, CELL: 3.1, "sensor.opti_balancing_watchdog": "netz"})
    assert result.decision_id == "balancing_grid"
    assert result.mode == "Akku Netzladen"
