"""Optionaler Balancing-Vorrang: Preisgrenze, Schutz und unveränderte Frist."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.battery_model_fixture import model_attributes, model_states
from tests.test_balancing_window import HOURS, PREVIEW, WINDOW, assert_parity
from tests.test_engine import (
    DONE_AT, DONE_VALID, NOW, POWER, SOC, StrategyEngine, evaluate, load_resources,
    measurements, solar_attrs,
)
from tests.test_engine_parity import BASE

OPTION = "input_boolean.opti_balancing_halten_vor_peak"
PRICE = "sensor.opti_price_current_ct_kwh"
WATCHDOG = "sensor.opti_balancing_watchdog"
PEAK = "binary_sensor.opti_peak_reserve_aktiv"
LEVEL = "sensor.opti_price_level"
REASON = "Balancing-Watchdog (Haltefenster, Vorrang vor Peak-Leiter)"


@pytest.fixture
def hold_engine():
    """Fenster und Vorschau ableiten, vorgelagerte Schutz-/Peak-Signale vorgeben."""
    resources = load_resources()
    for block in resources["template_blocks"]:
        for domain in ("sensor", "binary_sensor"):
            block[domain] = [item for item in block.get(domain, [])
                             if f"{domain}.{item['unique_id']}" in (WINDOW, PREVIEW)]
    resources["statistics"] = {}
    resources["balancing_automations"] = []
    return StrategyEngine(resources)


def peak_states(level="VERY_EXPENSIVE", **overrides):
    return {
        **BASE, SOC: 92, HOURS: 4, DONE_VALID: "on", DONE_AT: NOW.isoformat(),
        "input_boolean.akku_opti_automatik": "on",
        "input_boolean.opti_prognose_netzladen": "off",
        "input_number.maxsoc": 100,
        "sensor.opti_peak_reserve_soc": 30,
        OPTION: "on", PRICE: 50, PEAK: "on", LEVEL: level, **model_states(), **overrides,
    }


def peak_attrs():
    return {"sensor.opti_peak_reserve_soc": {"reserve_ve_soc": 30, "reserve_schutz_soc": 30,
                                             "min_preis_vor_peak_ct": 50,
                                             "peak_preis_avg_ct": 100,
                                             "peak_preis_kuenftig_ct": 100},
            **model_attributes()}


@pytest.mark.parametrize("level,peak", [("VERY_EXPENSIVE", "peak_l1"), ("EXPENSIVE", "peak_l2")])
@pytest.mark.parametrize("option", [None, "off", "on"])
@pytest.mark.parametrize("price,valid", [
    (-1, True), (0, True), (50, True), (60, True), (60.01, False), (100, False),
    ("fehlt", False), (None, False), ("unknown", False), ("unavailable", False),
    ("ungueltig", False), ("", False), ("nan", False), ("inf", False), ("-inf", False),
])
def test_price_and_opt_in_gate_both_peak_levels(hold_engine, level, peak, option, price, valid):
    states = peak_states(level, **{PRICE: price, OPTION: option})
    if option is None:
        states.pop(OPTION)
    if price == "fehlt":
        states.pop(PRICE)
    result = hold_engine.evaluate(states, peak_attrs(), NOW)
    holding = option == "on" and valid
    assert result.decision_id == ("balancing_hold" if holding else peak)
    assert result.mode == ("Akku nur Laden" if holding else "Akku nur Entladen")
    if holding:
        assert result.reason == REASON
    assert_parity(result)


@pytest.mark.parametrize("level,peak", [("VERY_EXPENSIVE", "peak_l1"), ("EXPENSIVE", "peak_l2")])
@pytest.mark.parametrize("watchdog", ["pv", "netz", "unknown", "unavailable"])
def test_running_or_unknown_watchdog_does_not_gain_priority(hold_engine, level, peak, watchdog):
    result = hold_engine.evaluate(peak_states(level, **{WATCHDOG: watchdog}), peak_attrs(), NOW)
    assert result.decision_id == peak
    assert_parity(result)


@pytest.mark.parametrize("changes,attrs,expected", [
    ({SOC: 90, "input_number.minsoc": 90}, {}, "minimum_soc"),
    ({SOC: 91, "input_number.minsoc": 90,
      "binary_sensor.opti_minsoc_schutz_aktiv": "on"},
     {"release_soc": 93}, "minimum_soc"),
    ({"binary_sensor.opti_minsoc_schutz_aktiv": "on"},
     {"ausloeser": "zellspannung", "zell_min_v": 3.0, "release_zell_v": 3.2}, "minimum_soc"),
    ({"binary_sensor.opti_minsoc_schutz_aktiv": "on", WATCHDOG: "netz"},
     {"release_soc": 93}, "balancing_grid"),
    # Peak-Vorladen ist bei EXPENSIVE/VERY_EXPENSIVE gesperrt, daher NORMAL.
    ({"input_boolean.opti_prognose_netzladen": "on", "sensor.opti_peak_reserve_soc": 100,
      LEVEL: "NORMAL"}, {}, "peak_precharge"),
    ({"input_boolean.opti_prognose_netzladen": "on", "sensor.opti_forecast_score": 1,
      PRICE: -1}, {}, "negative_price"),
])
def test_protection_and_grid_charging_keep_priority(hold_engine, changes, attrs, expected):
    attributes = {**peak_attrs(), "binary_sensor.opti_minsoc_schutz_aktiv": attrs}
    result = hold_engine.evaluate(peak_states(**changes), attributes, NOW)
    assert result.states[WINDOW] == "on"
    assert result.decision_id == expected
    if attrs.get("ausloeser") == "zellspannung":
        assert result.reason.startswith("Zellspannungs-Reserve")
    assert_parity(result)


@pytest.mark.parametrize("level", ["VERY_EXPENSIVE", "EXPENSIVE"])
@pytest.mark.parametrize("changes,expected", [
    ({"input_boolean.opti_ev_akku_pause": "on", "binary_sensor.opti_ev_schnellladung": "on"},
     "ev_lock"),
    ({"binary_sensor.opti_extreme_price_hold": "on"}, "extreme_peak_hold"),
])
def test_ev_and_extreme_reserve_never_discharge(hold_engine, level, changes, expected):
    for option in ("off", "on"):
        for hours in (4, 0):
            result = hold_engine.evaluate(
                peak_states(level, **{OPTION: option, HOURS: hours, **changes}), peak_attrs(), NOW)
            # Ohne Option hält das normale Haltefenster bei EXPENSIVE/VERY_EXPENSIVE
            # nicht mehr; die EV- bzw. Extrempreis-Sperre hält dann selbst.
            holding = hours and option == "on"
            assert result.decision_id == ("balancing_hold" if holding else expected)
            assert result.mode == "Akku nur Laden"
            assert_parity(result)


@pytest.mark.parametrize("level,peak", [("VERY_EXPENSIVE", "peak_l1"), ("EXPENSIVE", "peak_l2")])
def test_soc_dip_price_bypass_and_restart_do_not_extend_deadline(hold_engine, level, peak):
    deadline = NOW + dt.timedelta(hours=4)
    for minutes, soc, price, holding in [
        (0, 90, 60, True), (60, 89.9, 60, False), (120, 90, 60.01, False),
        (180, 90, 60, True), (239, 90, 60, True), (240, 90, 60, False),
        (241, 90, 60, False),
    ]:
        result = hold_engine.evaluate(
            peak_states(level, **{SOC: soc, PRICE: price}), peak_attrs(),
            NOW + dt.timedelta(minutes=minutes))
        assert dt.datetime.fromisoformat(result.attributes[WINDOW]["ende"]) == deadline
        assert result.decision_id == ("balancing_hold" if holding else peak)
        assert_parity(result)
        restored = StrategyEngine(hold_engine.resources)
        restored.restore(hold_engine.snapshot())
        hold_engine = restored


@pytest.mark.parametrize("option,expected", [("off", "peak_l1"), ("on", "balancing_hold")])
@pytest.mark.parametrize("soc,temperature,limit,hold_power", [
    (97, 25, 3000, 200), (92, 25, 3000, 500), (97, 25, 100, 100), (97, 50, 3000, 0),
])
def test_real_pipeline_keeps_balancing_and_temperature_limits(
    option, expected, soc, temperature, limit, hold_power
):
    now = NOW.replace(hour=19)
    states = measurements(**{
        SOC: soc, HOURS: 4, DONE_VALID: "on", DONE_AT: now.isoformat(),
        OPTION: option, PRICE: 50, "sensor.opti_battery_temp": temperature,
        "input_number.akkusteuerung_max_ladestaerke": limit,
        "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sun.sun": "below_horizon",
    })
    attrs = solar_attrs(now)
    attrs["sensor.opti_price_series"] = {
        "today": [10] * 18 + [50] * 4 + [10] * 2,
        "tomorrow": [10] * 24,
    }
    result = evaluate(states=states, attributes=attrs, now=now)
    assert result.states[WINDOW] == "on"
    assert result.states[WATCHDOG] == "aus"
    assert result.decision_id == expected
    if option == "on":
        assert result.reason == REASON
        assert float(result.states[POWER]) == hold_power
    assert_parity(result)


@pytest.mark.parametrize("option,expected_reason", [
    ("on", REASON), ("off", "Balancing-Watchdog (Haltefenster)"),
])
def test_without_peak_mode_is_unchanged_and_reason_stays_neutral(hold_engine, option, expected_reason):
    """Ohne Preisspitze ändert die Option nur den Grund-Text, nicht Modus oder Entscheidung."""
    result = hold_engine.evaluate(
        peak_states("NORMAL", **{OPTION: option, PRICE: 30, PEAK: "off"}), peak_attrs(), NOW)
    assert result.decision_id == "balancing_hold"
    assert result.mode == "Akku nur Laden"
    assert result.reason == expected_reason
    assert_parity(result)


@pytest.mark.parametrize("level,holding", [
    ("VERY_EXPENSIVE", False), ("EXPENSIVE", False),
    ("NORMAL", True), ("CHEAP", True), ("VERY_CHEAP", True),
    # Unbekanntes Preisniveau gilt nicht als "nicht teuer": kein Halten.
    ("unavailable", False), ("unknown", False),
])
def test_normal_window_does_not_hold_a_full_battery_at_expensive_prices(hold_engine, level, holding):
    """8.11.: ohne Peak-Reserve hielt das Fenster 3 h einen vollen Akku bei 53-55 ct.

    Der normale Haltefenster-Zweig hält nur noch unterhalb EXPENSIVE; bei teurem
    Preis fällt die Entscheidung auf den Ladedeckel durch. Das Fenster selbst und
    der bestätigte Abschluss bleiben unberührt.
    """
    result = hold_engine.evaluate(
        peak_states(level, **{OPTION: "off", PEAK: "off", SOC: 95, "input_number.maxsoc": 95,
                              PRICE: 54}),
        peak_attrs(), NOW)
    assert result.states[WINDOW] == "on"
    assert result.decision_id == ("balancing_hold" if holding else "maximum_soc")
    assert result.mode == ("Akku nur Laden" if holding else "Akku nur Entladen")
    assert result.reason == ("Balancing-Watchdog (Haltefenster)" if holding
                             else "Ladedeckel (maxsoc erreicht)")
    assert_parity(result)


@pytest.mark.parametrize("level", ["VERY_EXPENSIVE", "EXPENSIVE"])
def test_expensive_window_below_cap_falls_through_to_following_branches(hold_engine, level):
    result = hold_engine.evaluate(
        peak_states(level, **{OPTION: "off", PEAK: "off", PRICE: 54}), peak_attrs(), NOW)
    assert result.states[WINDOW] == "on"
    assert result.decision_id == "above_target"
    assert result.mode == "Akku nur Entladen"
    assert_parity(result)


@pytest.mark.parametrize("level", ["VERY_EXPENSIVE", "EXPENSIVE"])
def test_opt_in_priority_branch_keeps_its_own_60_ct_cap(hold_engine, level):
    result = hold_engine.evaluate(
        peak_states(level, **{OPTION: "on", PEAK: "off", PRICE: 54}), peak_attrs(), NOW)
    assert result.decision_id == "balancing_hold"
    assert result.reason == REASON
    assert_parity(result)
