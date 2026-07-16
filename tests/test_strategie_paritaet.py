"""Paritaets-Test: echte Steuer-Automation vs. Spiegel-Sensor.

Die Steuerung lebt in automations/opti_strategie.yaml (action-Alias
"Zwischen Speicherszenarien waehlen": choose-Kette mit 21 Optionen + default).
Ihr Spiegel ist der Vorschau-Sensor opti_strategie_vorschau in
packages/opti_derived.yaml (state-if/elif-Kette + grund-Attribut). Der
YAML-Kommentar verlangt manuelle Spiegelung ("MUSS mitgespiegelt werden") -
ohne diesen Test laesst eine Aenderung an der Automation alle Tests gruen,
obwohl die Steuerung von der Vorschau abweicht.

Der Test baut fuer JEDE der 21 choose-Optionen + den Default eine Fixture
(FakeHass-Zustand), die genau diesen Zweig trifft, und prueft:
  (i)   Automation-choose-Kette (eigener Evaluator) -> getroffener Zweig-Index
        + gesetzter Modus.
  (ii)  Vorschau-Sensor (state + grund) mit DERSELBEN FakeHass -> Modus + Label.
  Assert: Modus identisch UND der getroffene Zweig ist der beabsichtigte
  (Index der choose-Option == Position des grund-Labels).
Ein Struktur-Assert koppelt die Zahl der choose-Optionen an die Zahl der vom
Test abgedeckten Zweige: wer eine weitere Option ergaenzt, ohne den Test zu
erweitern, bricht hier hart.

Bewusst ignoriert: Trigger und die Top-Level-condition der Automation
(akku_opti_automatik on) sowie die beiden CLEANUP-Actions (Counter-Reset,
Booster-Aus). Fuer die Paritaet zaehlt ausschliesslich die Modus-Wahl in der
choose-Kette der Action "Zwischen Speicherszenarien waehlen".

Bekannte, GEWOLLTE Aequivalenz (kein Fail): die Vorschau-L3 (Zweig 17) laesst
die Bedingung 'soc <= ve_res + band' weg, die in der Automation-L3 explizit
steht. Das ist aequivalent, weil fuer soc > ve_res + band bei EXPENSIVE bereits
der davorstehende L2-Zweig (Index 4) in BEIDEN Seiten entladen haette; die
L3-Fixture liegt unter dieser Schwelle, sodass beide Seiten uebereinstimmen.
"""
from __future__ import annotations

import pytest

from .condition_eval import evaluate_condition
from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render
from .test_strategie_vorschau import BASIS, reserve_attrs

MAIN_ACTION_ALIAS = "Zwischen Speicherszenarien wählen"
MODUS_ENTITY = "input_select.akkusteuerung_modus"


def _load_main_action():
    cfg = load_yaml(REPO / "automations" / "opti_strategie.yaml")
    automation = cfg[0]
    for action in automation["actions"]:
        if action.get("alias") == MAIN_ACTION_ALIAS:
            return action
    raise KeyError(f"Action '{MAIN_ACTION_ALIAS}' nicht gefunden")


def _load_vorschau_entity():
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    return find_template_entity(cfg, "sensor", "opti_strategie_vorschau")


MAIN_ACTION = _load_main_action()
CHOOSE_OPTIONS = MAIN_ACTION["choose"]
VORSCHAU = _load_vorschau_entity()


def _mode_from_sequence(sequence):
    """Ziel-Modus einer choose-Option: input_select.select_option auf
    input_select.akkusteuerung_modus. Zusaetzliche Aktionen (stop, ...) egal."""
    for step in sequence:
        if step.get("action") == "input_select.select_option":
            if step.get("target", {}).get("entity_id") == MODUS_ENTITY:
                return step["data"]["option"]
    raise AssertionError(f"Kein Modus-Set in sequence: {sequence!r}")


def _evaluate_automation(hass):
    """choose-Kette auswerten -> (zweig, modus). zweig ist der Options-Index
    (0-basiert) oder 'default'. modus None, wenn auch der Default nicht greift."""
    for idx, option in enumerate(CHOOSE_OPTIONS):
        conds = option.get("conditions", [])
        if all(evaluate_condition(hass, c) for c in conds):
            return idx, _mode_from_sequence(option["sequence"])
    for step in MAIN_ACTION.get("default", []) or []:
        for option in step.get("choose", []) or []:
            conds = option.get("conditions", [])
            if all(evaluate_condition(hass, c) for c in conds):
                return "default", _mode_from_sequence(option["sequence"])
    return "default", None


def _make_hass(overrides):
    overrides = dict(overrides)
    attrs = overrides.pop("_attrs", None) or {}
    states = dict(BASIS)
    states.update(overrides)
    return FakeHass(states=states, attrs=attrs)


def _vorschau(hass, part):
    template = VORSCHAU["state"] if part == "state" else VORSCHAU["attributes"]["grund"]
    return render(hass, template)


# --- Zweig-Fixtures: jede trifft genau EINE choose-Option (Index) bzw. Default.
# grund = Teilstring, der das getroffene Vorschau-Label eindeutig festnagelt.
LEITER_BASE = {
    "sensor.opti_forecast_score": "1",
    "sensor.opti_forecast_score_tomorrow": "1",
    "sensor.opti_peak_reserve_soc": "45",
    "binary_sensor.opti_peak_reserve_aktiv": "on",
    "sensor.opti_price_current_ct_kwh": "50",
}

# (index, name, overrides, erwarteter_modus, grund-Teilstring)
BRANCHES = [
    (0, "minsoc_schutz",
     {"sensor.opti_soc": "5"},
     "Akku nur Laden", "MinSOC-Schutz"),
    (1, "negativpreis_netzladen",
     {"sensor.opti_price_current_ct_kwh": "3", "sensor.opti_forecast_score": "1"},
     "Akku Netzladen", "Negativpreis"),
    (2, "peak_vorladen",
     {"sensor.opti_price_current_ct_kwh": "50", "sensor.opti_forecast_score": "1",
      "sensor.opti_forecast_score_tomorrow": "1", "sensor.opti_peak_reserve_soc": "35",
      "binary_sensor.opti_peak_reserve_aktiv": "on", "sensor.opti_soc": "15",
      "_attrs": reserve_attrs(ve=25.0, min_vor=50.0, avg=200.0)},
     "Akku Netzladen", "Peak-Vorladen"),
    (3, "leiter_l1_very_expensive",
     {**LEITER_BASE, "sensor.opti_soc": "85", "sensor.opti_price_level": "VERY_EXPENSIVE",
      "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0)},
     "Akku nur Entladen", "Peak-Leiter L1"),
    (4, "leiter_l2_expensive",
     {**LEITER_BASE, "sensor.opti_soc": "85", "sensor.opti_price_level": "EXPENSIVE",
      "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0)},
     "Akku nur Entladen", "Peak-Leiter L2"),
    (5, "balancing_watchdog_pv",
     {"sensor.opti_balancing_watchdog": "pv"},
     "Akku nur Laden", "Balancing-Watchdog (PV"),
    (6, "balancing_watchdog_netz",
     {"sensor.opti_balancing_watchdog": "netz"},
     "Akku Netzladen", "Balancing-Watchdog (Netz"),
    (7, "ladedeckel",
     # BASIS: maxsoc 95, peak_reserve_aktiv off, watchdog aus, EV-Sperre aus ->
     # soc 96 >= maxsoc trifft den Deckel. Der EV-Guard-Fall (Sperre an ->
     # Deckel uebersprungen) liegt in test_ev_sperre.py (akku_voll_gesperrt).
     {"sensor.opti_soc": "96"},
     "Akku nur Entladen", "Ladedeckel"),
    (8, "soc20_prognose",
     {"sensor.opti_soc": "18", "sensor.opti_forecast_score": "1",
      "sensor.opti_forecast_score_tomorrow": "1", "sensor.opti_price_level": "NORMAL"},
     "Akku nur Laden", "SOC<20"),
    (9, "soc75_prognose",
     {"sensor.opti_soc": "50", "sensor.opti_forecast_score": "1",
      "sensor.opti_forecast_score_tomorrow": "1", "sensor.opti_price_level": "NORMAL"},
     "Akku nur Laden", "SOC<75"),
    (10, "soc80_wintermodus",
     {"sensor.opti_soc": "78", "sensor.opti_forecast_score": "1",
      "sensor.opti_forecast_score_tomorrow": "1", "sensor.opti_price_level": "EXPENSIVE"},
     "Akku nur Laden", "SOC<80 Wintermodus"),
    (11, "soc15_notfall",
     {"sensor.opti_soc": "12", "sensor.opti_forecast_score": "1",
      "sensor.opti_price_level": "NORMAL"},
     "Akku nur Laden", "SOC<15 Notfall"),
    (12, "soc45_sehr_guenstig",
     {"sensor.opti_soc": "30", "sensor.opti_forecast_score": "1",
      "sensor.opti_price_level": "CHEAP"},
     "Akku nur Laden", "SOC<45"),
    (13, "ev_sperre_schattet_ueberschuss",
     # Fixture wuerde ohne Sperre den 70%-Ueberschuss-Zweig treffen (wie
     # ueberschuss_70) - mit Sperre muss der EV-Zweig davor greifen und
     # 'Akku nur Laden' liefern (PV-Laden bleibt im Sperr-Modus moeglich).
     {"sun.sun": "above_horizon", "sensor.opti_soc": "70", "sensor.opti_target_soc": "50",
      "binary_sensor.opti_ueberschuss_70_aktiv": "on",
      "input_boolean.opti_ev_akku_pause": "on",
      "binary_sensor.opti_ev_schnellladung": "on"},
     "Akku nur Laden", "EV-Sperre"),
    (14, "ueberschuss_70",
     {"sun.sun": "above_horizon", "sensor.opti_soc": "70", "sensor.opti_target_soc": "50",
      "binary_sensor.opti_ueberschuss_70_aktiv": "on"},
     "Akku Dynamisch", "70% Ueberschuss"),
    (15, "ueberschuss_ac",
     {"sun.sun": "above_horizon", "sensor.opti_soc": "70", "sensor.opti_target_soc": "50",
      "binary_sensor.opti_ueberschuss_ac_aktiv": "on"},
     "Akku Dynamisch", "AC Ueberschuss"),
    (16, "akku_voll",
     # Seit dem Ladedeckel ist 'Akku voll' bei soc >= maxsoc geschattet (der
     # Deckel bei Index 7 gewinnt und entlaedt). Erreichbar bleibt der Zweig,
     # wenn der Deckel gegated ist - hier via aktiver Peak-Reserve (L1-L4
     # greifen bei NORMAL-Preis nicht).
     {"sensor.opti_soc": "100", "binary_sensor.opti_peak_reserve_aktiv": "on",
      "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0)},
     "Akku Dynamisch", "Akku voll"),
    (17, "leiter_l3_halten",
     {**LEITER_BASE, "sensor.opti_soc": "31", "sensor.opti_price_level": "EXPENSIVE",
      "input_boolean.opti_prognose_netzladen": "off",
      "input_number.opti_halte_spread_ct": "3",
      "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0, ve_avg=55.0)},
     "Akku nur Laden", "Peak-Leiter L3"),
    (18, "leiter_l4_halten",
     {**LEITER_BASE, "sensor.opti_soc": "40", "sensor.opti_price_level": "NORMAL",
      "input_boolean.opti_prognose_netzladen": "off",
      "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0)},
     "Akku nur Laden", "Peak-Leiter L4"),
    (19, "dyn_bis_ziel",
     {"sun.sun": "above_horizon", "sensor.opti_soc": "40", "sensor.opti_target_soc": "60"},
     "Akku Dynamisch", "dyn bis Ziel"),
    (20, "ueber_ziel_soc",
     {"sensor.opti_soc": "70", "sensor.opti_target_soc": "60"},
     "Akku nur Entladen", "ueber Ziel-SoC"),
]

DEFAULT_BRANCH = ("default", "default_nacht", {}, "Akku Dynamisch", "Default")


@pytest.mark.parametrize(
    "zweig,name,overrides,erw_modus,erw_grund",
    BRANCHES + [DEFAULT_BRANCH],
    ids=[b[1] for b in BRANCHES] + [DEFAULT_BRANCH[1]],
)
def test_paritaet_pro_zweig(zweig, name, overrides, erw_modus, erw_grund):
    hass = _make_hass(overrides)

    auto_zweig, auto_modus = _evaluate_automation(hass)
    vor_modus = _vorschau(hass, "state")
    vor_grund = _vorschau(hass, "grund")

    # 1) Beide Seiten setzen denselben Modus (Kern der Paritaet).
    assert auto_modus == vor_modus, (
        f"[{name}] Automation setzt {auto_modus!r}, Vorschau {vor_modus!r}")
    assert vor_modus == erw_modus, (
        f"[{name}] erwarteter Modus {erw_modus!r}, war {vor_modus!r}")

    # 2) Der getroffene Zweig ist der beabsichtigte (Index == grund-Position).
    assert auto_zweig == zweig, (
        f"[{name}] Automation traf Zweig {auto_zweig!r}, erwartet {zweig!r}")
    assert erw_grund in vor_grund, (
        f"[{name}] Vorschau-grund {vor_grund!r} enthaelt nicht {erw_grund!r}")


def test_paritaet_gate_unavailable_fail_closed():
    # Die Gates (opti_prognose_netzladen etc.) sind in der Automation
    # state:"on"-Conditions und fallen bei unavailable ZU (Zweig wird
    # uebersprungen). Die Vorschau muss dasselbe tun - ihr frueheres
    # fail-open ("!= 'off'") liess sie hier den SOC<20-Zweig anzeigen,
    # den die Automation nie geschaltet haette.
    hass = _make_hass({
        "sensor.opti_soc": "18",
        "sensor.opti_forecast_score": "1",
        "sensor.opti_forecast_score_tomorrow": "1",
        "sensor.opti_price_level": "NORMAL",
        "input_boolean.opti_prognose_netzladen": "unavailable",
    })
    auto_zweig, auto_modus = _evaluate_automation(hass)
    assert auto_zweig == "default"
    assert auto_modus == "Akku Dynamisch"
    assert _vorschau(hass, "state") == auto_modus


def test_struktur_alle_choose_optionen_abgedeckt():
    """Harter Fail, wenn jemand eine choose-Option ergaenzt/entfernt, ohne den
    Test mitzuziehen: Zahl der choose-Optionen == Zahl der getesteten Zweige."""
    assert len(CHOOSE_OPTIONS) == len(BRANCHES), (
        f"{len(CHOOSE_OPTIONS)} choose-Optionen, aber {len(BRANCHES)} Test-Zweige - "
        "neue/entfernte Option in opti_strategie.yaml nicht im Paritaets-Test gespiegelt")
    # Zweig-Indizes lueckenlos 0..N-1 (kein Tippfehler in der BRANCHES-Tabelle).
    assert [b[0] for b in BRANCHES] == list(range(len(BRANCHES)))
