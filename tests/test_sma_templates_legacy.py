"""Regressionstests fuer den Legacy-Template-Layer (sma_templates.yaml).

Der Layer laeuft live parallel zum Canonical-Layer; hier werden nur die
robustheitskritischen Randfaelle gepinnt, keine volle Logik-Abdeckung.
"""
import datetime as dt

from .ha_harness import REPO, TZ, FakeHass, find_template_entity, load_yaml, render


def _target_soc_state(hass):
    cfg = load_yaml(REPO / "packages" / "sma_templates.yaml")
    entity = find_template_entity(cfg, "sensor", "akku_target_soc_intelligent")
    return render(hass, entity["state"])


def test_legacy_target_soc_cap_null_faellt_auf_maxsoc():
    # Modbus-Kapazitaetssensor verfuegbar, liefert aber 0 (z.B. WR-Startphase):
    # frueher Division durch 0 (Template-Fehler -> Sensor unavailable), jetzt
    # Fallback auf maxsoc wie beim Canonical-Nachfolger opti_target_soc.
    hass = FakeHass(
        states={
            "sensor.sma_stp_se_40187_batterie_nennkapazitaet": "0",
            "sensor.solcast_pv_forecast_prognose_verbleibende_leistung_heute": "5",
            "sensor.haus_stromverbrauch_60_min": "500",
            "input_number.maxsoc": "95",
            "input_number.minsoc": "10",
            "input_boolean.hausakku_aus_netz_laden": "off",
        },
    )
    assert float(_target_soc_state(hass)) == 95.0
