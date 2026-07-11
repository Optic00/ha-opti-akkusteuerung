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
