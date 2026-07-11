"""Tests fuer das KI-Analyse-Paket (Phase 1)."""
import json
import pathlib

import jinja2
import yaml

from .ha_harness import REPO, FakeHass, load_yaml, render

KI_PACKAGE = REPO / "packages" / "opti_ki_analyse.yaml"
KI_AUTOMATIONS = REPO / "automations" / "opti_ki_analyse.yaml"


def _walk_templates(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_templates(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_templates(v, f"{path}[{i}]")
    elif isinstance(node, str) and ("{{" in node or "{%" in node):
        yield path, node


def test_ki_package_jinja_parst():
    cfg = load_yaml(KI_PACKAGE)
    env = jinja2.Environment()
    fehler = [p for p, t in _walk_templates(cfg)
              if _parse_fails(env, t)]
    assert fehler == []


def test_mapping_example_jinja_parst():
    cfg = load_yaml(REPO / "opti_mapping.example.yaml")
    env = jinja2.Environment()
    fehler = [p for p, t in _walk_templates(cfg)
              if _parse_fails(env, t)]
    assert fehler == []


def _parse_fails(env, template_str):
    try:
        env.parse(template_str)
        return False
    except jinja2.TemplateSyntaxError:
        return True


def _analyse_automation():
    cfg = load_yaml(KI_AUTOMATIONS)
    return next(a for a in cfg if a["id"] == "opti_ki_analyse_taeglich")


def _datenpaket_template():
    auto = _analyse_automation()
    schritt = next(a for a in auto["actions"] if "variables" in a)
    return schritt["variables"]["datenpaket"]


STATES_VOLL = {
    "input_select.akkusteuerung_modus": "Akku Dynamisch",
    "sensor.opti_strategie_vorschau": "Akku Dynamisch",
    "sensor.opti_ki_dauer_nur_laden": "1.5",
    "sensor.opti_ki_starts_nur_laden": "2",
    "sensor.opti_ki_dauer_netzladen": "0.0",
    "sensor.opti_ki_starts_netzladen": "0",
    "sensor.opti_ki_dauer_nur_entladen": "6.25",
    "sensor.opti_ki_starts_nur_entladen": "3",
    "sensor.opti_ki_dauer_dynamisch": "13.0",
    "sensor.opti_ki_starts_dynamisch": "4",
    "sensor.opti_ki_preis_unavailable_h": "0.1",
    "sensor.opti_ki_forecast_unavailable_h": "0.0",
    "sensor.opti_ki_soc_min_24h": "38.0",
    "sensor.opti_ki_soc_max_24h": "92.0",
    "sensor.opti_ki_akku_ladung_heute": "9.8",
    "sensor.opti_ki_akku_entladung_heute": "7.2",
    "sensor.opti_price_spread_today_ct": "21.4",
    "sensor.opti_grid_import_today_kwh": "3.1",
    "sensor.opti_pv_yield_today_kwh": "118.0",
    "sensor.opti_forecast_today_kwh": "127.0",
    "sensor.opti_forecast_score": "9",
    "sensor.opti_forecast_score_tomorrow": "10",
    "sensor.opti_target_soc": "50",
    "input_number.opti_forecast_optimismus": "40",
    "input_number.opti_peak_min_aufschlag_ct": "5",
    "input_number.opti_halte_spread_ct": "5",
    "input_number.minsoc": "10",
    "input_number.maxsoc": "95",
    "sensor.byd_zellspreizung_ruhe": "2",
    "sensor.opti_ki_ruhe_spreizung_max_24h": "4",
    "sensor.byd_temperatur_spreizung": "3",
    "sensor.byd_soh": "96",
    "sensor.opti_ki_balancing_dauer_h": "1.2",
}


def test_datenpaket_rendert_valides_json():
    hass = FakeHass(states=STATES_VOLL,
                    attrs={"sensor.opti_strategie_vorschau": {"grund": "Default (Nacht/keine Aktion)"}})
    ergebnis = json.loads(render(hass, _datenpaket_template()))
    assert ergebnis["modi"]["nur_entladen"]["stunden"] == 6.25
    assert ergebnis["modi"]["nur_entladen"]["starts"] == 3
    assert ergebnis["datenqualitaet"]["preis_unavailable_min"] == 6
    assert ergebnis["akku"]["soc_min"] == 38.0
    assert ergebnis["byd"]["ruhe_spreizung_max_24h_mv"] == 4


def test_datenpaket_markiert_fehlende_quellen():
    ohne_optionales = {k: v for k, v in STATES_VOLL.items()
                       if not k.startswith(("sensor.opti_price_spread", "sensor.opti_grid_import",
                                            "sensor.opti_pv_yield", "sensor.byd_"))}
    hass = FakeHass(states=ohne_optionales,
                    attrs={"sensor.opti_strategie_vorschau": {"grund": "Default (Nacht/keine Aktion)"}})
    ergebnis = json.loads(render(hass, _datenpaket_template()))
    assert ergebnis["tag"]["preisspanne_ct"] == "nicht verfuegbar"
    assert ergebnis["byd"]["verfuegbar"] is False
