"""Regressionstests fuer den Legacy-Template-Layer (sma_templates.yaml).

Der Layer laeuft live parallel zum Canonical-Layer; hier werden nur die
robustheitskritischen Randfaelle gepinnt, keine volle Logik-Abdeckung.
"""
import datetime as dt

from .ha_harness import REPO, TZ, FakeHass, find_template_entity, load_yaml, render


def _target_soc_entity():
    cfg = load_yaml(REPO / "packages" / "sma_templates.yaml")
    return find_template_entity(cfg, "sensor", "akku_target_soc_intelligent")


def _target_soc_state(hass):
    return render(hass, _target_soc_entity()["state"])


def _target_soc_attr(hass, attr):
    return render(hass, _target_soc_entity()["attributes"][attr])


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
    # Auch die Attribute rechnen net_available/cap_kwh separat - gleicher Guard.
    assert float(_target_soc_attr(hass, "ratio")) == 0.0
    assert "maxsoc" in _target_soc_attr(hass, "branch")
