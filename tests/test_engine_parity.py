"""All 22 original decision branches run through StrategyEngine.evaluate.

The L3 boundary fixture uses SoC equal to VE reserve after the 0.5.2b3
hysteresis correction; original +3 entry padding is intentionally removed.
Golden inputs and expected mode/reason originate from the predecessor's tracked
strategy-parity suite at 7f4d5214ff49a86a7eaf2f0551720fe96830bde3. Only the preview
is derived in this decision-layer fixture: its upstream measurements are fixed
inputs, while the complete real strategy action tree executes. Full upstream
pipeline/time/restore coverage lives in test_engine.py.
"""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.ev_preparation import apply_preparation

_SPEC = importlib.util.spec_from_file_location(
    "opti_parity_engine", Path(__file__).parents[1] / "custom_components/opti_akku/engine.py"
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
StrategyEngine = _MODULE.StrategyEngine
load_resources = _MODULE.load_resources
NOW = dt.datetime(2026, 1, 15, 18, 30, tzinfo=ZoneInfo("Europe/Berlin"))

BASE = {'sensor.opti_soc': '40',
 'sensor.opti_battery_capacity_kwh': '12.8',
 'sensor.opti_forecast_score': '5',
 'sensor.opti_forecast_score_tomorrow': '5',
 'sensor.opti_price_level': 'NORMAL',
 'sensor.opti_target_soc': '60',
 'sensor.opti_price_current_ct_kwh': '30',
 'binary_sensor.opti_ueberschuss_70_aktiv': 'off',
 'binary_sensor.opti_ueberschuss_ac_aktiv': 'off',
 'binary_sensor.opti_ueberschuss_veto_aktiv': 'off',
 'sensor.opti_peak_reserve_soc': 'unavailable',
 'binary_sensor.opti_peak_reserve_aktiv': 'off',
 'binary_sensor.opti_winter_charging_allowed': 'on',
 'input_number.minsoc': '10',
 'input_number.maxsoc': '95',
 'input_number.opti_einspeiseverguetung_ct': '8',
 'input_number.opti_netzlade_spread_ct': '10',
 'input_number.opti_halte_spread_ct': '0',
 'input_boolean.opti_prognose_netzladen': 'on',
 'input_boolean.opti_pv_ueberschuss_ladung': 'on',
 'input_select.akkusteuerung_modus': 'Akku Dynamisch',
 'sun.sun': 'below_horizon',
 'sensor.opti_balancing_watchdog': 'aus',
 'input_boolean.opti_ev_akku_pause': 'off',
 'binary_sensor.opti_ev_schnellladung': 'off'}

ORIGINAL_DECISION_IDS = ["minimum_soc", "negative_price", "peak_precharge", "peak_l1", "peak_l2", "balancing_pv", "balancing_grid", "maximum_soc", "reserve_low", "reserve_forecast", "reserve_winter", "reserve_today_low", "reserve_cheap", "ev_lock", "surplus_70", "surplus_ac", "battery_full", "peak_l3", "peak_l4", "surplus_veto", "below_target", "above_target"]

BRANCHES = [(0, 'minsoc_schutz', {'sensor.opti_soc': '5'}, 'Akku nur Laden', 'MinSOC-Schutz'),
 (1,
  'negativpreis_netzladen',
  {'sensor.opti_price_current_ct_kwh': '3', 'sensor.opti_forecast_score': '1'},
  'Akku Netzladen',
  'Negativpreis'),
 (2,
  'peak_vorladen',
  {'sensor.opti_price_current_ct_kwh': '50',
   'sensor.opti_forecast_score': '1',
   'sensor.opti_forecast_score_tomorrow': '1',
   'sensor.opti_peak_reserve_soc': '35',
   'binary_sensor.opti_peak_reserve_aktiv': 'on',
   'sensor.opti_soc': '15',
   '_attrs': {'sensor.opti_peak_reserve_soc': {'reserve_ve_soc': 25.0,
                                               'min_preis_vor_peak_ct': 50.0,
                                               'peak_preis_avg_ct': 200.0,
                                               'peak_preis_ve_avg_ct': 200.0}}},
  'Akku Netzladen',
  'Peak-Vorladen'),
 (3,
  'leiter_l1_very_expensive',
  {'sensor.opti_forecast_score': '1',
   'sensor.opti_forecast_score_tomorrow': '1',
   'sensor.opti_peak_reserve_soc': '45',
   'binary_sensor.opti_peak_reserve_aktiv': 'on',
   'sensor.opti_price_current_ct_kwh': '50',
   'sensor.opti_soc': '85',
   'sensor.opti_price_level': 'VERY_EXPENSIVE',
   '_attrs': {'sensor.opti_peak_reserve_soc': {'reserve_ve_soc': 30.0,
                                               'min_preis_vor_peak_ct': 50.0,
                                               'peak_preis_avg_ct': 200.0,
                                               'peak_preis_ve_avg_ct': 200.0}}},
  'Akku nur Entladen',
  'Peak-Leiter L1'),
 (4,
  'leiter_l2_expensive',
  {'sensor.opti_forecast_score': '1',
   'sensor.opti_forecast_score_tomorrow': '1',
   'sensor.opti_peak_reserve_soc': '45',
   'binary_sensor.opti_peak_reserve_aktiv': 'on',
   'sensor.opti_price_current_ct_kwh': '50',
   'sensor.opti_soc': '85',
   'sensor.opti_price_level': 'EXPENSIVE',
   '_attrs': {'sensor.opti_peak_reserve_soc': {'reserve_ve_soc': 30.0,
                                               'min_preis_vor_peak_ct': 50.0,
                                               'peak_preis_avg_ct': 200.0,
                                               'peak_preis_ve_avg_ct': 200.0}}},
  'Akku nur Entladen',
  'Peak-Leiter L2'),
 (5,
  'balancing_watchdog_pv',
  {'sensor.opti_balancing_watchdog': 'pv'},
  'Akku nur Laden',
  'Balancing-Watchdog (PV'),
 (6,
  'balancing_watchdog_netz',
  {'sensor.opti_balancing_watchdog': 'netz'},
  'Akku Netzladen',
  'Balancing-Watchdog (Netz'),
 (7, 'ladedeckel', {'sensor.opti_soc': '96'}, 'Akku nur Entladen', 'Ladedeckel'),
 (8,
  'soc20_prognose',
  {'sensor.opti_soc': '18',
   'sensor.opti_forecast_score': '1',
   'sensor.opti_forecast_score_tomorrow': '1',
   'sensor.opti_price_level': 'NORMAL'},
  'Akku nur Laden',
  'SOC<20'),
 (9,
  'soc75_prognose',
  {'sensor.opti_soc': '50',
   'sensor.opti_forecast_score': '1',
   'sensor.opti_forecast_score_tomorrow': '1',
   'sensor.opti_price_level': 'NORMAL'},
  'Akku nur Laden',
  'SOC<75'),
 (10,
  'soc80_wintermodus',
  {'sensor.opti_soc': '78',
   'sensor.opti_forecast_score': '1',
   'sensor.opti_forecast_score_tomorrow': '1',
   'sensor.opti_price_level': 'EXPENSIVE'},
  'Akku nur Laden',
  'SOC<80 Wintermodus'),
 (11,
  'soc15_notfall',
  {'sensor.opti_soc': '12', 'sensor.opti_forecast_score': '1', 'sensor.opti_price_level': 'NORMAL'},
  'Akku nur Laden',
  'SOC<15 Notfall'),
 (12,
  'soc45_sehr_guenstig',
  {'sensor.opti_soc': '30', 'sensor.opti_forecast_score': '1', 'sensor.opti_price_level': 'CHEAP'},
  'Akku nur Laden',
  'SOC<45'),
 (13,
  'ev_sperre_schattet_ueberschuss',
  {'sun.sun': 'above_horizon',
   'sensor.opti_soc': '70',
   'sensor.opti_target_soc': '50',
   'binary_sensor.opti_ueberschuss_70_aktiv': 'on',
   'input_boolean.opti_ev_akku_pause': 'on',
   'binary_sensor.opti_ev_schnellladung': 'on'},
  'Akku nur Laden',
  'EV-Sperre'),
 (14,
  'ueberschuss_70',
  {'sun.sun': 'above_horizon',
   'sensor.opti_soc': '70',
   'sensor.opti_target_soc': '50',
   'binary_sensor.opti_ueberschuss_70_aktiv': 'on'},
  'Akku Dynamisch',
  '70% Ueberschuss'),
 (15,
  'ueberschuss_ac',
  {'sun.sun': 'above_horizon',
   'sensor.opti_soc': '70',
   'sensor.opti_target_soc': '50',
   'binary_sensor.opti_ueberschuss_ac_aktiv': 'on'},
  'Akku Dynamisch',
  'AC Ueberschuss'),
 # With a valid Max-SoC, the ladedeckel now intentionally owns this boundary.
 # An unavailable limit isolates the historical full-battery fallback itself.
 (16,
  'akku_voll',
  {'sensor.opti_soc': '100',
   'input_number.maxsoc': 'unavailable',
   'binary_sensor.opti_peak_reserve_aktiv': 'on',
   '_attrs': {'sensor.opti_peak_reserve_soc': {'reserve_ve_soc': 30.0,
                                               'min_preis_vor_peak_ct': 50.0,
                                               'peak_preis_avg_ct': 200.0,
                                               'peak_preis_ve_avg_ct': 200.0}}},
  'Akku Dynamisch',
  'Akku voll'),
 (17,
  'leiter_l3_halten',
  {'sensor.opti_forecast_score': '1',
   'sensor.opti_forecast_score_tomorrow': '1',
   'sensor.opti_peak_reserve_soc': '45',
   'binary_sensor.opti_peak_reserve_aktiv': 'on',
   'sensor.opti_price_current_ct_kwh': '50',
   'sensor.opti_soc': '30',
   'sensor.opti_price_level': 'EXPENSIVE',
   'input_boolean.opti_prognose_netzladen': 'off',
   'input_number.opti_halte_spread_ct': '3',
   '_attrs': {'sensor.opti_peak_reserve_soc': {'reserve_ve_soc': 30.0,
                                               'min_preis_vor_peak_ct': 50.0,
                                               'peak_preis_avg_ct': 200.0,
                                               'peak_preis_ve_avg_ct': 55.0}}},
  'Akku nur Laden',
  'Peak-Leiter L3'),
 (18,
  'leiter_l4_halten',
  {'sensor.opti_forecast_score': '1',
   'sensor.opti_forecast_score_tomorrow': '1',
   'sensor.opti_peak_reserve_soc': '45',
   'binary_sensor.opti_peak_reserve_aktiv': 'on',
   'sensor.opti_price_current_ct_kwh': '50',
   'sensor.opti_soc': '40',
   'sensor.opti_price_level': 'NORMAL',
   'input_boolean.opti_prognose_netzladen': 'off',
   '_attrs': {'sensor.opti_peak_reserve_soc': {'reserve_ve_soc': 30.0,
                                               'min_preis_vor_peak_ct': 50.0,
                                               'peak_preis_avg_ct': 200.0,
                                               'peak_preis_ve_avg_ct': 200.0}}},
  'Akku nur Laden',
  'Peak-Leiter L4'),
 (19,
  'ueberschuss_veto_sticht_ziel_soc',
  {'sun.sun': 'above_horizon',
   'sensor.opti_soc': '70',
   'sensor.opti_target_soc': '60',
   'binary_sensor.opti_ueberschuss_veto_aktiv': 'on'},
  'Akku Dynamisch',
  'Ueberschuss-Veto'),
 (20,
  'dyn_bis_ziel',
  {'sun.sun': 'above_horizon', 'sensor.opti_soc': '40', 'sensor.opti_target_soc': '60'},
  'Akku Dynamisch',
  'dyn bis Ziel'),
 (21,
  'ueber_ziel_soc',
  {'sensor.opti_soc': '70', 'sensor.opti_target_soc': '60'},
  'Akku nur Entladen',
  'ueber Ziel-SoC'),
 ('default', 'default_nacht', {}, 'Akku Dynamisch', 'Default')]


@pytest.fixture
def decision_engine():
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


@pytest.mark.parametrize(
    "branch,name,overrides,expected_mode,reason", BRANCHES,
    ids=[branch[1] for branch in BRANCHES],
)
def test_every_original_branch_executes_through_engine(
    decision_engine, branch, name, overrides, expected_mode, reason
):
    overrides = copy.deepcopy(overrides)
    attrs = overrides.pop("_attrs", {})
    states = {**BASE, **overrides, "input_boolean.akku_opti_automatik": "on"}
    result = decision_engine.evaluate(states, attrs, NOW)
    assert result.mode == expected_mode

    assert result.decision_id == ("default" if branch == "default" else ORIGINAL_DECISION_IDS[branch])
    assert reason in result.reason
    assert result.states["sensor.opti_strategie_vorschau"] == expected_mode
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"


def test_all_original_branches_have_explicit_golden_case():
    choose = load_resources()["strategy"]["actions"][0]["choose"]
    actual = [branch["sequence"][0]["decision_id"] for branch in choose]
    assert actual.pop(17) == "extreme_peak_hold"
    assert actual == ORIGINAL_DECISION_IDS
    assert len(actual) == len(BRANCHES) - 1
    assert [case[0] for case in BRANCHES[:-1]] == list(range(len(actual)))


@pytest.mark.parametrize("previous", ["Akku Dynamisch", "Akku nur Entladen"])
def test_missing_price_returns_to_dynamic_in_neutral_zone(decision_engine, previous):
    states = {**BASE,
        "input_boolean.akku_opti_automatik": "on",
        "input_select.akkusteuerung_modus": previous,
        "sensor.opti_price_level": "unavailable",
        "sensor.opti_soc": "59",
    }
    result = decision_engine.evaluate(states, {}, NOW)
    assert result.mode == "Akku Dynamisch"
    assert "Preisniveau fehlt (Dynamisch)" in result.reason


@pytest.mark.parametrize("previous", ["Akku Netzladen", "Akku nur Laden", "Akku Pause"])
def test_missing_price_releases_forced_mode_in_neutral_zone(decision_engine, previous):
    states = {**BASE,
        "input_boolean.akku_opti_automatik": "on",
        "input_select.akkusteuerung_modus": previous,
        "sensor.opti_price_level": "unavailable",
        "sensor.opti_soc": "59",
    }
    result = decision_engine.evaluate(states, {}, NOW)
    assert result.mode == "Akku Dynamisch"


def test_unavailable_forecast_gate_never_enables_price_reserve_branch(decision_engine):
    states = {**BASE,
        "input_boolean.akku_opti_automatik": "on",
        "sensor.opti_soc": "18",
        "sensor.opti_forecast_score": "1",
        "sensor.opti_forecast_score_tomorrow": "1",
        "sensor.opti_price_level": "NORMAL",
        "input_boolean.opti_prognose_netzladen": "unavailable",
    }
    result = decision_engine.evaluate(states, {}, NOW)
    assert result.mode == "Akku Dynamisch"


def test_missing_price_default_never_becomes_ev_preparation(decision_engine):
    states = {**BASE, "sensor.opti_price_level": "unavailable", "input_boolean.akku_opti_automatik": "on"}
    result = decision_engine.evaluate(states, {}, NOW)
    assert result.mode == "Akku Dynamisch"
    assert result.decision_id == "price_unavailable"
    assert apply_preparation(result, {"ready": True, "target_soc": 90})[0] is result


@pytest.mark.parametrize("price_level", ["NORMAL", "EXPENSIVE", "VERY_EXPENSIVE"])
def test_extreme_hold_blocks_relative_peak_discharge(decision_engine, price_level):
    states = {**BASE, "input_boolean.akku_opti_automatik": "on",
              "input_boolean.opti_prognose_netzladen": "off",
              "binary_sensor.opti_peak_reserve_aktiv": "on",
              "binary_sensor.opti_extreme_price_hold": "on",
              "sensor.opti_peak_reserve_soc": "50",
              "sensor.opti_price_level": price_level,
              "sensor.opti_price_current_ct_kwh": "50"}
    attrs = {"sensor.opti_peak_reserve_soc": {"reserve_ve_soc": 20}}
    result = decision_engine.evaluate(states, attrs, NOW)
    assert result.decision_id == "extreme_peak_hold"
    assert result.mode == result.states["sensor.opti_strategie_vorschau"] == "Akku nur Laden"
    assert "Extrempreis-Reserve" in result.reason
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"
    # Only the EV charging guard can replace this reserve, not EV preparation.
    assert apply_preparation(result, {"ready": True, "target_soc": 90})[0] is result
    guarded, _ = apply_preparation(result, {"charging_guard": True})
    assert guarded.mode == "Akku Pause"
    assert guarded.decision_id == "ev_priority"


@pytest.mark.parametrize("changes, attrs, expected", [
    ({"sensor.opti_soc": "10"}, {}, "minimum_soc"),
    ({"sensor.opti_balancing_watchdog": "pv"}, {}, "balancing_pv"),
    ({"sensor.opti_balancing_watchdog": "netz"}, {}, "balancing_grid"),
    ({"sensor.opti_soc": "95"}, {}, "maximum_soc"),
    ({"sensor.opti_soc": "93", "binary_sensor.opti_ladedeckel_aktiv": "on"},
     {"binary_sensor.opti_ladedeckel_aktiv": {"maxsoc": 95}}, "maximum_soc"),
    ({"input_boolean.opti_ev_akku_pause": "on", "binary_sensor.opti_ev_schnellladung": "on"}, {}, "ev_lock"),
    ({"input_boolean.opti_prognose_netzladen": "on"},
     {"sensor.opti_peak_reserve_soc": {"min_preis_vor_peak_ct": 50, "peak_preis_avg_ct": 100}}, "peak_precharge"),
])
def test_extreme_hold_preserves_existing_priority(decision_engine, changes, attrs, expected):
    states = {**BASE, "input_boolean.akku_opti_automatik": "on",
              "input_boolean.opti_prognose_netzladen": "off",
              "binary_sensor.opti_peak_reserve_aktiv": "on",
              "binary_sensor.opti_extreme_price_hold": "on",
              "sensor.opti_peak_reserve_soc": "70",
              "sensor.opti_price_current_ct_kwh": "50", **changes}
    result = decision_engine.evaluate(states, attrs, NOW)
    assert result.decision_id == expected
    assert result.mode == result.states["sensor.opti_strategie_vorschau"]
    if expected == "maximum_soc":
        assert result.mode == "Akku Pause"
    if expected == "peak_precharge":
        assert result.mode == "Akku Netzladen"
    assert result.states["sensor.opti_engine_diagnostics"] == "ok"


@pytest.mark.parametrize("hold, expected", [("on", "extreme_peak_hold"), ("off", "peak_l1")])
def test_extreme_hold_release_restores_peak_use(decision_engine, hold, expected):
    states = {**BASE, "input_boolean.akku_opti_automatik": "on",
              "input_boolean.opti_prognose_netzladen": "off",
              "binary_sensor.opti_peak_reserve_aktiv": "on",
              "binary_sensor.opti_extreme_price_hold": hold,
              "sensor.opti_peak_reserve_soc": "50",
              "sensor.opti_price_level": "VERY_EXPENSIVE"}
    result = decision_engine.evaluate(states, {}, NOW)
    assert result.decision_id == expected
    assert result.mode == result.states["sensor.opti_strategie_vorschau"]
