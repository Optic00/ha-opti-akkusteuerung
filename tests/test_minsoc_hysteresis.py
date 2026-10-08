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
    # The hard floor keeps its existing priority over these charge requests.
    result = engine.evaluate(inputs(10, **overrides), attributes, NOW)
    assert result.decision_id == "minimum_soc"


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
