"""MinSOC-Eintritt, Freigabe und Ladeprioritäten bei springenden BMS-Werten."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.test_engine import NOW, SOC, StrategyEngine, evaluate, load_resources, measurements
from tests.battery_model_fixture import model_attributes, model_states
from tests.test_engine_parity import BASE
from custom_components.opti_akku.ev_preparation import apply_preparation

LATCH = "binary_sensor.opti_minsoc_schutz_aktiv"
PREVIEW = "sensor.opti_strategie_vorschau"
DEBOUNCE = "binary_sensor.opti_zellspannung_unter_reserve"


def decision_engine(snapshot=None):
    resources = load_resources()
    for block in resources["template_blocks"]:
        for domain in ("sensor", "binary_sensor"):
            block[domain] = [item for item in block.get(domain, [])
                             if f"{domain}.{item['unique_id']}" in (LATCH, PREVIEW, DEBOUNCE)]
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


def run(engine, soc, now=NOW, **overrides):
    result = engine.evaluate(inputs(soc, **overrides), {}, now)
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
        # Peak-Vorladen ist bei EXPENSIVE/VERY_EXPENSIVE gesperrt; der Vorrang vor
        # dem MinSOC-Halteband gilt für einen zulässigen, also nicht teuren Slot.
        overrides = {"sensor.opti_peak_reserve_soc": 35, "sensor.opti_price_current_ct_kwh": 50,
                     "sensor.opti_price_level": "NORMAL", **model_states()}
        attributes = {"sensor.opti_peak_reserve_soc": {
            "reserve_ve_soc": 25, "min_preis_vor_peak_ct": 50, "peak_preis_avg_ct": 200,
            "peak_preis_kuenftig_ct": 200}, **model_attributes()}
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
              "sensor.opti_peak_reserve_soc": 39.7, "sensor.opti_price_current_ct_kwh": price,
              **model_states()}
    # The future peak price clears the default model's threshold at both prices,
    # p_min(37.31) = 49.40 and p_min(37.85) = 50.06, so only the charging window
    # separates the two cases. With 48 ct the economic rule would block
    # (test_issue_154_peak_is_no_longer_economic).
    attributes = {"sensor.opti_peak_reserve_soc": {
        "reserve_ve_soc": 36.2, "min_preis_vor_peak_ct": 37.31, "peak_preis_avg_ct": 48.0,
        "peak_preis_kuenftig_ct": 51.0}, **model_attributes()}
    for soc in (5, 4):
        result = decision_engine().evaluate(inputs(soc, **states), attributes, NOW)
        assert result.decision_id == expected
        assert result.mode == result.states[PREVIEW] == mode


def test_issue_154_peak_is_no_longer_economic():
    """With 48 ct ahead, charging at 37.31 ct does not cover losses and wear (A3/B2)."""
    states = {"input_number.minsoc": 5, "input_select.akkusteuerung_modus": "Akku nur Laden",
              "input_boolean.opti_prognose_netzladen": "on",
              "sensor.opti_price_level": "NORMAL", "sensor.opti_forecast_score": 10,
              "sensor.opti_peak_reserve_soc": 39.7, "sensor.opti_price_current_ct_kwh": 37.31,
              **model_states()}
    attributes = {"sensor.opti_peak_reserve_soc": {
        "reserve_ve_soc": 36.2, "min_preis_vor_peak_ct": 37.31, "peak_preis_avg_ct": 48.0,
        "peak_preis_kuenftig_ct": 48.0}, **model_attributes()}
    result = decision_engine().evaluate(inputs(5, **states), attributes, NOW)
    assert result.decision_id == "minimum_soc"
    assert result.mode == result.states[PREVIEW] == "Akku nur Laden"


CELL = "sensor.opti_cell_voltage_min_v"
RESERVE = "input_number.opti_zellspannung_reserve_v"
LATER = NOW + dt.timedelta(seconds=60)


def cell_run(engine, soc, volt, reserve=3.0, at=LATER, **overrides):
    return run(engine, soc, now=at, **{RESERVE: reserve, CELL: volt}, **overrides)


def trigger_cell(engine, soc=12, volt=2.99, reserve=3.0):
    """Entry needs the debounced sensor: below the reserve for 60 s."""
    first = run(engine, soc, now=NOW, **{RESERVE: reserve, CELL: volt})
    assert first.states[LATCH] == "off"
    return cell_run(engine, soc, volt, reserve)


def test_cell_voltage_reserve_triggers_and_holds_through_relaxation():
    engine = decision_engine()
    result = trigger_cell(engine)
    assert result.decision_id == "minimum_soc"
    assert result.mode == "Akku nur Laden"
    assert result.reason == "Zellspannungs-Reserve (Zelle 2.99 V, Halten bis 3.2 V)"
    attributes = result.attributes[LATCH]
    assert (attributes["ausloeser"], attributes["reserve_v"], attributes["release_zell_v"]) == ("zellspannung", 3.0, 3.2)
    # Swap case: voltage recovers while the SoC (12 %) is above minsoc but inside the SoC band
    for volt in (3.10, 3.19):
        held = cell_run(engine, 12, volt)
        assert held.states[LATCH] == "on"
        assert held.decision_id == "minimum_soc"
        assert held.attributes[LATCH]["ausloeser"] == "zellspannung"
        assert held.attributes[LATCH]["soc_aktiv"] is False
    released = cell_run(engine, 12, 3.20)
    assert released.states[LATCH] == "off"
    assert released.attributes[LATCH]["ausloeser"] == "aus"


def test_cell_hold_does_not_inherit_soc_hysteresis_nor_soc_hold_the_cell_origin():
    engine = decision_engine()
    trigger_cell(engine)
    # reserve switched off at SoC 12: no SoC hysteresis applies, protection ends
    assert cell_run(engine, 12, 3.1, reserve=0).states[LATCH] == "off"
    # SoC trigger while the cell hold runs keeps both origins apart
    engine = decision_engine()
    trigger_cell(engine, soc=40)
    both = cell_run(engine, 9, 3.1)
    assert both.attributes[LATCH]["ausloeser"] == "beide"
    assert both.reason.startswith("MinSOC-Schutz")
    after = cell_run(engine, 40, 3.1)
    assert after.attributes[LATCH]["ausloeser"] == "zellspannung"
    assert after.states[LATCH] == "on"


def test_simultaneous_trigger_reports_both_and_releases_independently():
    engine = decision_engine()
    run(engine, 9, now=NOW, **{RESERVE: 3.0, CELL: 2.9})
    both = cell_run(engine, 9, 2.9)
    assert both.attributes[LATCH]["ausloeser"] == "beide"
    soc_only = cell_run(engine, 11, 3.25)
    assert soc_only.attributes[LATCH]["ausloeser"] == "soc"
    assert soc_only.reason == "MinSOC-Schutz (Halten bis 13%)"


def test_cell_hold_survives_maxsoc_change_and_bounds_at_release():
    engine = decision_engine()
    trigger_cell(engine, soc=40)
    assert cell_run(engine, 40, 3.1, **{"input_number.maxsoc": 90}).states[LATCH] == "on"
    # exactly reserve + 0.20 V (3.30 V at reserve 3.10) releases; float noise must not hold it
    engine = decision_engine()
    trigger_cell(engine, soc=40, volt=3.05, reserve=3.1)
    assert cell_run(engine, 40, 3.29, reserve=3.1).states[LATCH] == "on"
    result = cell_run(engine, 40, 3.30, reserve=3.1)
    assert result.states[LATCH] == "off"
    assert result.attributes[LATCH]["release_zell_v"] == 3.3


def test_load_spike_shorter_than_debounce_does_not_trigger():
    engine = decision_engine()
    run(engine, 40, now=NOW, **{RESERVE: 3.0, CELL: 2.95})
    spike_over = cell_run(engine, 40, 3.2, at=NOW + dt.timedelta(seconds=30))
    assert spike_over.states[LATCH] == "off"
    again = cell_run(engine, 40, 3.2, at=NOW + dt.timedelta(seconds=90))
    assert again.states[LATCH] == "off"
    # a new dip must again persist for 60 s
    assert cell_run(engine, 40, 2.95, at=NOW + dt.timedelta(seconds=100)).states[LATCH] == "off"
    assert cell_run(engine, 40, 2.95, at=NOW + dt.timedelta(seconds=161)).states[LATCH] == "on"


def test_lowered_reserve_ends_cell_hold_and_new_dip_needs_debounce():
    engine = decision_engine()
    trigger_cell(engine, soc=40)
    assert cell_run(engine, 40, 3.1).states[LATCH] == "on"
    assert cell_run(engine, 40, 3.1, reserve=2.95).states[LATCH] == "off"
    for seconds in (1, 30):
        assert cell_run(engine, 40, 2.94, reserve=2.95,
                        at=LATER + dt.timedelta(seconds=seconds)).states[LATCH] == "off"
    assert cell_run(engine, 40, 2.94, reserve=2.95, at=LATER + dt.timedelta(seconds=61)).states[LATCH] == "on"


def test_raised_reserve_keeps_cell_hold_without_new_debounce():
    engine = decision_engine()
    trigger_cell(engine, soc=40, volt=2.99, reserve=3.0)
    assert cell_run(engine, 40, 3.05).states[LATCH] == "on"
    for seconds in (1, 30):
        held = cell_run(engine, 40, 3.05, reserve=3.10, at=LATER + dt.timedelta(seconds=seconds))
        assert held.states[LATCH] == "on"
        assert held.states[DEBOUNCE] == "off"
        assert held.decision_id == "minimum_soc"
        assert held.attributes[LATCH]["zell_aktiv"] is True
        assert held.attributes[LATCH]["soc_aktiv"] is False
        assert held.attributes[LATCH]["ausloeser"] == "zellspannung"
        assert held.attributes[LATCH]["release_zell_v"] == 3.30
    assert cell_run(engine, 40, 3.25, reserve=3.10, at=LATER + dt.timedelta(seconds=31)).states[LATCH] == "on"
    released = cell_run(engine, 40, 3.30, reserve=3.10, at=LATER + dt.timedelta(seconds=32))
    assert released.states[LATCH] == "off"
    assert released.attributes[LATCH]["zell_aktiv"] is False
    assert released.attributes[LATCH]["ausloeser"] == "aus"


@pytest.mark.parametrize("previous,reserve", [(3030 * 0.001, 3.03), (3.03, 3030 * 0.001)])
def test_cell_hold_compares_reserves_rounded_to_millivolts(previous, reserve):
    engine = decision_engine()
    trigger_cell(engine, soc=40, reserve=previous)
    held = cell_run(engine, 40, 3.10, reserve=reserve)
    assert held.states[LATCH] == "on"
    assert held.attributes[LATCH]["ausloeser"] == "zellspannung"


def test_source_outage_without_prior_hold_never_triggers():
    engine = decision_engine()
    assert run(engine, 12, now=NOW, **{RESERVE: 3.0, CELL: "unavailable"}).states[LATCH] == "off"
    assert cell_run(engine, 12, "unavailable").states[LATCH] == "off"


def test_source_outage_keeps_cell_hold_until_valid_release_or_reserve_change():
    engine = decision_engine()
    trigger_cell(engine, soc=40)
    assert cell_run(engine, 40, "unavailable").states[LATCH] == "on"
    assert cell_run(engine, 40, "unavailable").attributes[LATCH]["ausloeser"] == "zellspannung"
    assert cell_run(engine, 40, "unavailable").reason == "Zellspannungs-Reserve (Zelle n/v V, Halten bis 3.2 V)"
    assert cell_run(engine, 40, 3.1).states[LATCH] == "on"
    assert cell_run(engine, 40, 3.2).states[LATCH] == "off"
    # reserve 0 during an outage frees the protection
    engine = decision_engine()
    trigger_cell(engine, soc=40)
    assert cell_run(engine, 40, "unavailable").states[LATCH] == "on"
    assert cell_run(engine, 40, "unavailable", reserve=0).states[LATCH] == "off"
    # raising the reserve during an outage keeps the hold; lowering ends it
    engine = decision_engine()
    trigger_cell(engine, soc=40)
    raised = cell_run(engine, 40, "unavailable", reserve=3.05)
    assert raised.states[LATCH] == "on"
    assert raised.attributes[LATCH]["ausloeser"] == "zellspannung"
    assert cell_run(engine, 40, "unavailable", reserve=2.95).states[LATCH] == "off"
    # no re-entry without a valid value
    assert cell_run(engine, 40, "unavailable", reserve=2.95,
                    at=LATER + dt.timedelta(seconds=60)).states[LATCH] == "off"


def test_reserve_zero_keeps_legacy_behaviour():
    engine = decision_engine()
    result = cell_run(engine, 40, 2.5, reserve=0)
    assert result.states[LATCH] == "off"
    assert result.attributes[LATCH]["ausloeser"] == "aus"
    assert result.attributes[LATCH]["release_zell_v"] is None
    run(engine, 9, **{RESERVE: 0, CELL: 2.5})
    assert run(engine, 11, **{RESERVE: 0, CELL: 2.5}).reason == "MinSOC-Schutz (Halten bis 13%)"


def test_legacy_latch_without_new_attributes_keeps_soc_hold():
    engine = decision_engine()
    run(engine, 9)
    snapshot = json.loads(json.dumps(engine.snapshot()))
    for name in ("soc_aktiv", "zell_aktiv", "reserve_v", "ausloeser"):
        snapshot["attributes"][LATCH].pop(name, None)
    restored = decision_engine(snapshot)
    result = run(restored, 11)
    assert result.states[LATCH] == "on"
    assert result.attributes[LATCH]["ausloeser"] == "soc"
    assert run(restored, 13).states[LATCH] == "off"


def test_cell_voltage_latch_yields_to_balancing_grid_charging():
    engine = decision_engine()
    trigger_cell(engine, soc=40)
    result = cell_run(engine, 40, 3.1, **{"sensor.opti_balancing_watchdog": "netz"})
    assert result.decision_id == "balancing_grid"
    assert result.mode == "Akku Netzladen"


def test_millivolt_normalisation_artefact_meets_exact_reserve():
    # 3030 mV * 0.001 ergibt 3.0300000000000002 V; die Reserve 3,03 V muss greifen.
    engine = decision_engine()
    run(engine, 40, now=NOW, **{RESERVE: 3.03, CELL: 3030 * 0.001})
    result = cell_run(engine, 40, 3030 * 0.001, reserve=3.03)
    assert result.states[LATCH] == "on"
    assert result.decision_id == "minimum_soc"
