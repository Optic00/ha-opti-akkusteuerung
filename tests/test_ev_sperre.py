"""Tests EV-Schnelllade-Entladesperre (Spec 2026-07-11 rev. 3).

Drei Bloecke: (A) Mapping-Ladepunkt-Sensoren (privat, skipif), (B) Aggregations-/
Latch-Sensor in packages/opti_ev_sperre.yaml (Wahrheitstabelle der Spec),
(C) Leiter-Regression (Sperre blockiert Entladen/Dynamisch, Laden unberuehrt).
"""
from __future__ import annotations

import pytest

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

MAPPING_PATH = REPO / "packages" / "opti_mapping.yaml"

LP1_MODE = "select.evcc_bridge_garage_1_hinten_mode"
LP1_CHARGING = "binary_sensor.evcc_bridge_garage_1_hinten_charging"


def _lp1_state(hass):
    cfg = load_yaml(MAPPING_PATH)
    entity = find_template_entity(cfg, "binary_sensor", "opti_mapping_ev_lp1_schnell")
    return render(hass, entity["state"]), render(hass, entity["availability"])


@pytest.mark.skipif(not MAPPING_PATH.exists(),
                    reason="privates packages/opti_mapping.yaml nicht vorhanden (gitignored)")
@pytest.mark.parametrize("mode,charging,erwartet", [
    ("now", "on", "True"),
    ("minpv", "on", "True"),
    ("pv", "on", "False"),
    ("off", "off", "False"),
    ("now", "off", "False"),
])
def test_mapping_lp1_modusbasiert(mode, charging, erwartet):
    hass = FakeHass(states={LP1_MODE: mode, LP1_CHARGING: charging})
    state, avail = _lp1_state(hass)
    assert avail == "True"
    assert state == erwartet


@pytest.mark.skipif(not MAPPING_PATH.exists(),
                    reason="privates packages/opti_mapping.yaml nicht vorhanden (gitignored)")
def test_mapping_lp1_invalide_wird_unavailable():
    # Quelle fehlt -> availability False -> HA setzt unavailable (nicht off).
    hass = FakeHass(states={LP1_MODE: "unavailable", LP1_CHARGING: "on"})
    _state, avail = _lp1_state(hass)
    assert avail == "False"


# --- Block B: Aggregations-/Latch-Sensor (packages/opti_ev_sperre.yaml) ---

EV_AGG = "binary_sensor.opti_ev_schnellladung"
LP1 = "binary_sensor.opti_ev_lp1_schnell"
LP2 = "binary_sensor.opti_ev_lp2_schnell"


def _agg(part, states):
    cfg = load_yaml(REPO / "packages" / "opti_ev_sperre.yaml")
    entity = find_template_entity(cfg, "binary_sensor", "opti_ev_schnellladung")
    template = entity["state"] if part == "state" else entity["attributes"][part]
    return render(FakeHass(states=states), template)


# Latch-Wahrheitstabelle (Spec rev. 3). prev = Selbstreferenz auf den eigenen
# vorherigen Zustand (EV_AGG in den states). delay_off (300 s) haengt am
# Sensor-YAML und ist hier nicht simulierbar - getestet wird die Template-Logik.
@pytest.mark.parametrize("name,lp1,lp2,prev,erwartet", [
    ("ein_lp_valide_on_sofort_on",      "on",          "off",         "off",     "True"),
    ("beide_valide_off_wird_off",       "off",         "off",         "on",      "False"),
    ("invalide_haelt_on",               "unavailable", "off",         "on",      "True"),
    ("invalide_ohne_vorzustand_off",    "unavailable", "unavailable", "unknown", "False"),
    ("valide_on_schlaegt_invalide_lp2", "on",          "unavailable", "off",     "True"),
])
def test_latch_wahrheitstabelle(name, lp1, lp2, prev, erwartet):
    assert _agg("state", {LP1: lp1, LP2: lp2, EV_AGG: prev}) == erwartet


def test_haltegrund_gehalten_bei_invaliditaet():
    grund = _agg("haltegrund", {LP1: "unavailable", LP2: "off", EV_AGG: "on"})
    assert "gehalten" in grund


def test_haltegrund_aktiv_bei_ladung():
    grund = _agg("haltegrund", {LP1: "on", LP2: "off", EV_AGG: "off"})
    assert "aktiv" in grund


# --- Block C: Leiter-Regression (nutzt die Paritaets-Infrastruktur) ---

from .test_strategie_paritaet import _evaluate_automation, _make_hass, _vorschau
from .test_strategie_vorschau import reserve_attrs

SPERRE_AN = {"input_boolean.opti_ev_akku_pause": "on",
             "binary_sensor.opti_ev_schnellladung": "on"}

LEITER_BASE = {
    "sensor.opti_forecast_score": "1",
    "sensor.opti_forecast_score_tomorrow": "1",
    "sensor.opti_peak_reserve_soc": "45",
    "binary_sensor.opti_peak_reserve_aktiv": "on",
    "sensor.opti_price_current_ct_kwh": "50",
}


@pytest.mark.parametrize("name,overrides,erw_modus,erw_grund", [
    # L1/L2 werden durch die Sperre blockiert -> EV-Zweig faengt sie ab.
    ("l1_gesperrt",
     {**LEITER_BASE, **SPERRE_AN, "sensor.opti_soc": "85",
      "sensor.opti_price_level": "VERY_EXPENSIVE",
      "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0)},
     "Akku nur Laden", "EV-Sperre"),
    ("l2_gesperrt",
     {**LEITER_BASE, **SPERRE_AN, "sensor.opti_soc": "85",
      "sensor.opti_price_level": "EXPENSIVE",
      "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0)},
     "Akku nur Laden", "EV-Sperre"),
    # Akku voll + Sperre -> nur Laden statt Dynamisch (wuerde sonst entladen).
    ("akku_voll_gesperrt",
     {**SPERRE_AN, "sensor.opti_soc": "100"},
     "Akku nur Laden", "EV-Sperre"),
    # dyn bis Ziel / ueber Ziel / Default: alle gesperrt.
    ("dyn_bis_ziel_gesperrt",
     {**SPERRE_AN, "sun.sun": "above_horizon", "sensor.opti_soc": "40",
      "sensor.opti_target_soc": "60"},
     "Akku nur Laden", "EV-Sperre"),
    ("ueber_ziel_gesperrt",
     {**SPERRE_AN, "sensor.opti_soc": "70", "sensor.opti_target_soc": "60"},
     "Akku nur Laden", "EV-Sperre"),
    ("default_nacht_gesperrt",
     {**SPERRE_AN},
     "Akku nur Laden", "EV-Sperre"),
    # Negativtests: Lade-Zweige OBERHALB bleiben aktiv (Netzladen/Balancing).
    ("peak_vorladen_bleibt",
     {**SPERRE_AN, "sensor.opti_price_current_ct_kwh": "50",
      "sensor.opti_forecast_score": "1", "sensor.opti_forecast_score_tomorrow": "1",
      "sensor.opti_peak_reserve_soc": "35",
      "binary_sensor.opti_peak_reserve_aktiv": "on", "sensor.opti_soc": "15",
      "_attrs": reserve_attrs(ve=25.0, min_vor=50.0, avg=200.0)},
     "Akku Netzladen", "Peak-Vorladen"),
    ("balancing_netz_bleibt",
     {**SPERRE_AN, "sensor.opti_balancing_watchdog": "netz"},
     "Akku Netzladen", "Balancing-Watchdog (Netz"),
    ("minsoc_schutz_bleibt",
     {**SPERRE_AN, "sensor.opti_soc": "5"},
     "Akku nur Laden", "MinSOC-Schutz"),
    # Feature-Schalter aus -> Sperre wirkungslos, normale Leiter.
    ("toggle_aus_keine_sperre",
     {"binary_sensor.opti_ev_schnellladung": "on",
      "input_boolean.opti_ev_akku_pause": "off",
      "sensor.opti_soc": "70", "sensor.opti_target_soc": "60"},
     "Akku nur Entladen", "ueber Ziel-SoC"),
    # Sensor fehlt komplett (Repo-Nutzer ohne evcc) -> keine Sperre.
    ("sensor_fehlt_keine_sperre",
     {"input_boolean.opti_ev_akku_pause": "on",
      "binary_sensor.opti_ev_schnellladung": "unavailable",
      "sensor.opti_soc": "70", "sensor.opti_target_soc": "60"},
     "Akku nur Entladen", "ueber Ziel-SoC"),
])
def test_leiter_mit_ev_sperre(name, overrides, erw_modus, erw_grund):
    hass = _make_hass(overrides)
    _zweig, auto_modus = _evaluate_automation(hass)
    vor_modus = _vorschau(hass, "state")
    vor_grund = _vorschau(hass, "grund")
    assert auto_modus == vor_modus == erw_modus, (
        f"[{name}] Automation={auto_modus!r} Vorschau={vor_modus!r} erwartet={erw_modus!r}")
    assert erw_grund in vor_grund
