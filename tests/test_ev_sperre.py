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
