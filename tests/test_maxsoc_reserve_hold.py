"""Ladedeckel: Pause nur, wenn die Reserve tatsächlich gehalten werden muss.

Bis 2026.11-beta2 setzte der Ladedeckel bei jeder aktiven Peak-Reserve
(benoetigt_kwh > 0) Pause. Mit SoC 95, Reserve 40 % und Preis NORMAL blieb der
Akku so in Pause, der SoC sank nicht und der Deckel hielt sich selbst
(Ratsche). Jetzt hält er nur noch, wenn L3, L4, die Extrempreis-Reserve oder
die EV-Sperre an dieser Stelle halten würden; sonst bleibt Entladen erlaubt.
Aktionsbaum (decisions.yaml) und Vorschau (templates.yaml) müssen übereinstimmen.
"""
from __future__ import annotations

import pytest

from tests.test_engine_parity import BASE, NOW, StrategyEngine, load_resources

MODE = "input_select.akkusteuerung_modus"
PREVIEW = "sensor.opti_strategie_vorschau"
RESERVE = "sensor.opti_peak_reserve_soc"


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


def run(engine, mode="Akku Dynamisch", ve=30.0, ve_avg=60.0, **changes):
    states = {
        **BASE,
        "input_boolean.akku_opti_automatik": "on",
        "input_boolean.opti_prognose_netzladen": "off",
        "sensor.opti_soc": "95",
        "input_number.maxsoc": "95",
        "binary_sensor.opti_peak_reserve_aktiv": "on",
        RESERVE: "40",
        MODE: mode,
        **changes,
    }
    attrs = {RESERVE: {"reserve_ve_soc": ve, "min_preis_vor_peak_ct": 30.0,
                       "peak_preis_avg_ct": 60.0, "peak_preis_ve_avg_ct": ve_avg}}
    result = engine.evaluate(states, attrs, NOW)
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    assert result.decision_id == "maximum_soc"
    assert result.states[PREVIEW] == result.mode
    assert result.attributes[PREVIEW]["grund"] == result.reason
    return result


@pytest.mark.parametrize("mode", ["Akku Dynamisch", "Akku Pause", "Akku nur Entladen"])
def test_reserve_far_below_soc_discharges_instead_of_ratchet_pause(engine, mode):
    result = run(engine, mode=mode)
    assert result.mode == "Akku nur Entladen"
    assert result.reason == "Ladedeckel (maxsoc erreicht)"


@pytest.mark.parametrize("mode,reserve,expected", [
    ("Akku Dynamisch", "95", "Akku Pause"),
    ("Akku Dynamisch", "94.9", "Akku nur Entladen"),
    # Gleiches Halteband wie L4: im Modus 'nur Laden' +2 Prozentpunkte.
    ("Akku nur Laden", "93", "Akku Pause"),
    ("Akku nur Laden", "92.9", "Akku nur Entladen"),
])
def test_l4_hold_condition_with_band_decides_pause(engine, mode, reserve, expected):
    result = run(engine, mode=mode, **{RESERVE: reserve})
    assert result.mode == expected
    if expected == "Akku Pause":
        assert result.reason == "Ladedeckel (maxsoc erreicht; Peak-Reserve halten)"


@pytest.mark.parametrize("ve_avg,expected", [(60.0, "Akku Pause"), (32.9, "Akku nur Entladen")])
def test_l3_hold_condition_including_spread_decides_pause(engine, ve_avg, expected):
    # SoC an der VE-Reserve: L2 entlädt nicht, L3 hält nur mit genug Spread.
    result = run(engine, ve=95.0, ve_avg=ve_avg, **{
        "sensor.opti_price_level": "EXPENSIVE",
        "sensor.opti_price_current_ct_kwh": "30",
        "input_number.opti_halte_spread_ct": "3",
    })
    assert result.mode == expected


def test_extreme_reserve_still_pauses_at_the_cap(engine):
    result = run(engine, **{
        "sensor.opti_price_level": "VERY_EXPENSIVE",
        "binary_sensor.opti_extreme_price_hold": "on",
    })
    assert result.mode == "Akku Pause"
    assert result.reason == "Ladedeckel (maxsoc erreicht; Extrempreis-Reserve halten)"


def test_ev_lock_still_pauses_without_reserve(engine):
    result = run(engine, **{
        "binary_sensor.opti_peak_reserve_aktiv": "off",
        "input_boolean.opti_ev_akku_pause": "on",
        "binary_sensor.opti_ev_schnellladung": "on",
    })
    assert result.mode == "Akku Pause"
    assert result.reason == "Ladedeckel (maxsoc erreicht; EV-Entladesperre)"


def test_pause_held_by_the_cap_keeps_the_hold_band_without_flapping(engine):
    """Review: ohne Band in der eigenen Pause flatterte 95,0 -> Pause, 95,1 -> Entladen."""
    mode = "Akku nur Laden"
    seen = []
    for soc in ("95.0", "95.1", "95.0", "95.1", "96.9", "97.0"):
        result = run(engine, mode=mode, **{RESERVE: "95", "sensor.opti_soc": soc})
        seen.append(result.mode)
        mode = result.mode
    assert seen == ["Akku Pause"] * 6
    # Oberhalb Reserve + 2 hält auch die Pause nicht mehr.
    above = run(engine, mode="Akku Pause", **{RESERVE: "95", "sensor.opti_soc": "97.1"})
    assert above.mode == "Akku nur Entladen"
    assert above.reason == "Ladedeckel (maxsoc erreicht)"


def test_cap_band_does_not_leak_into_the_real_l4_branch(engine):
    # Unter dem Ladedeckel gilt für L4 weiterhin nur das Band im Modus 'nur Laden'.
    states = {**BASE, "input_boolean.akku_opti_automatik": "on",
              "input_boolean.opti_prognose_netzladen": "off",
              "sensor.opti_soc": "41", "input_number.maxsoc": "95",
              "binary_sensor.opti_peak_reserve_aktiv": "on", RESERVE: "40",
              MODE: "Akku Pause"}
    attrs = {RESERVE: {"reserve_ve_soc": 30.0, "peak_preis_ve_avg_ct": 60.0,
                       "peak_preis_avg_ct": 60.0, "min_preis_vor_peak_ct": 30.0}}
    result = engine.evaluate(states, attrs, NOW)
    assert result.decision_id != "peak_l4"
    assert result.states[PREVIEW] == result.mode


DECKEL = "binary_sensor.opti_ladedeckel_aktiv"


def run_expensive(engine, mode, soc, deckel="on", deckel_maxsoc=95.0):
    """EXPENSIVE, maxsoc = reserve_ve_soc = 95, Halte-Spread ausreichend."""
    states = {
        **BASE,
        "input_boolean.akku_opti_automatik": "on",
        "input_boolean.opti_prognose_netzladen": "off",
        "input_number.maxsoc": "95",
        "binary_sensor.opti_peak_reserve_aktiv": "on",
        RESERVE: "95",
        "sensor.opti_price_level": "EXPENSIVE",
        "sensor.opti_price_current_ct_kwh": "30",
        "input_number.opti_halte_spread_ct": "3",
        "sensor.opti_soc": soc,
        DECKEL: deckel,
        MODE: mode,
    }
    attrs = {RESERVE: {"reserve_ve_soc": 95.0, "min_preis_vor_peak_ct": 30.0,
                       "peak_preis_avg_ct": 60.0, "peak_preis_ve_avg_ct": 60.0},
             DECKEL: {"maxsoc": deckel_maxsoc}}
    result = engine.evaluate(states, attrs, NOW)
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    assert result.states[PREVIEW] == result.mode
    assert result.attributes[PREVIEW]["grund"] == result.reason
    return result


@pytest.mark.parametrize("socs", [
    ("95.0", "95.1", "95.0", "95.1", "96.9", "95.2"),  # schwankend
    ("95.5",) * 6,                                      # konstant
])
def test_l2_respects_the_pause_held_by_the_active_cap(engine, socs):
    """Re-Review: L2 entlud die Ladedeckel-Pause mit Band 0, bevor L3 halten konnte."""
    mode = "Akku nur Laden"
    seen = []
    for soc in socs:
        result = run_expensive(engine, mode, soc)
        seen.append((result.decision_id, result.mode))
        mode = result.mode
    assert seen == [("maximum_soc", "Akku Pause")] * len(socs)
    # Oberhalb VE-Reserve + 2 entlädt L2 auch aus der Pause.
    above = run_expensive(engine, "Akku Pause", "97.1")
    assert (above.decision_id, above.mode) == ("peak_l2", "Akku nur Entladen")


@pytest.mark.parametrize("deckel,deckel_maxsoc", [("off", 95.0), ("on", 90.0)])
def test_l2_still_discharges_a_pause_without_active_cap(engine, deckel, deckel_maxsoc):
    result = run_expensive(engine, "Akku Pause", "95.5", deckel=deckel, deckel_maxsoc=deckel_maxsoc)
    assert (result.decision_id, result.mode) == ("peak_l2", "Akku nur Entladen")
