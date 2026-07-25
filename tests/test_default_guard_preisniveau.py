"""Default-Guard bei fehlendem Preisniveau (Live-Befund 23./24.07.2026).

BEFUND: Bei kurzen Ausfaellen der Preisreihen-Quelle (Tibber-REST-Timeout)
verstummen alle preisabhaengigen Zweige gleichzeitig. Der Default-Zweig
ueberschrieb den Modus daraufhin mit 'Akku Dynamisch'; beim naechsten
erfolgreichen Poll sprang er zurueck. Gemessen: ~15 Episoden in 7 Tagen, teils
mit 20-40 s Verweildauer.

Das Fail-closed von opti_price_level (unavailable statt NORMAL) behebt die
falsche Zweig-Berechtigung, aber NICHT das Flattern - 'unavailable' fuehrt in
genau denselben Default. Der Guard haelt deshalb die ENTSCHEIDUNG statt der
Daten: der Modus-Select ist der Zustand und bleibt stehen, bis wieder ein
Preisniveau vorliegt.

Die Tests pruefen beides: dass der Default schweigt, und dass die
preisUNabhaengigen Zweige weiter greifen - ein Guard, der die Strategie
lahmlegt, waere schlimmer als das Flattern.
"""
from __future__ import annotations

import pytest

from .condition_eval import evaluate_condition
from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render
from .test_strategie_paritaet import CHOOSE_OPTIONS, MAIN_ACTION, _mode_from_sequence
from .test_strategie_vorschau import BASIS, reserve_attrs

PRICE = "sensor.opti_price_level"
MODUS = "input_select.akkusteuerung_modus"


def _hass(overrides):
    overrides = dict(overrides)
    attrs = overrides.pop("_attrs", None) or {}
    states = dict(BASIS)
    states.update(overrides)
    return FakeHass(states=states, attrs=attrs)


def _entscheidung(hass):
    """Wertet die echte choose-Kette aus. Liefert (alias, modus); modus ist None,
    wenn KEIN Zweig greift - dann bleibt der Modus in HA unveraendert."""
    for option in CHOOSE_OPTIONS:
        if all(evaluate_condition(hass, c) for c in option.get("conditions", [])):
            return option.get("alias"), _mode_from_sequence(option["sequence"])
    for step in MAIN_ACTION.get("default", []) or []:
        for option in step.get("choose", []) or []:
            if all(evaluate_condition(hass, c) for c in option.get("conditions", [])):
                return "default", _mode_from_sequence(option["sequence"])
    return None, None


def _vorschau(hass, teil="state"):
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    entity = find_template_entity(cfg, "sensor", "opti_strategie_vorschau")
    tpl = entity["state"] if teil == "state" else entity["attributes"]["grund"]
    return render(hass, tpl)


# --- Der Kern: Default schweigt ohne Preisniveau ---------------------------

@pytest.mark.parametrize("zustand", ["unavailable", "unknown"])
def test_default_haelt_passiven_modus_ohne_preisniveau(zustand):
    """Nachts, SoC im neutralen Band: vorher landete das im Default und
    ueberschrieb den Modus mit 'Akku Dynamisch'."""
    hass = _hass({PRICE: zustand, "sensor.opti_soc": "60",
                  "sensor.opti_target_soc": "95", "sun.sun": "below_horizon",
                  MODUS: "Akku nur Entladen"})
    alias, modus = _entscheidung(hass)
    assert modus is None, f"Zweig '{alias}' setzt trotz fehlendem Preisniveau"


@pytest.mark.parametrize("passiv", ["Akku Dynamisch", "Akku nur Entladen"])
def test_nur_passive_modi_werden_gehalten(passiv):
    hass = _hass({PRICE: "unavailable", "sensor.opti_soc": "60",
                  "sensor.opti_target_soc": "95", "sun.sun": "below_horizon",
                  MODUS: passiv})
    assert _entscheidung(hass)[1] is None


@pytest.mark.parametrize("zwang", ["Akku Netzladen", "Akku nur Laden",
                                   "Akku Pause", "Akku schnell Laden"])
def test_zwangsmodi_fallen_ohne_preisniveau_auf_dynamisch(zwang):
    """Review-Finding 25.07.2026: ein bereits aktives 'Akku Netzladen' blieb
    nach Wegfall seiner Preisberechtigung stehen und kaufte weiter Netzstrom zu
    inzwischen beliebig hohem Preis - beendet erst durch den maxsoc-Ladedeckel.
    'Akku nur Laden' sperrt die Entladung (Haus laeuft aus dem Netz), 'Akku
    Pause' legt den Akku still. Diese Modi verlieren mit dem Preisniveau ihre
    Begruendung und muessen zurueckfallen; ein Modus-Wechsel ist hier das
    kleinere Uebel als ein eingefrorener Zwangszustand."""
    hass = _hass({PRICE: "unavailable", "sensor.opti_soc": "60",
                  "sensor.opti_target_soc": "95", "sun.sun": "below_horizon",
                  MODUS: zwang})
    alias, modus = _entscheidung(hass)
    assert alias == "default"
    assert modus == "Akku Dynamisch", f"'{zwang}' bleibt haengen"


def test_netzladen_kauft_nicht_weiter_bei_teurem_skalarpreis():
    """Die reproduzierte Situation des Findings: Preisniveau weg, Skalarpreis
    60 ct, SoC 40 % - Netzladen darf NICHT weiterlaufen."""
    hass = _hass({PRICE: "unavailable",
                  "sensor.opti_price_current_ct_kwh": "60",
                  "sensor.opti_soc": "40", "sensor.opti_target_soc": "95",
                  "sun.sun": "below_horizon", MODUS: "Akku Netzladen"})
    alias, modus = _entscheidung(hass)
    assert modus == "Akku Dynamisch"
    assert _vorschau(hass) == "Akku Dynamisch"


def test_default_greift_mit_gueltigem_preisniveau_weiter():
    """Gegenprobe: der Guard darf den Default nicht generell lahmlegen."""
    hass = _hass({PRICE: "NORMAL", "sensor.opti_soc": "60",
                  "sensor.opti_target_soc": "95", "sun.sun": "below_horizon"})
    alias, modus = _entscheidung(hass)
    assert alias == "default"
    assert modus == "Akku Dynamisch"


def test_flatter_szenario_aus_dem_livebefund():
    """Die konkrete Sequenz vom 23.07.2026, 22:05-22:22, mit den echten
    Live-Werten: SoC 78 %, Ziel-SoC 95 %, nachts, Preis VERY_EXPENSIVE.
    Vorher lief das als L1 -> Quellausfall -> Default(Dynamisch) -> L1.
    Entscheidend fuer die Aussagekraft: SoC muss UNTER dem Ziel-SoC liegen,
    sonst faengt der Zweig 'ueber Ziel-SoC' den Fall ab und der Default wird
    nie erreicht - der Test waere dann wertlos."""
    leiter = {"sensor.opti_forecast_score": "1",
              "sensor.opti_forecast_score_tomorrow": "1",
              "sensor.opti_peak_reserve_soc": "20",
              "binary_sensor.opti_peak_reserve_aktiv": "on",
              "sensor.opti_price_current_ct_kwh": "42.4",
              "sensor.opti_soc": "78",
              "sensor.opti_target_soc": "95",
              "sun.sun": "below_horizon",
              "_attrs": reserve_attrs(ve=30.0, min_vor=40.0, avg=200.0)}

    vorher = _hass({**leiter, PRICE: "VERY_EXPENSIVE"})
    alias, modus = _entscheidung(vorher)
    assert alias is not None and "Peak-Leiter L1" in alias
    assert modus == "Akku nur Entladen"

    # Quellausfall: Preisniveau UND Peak-Reserve fallen gemeinsam weg - genau so
    # war es live zu sehen.
    luecke = _hass({**leiter, PRICE: "unavailable",
                    "sensor.opti_peak_reserve_soc": "unavailable",
                    "binary_sensor.opti_peak_reserve_aktiv": "off",
                    MODUS: "Akku nur Entladen"})
    alias_luecke, modus_luecke = _entscheidung(luecke)
    assert modus_luecke is None, (
        f"Zweig '{alias_luecke}' setzt '{modus_luecke}' und erzeugt das "
        "Flattern erneut")

    # Und nach dem Ausfall greift L1 sofort wieder.
    danach = _hass({**leiter, PRICE: "VERY_EXPENSIVE",
                    MODUS: "Akku nur Entladen"})
    alias_danach, modus_danach = _entscheidung(danach)
    assert "Peak-Leiter L1" in alias_danach
    assert modus_danach == "Akku nur Entladen"


# --- Der Guard darf die Sicherheitszweige nicht lahmlegen ------------------

def test_minsoc_schutz_greift_ohne_preisniveau():
    hass = _hass({PRICE: "unavailable", "sensor.opti_soc": "3",
                  "input_number.minsoc": "10"})
    alias, modus = _entscheidung(hass)
    assert "MinSOC-Schutz" in alias
    assert modus == "Akku nur Laden"


def test_ladedeckel_greift_ohne_preisniveau():
    hass = _hass({PRICE: "unavailable", "sensor.opti_soc": "97",
                  "input_number.maxsoc": "95"})
    alias, modus = _entscheidung(hass)
    assert "Ladedeckel" in alias
    assert modus == "Akku nur Entladen"


def test_ueber_ziel_soc_entlaedt_ohne_preisniveau():
    """Wichtig fuer den Nachtbetrieb: ohne Preisniveau wird weiter entladen,
    solange der SoC ueber dem Ziel liegt - der Guard friert nicht alles ein."""
    hass = _hass({PRICE: "unavailable", "sensor.opti_soc": "80",
                  "sensor.opti_target_soc": "60", "sun.sun": "below_horizon"})
    alias, modus = _entscheidung(hass)
    assert "ueber Ziel-SoC" in alias.lower() or "Ziel-SoC" in alias
    assert modus == "Akku nur Entladen"


def test_balancing_watchdog_greift_ohne_preisniveau():
    hass = _hass({PRICE: "unavailable", "sensor.opti_balancing_watchdog": "pv"})
    alias, modus = _entscheidung(hass)
    assert "Balancing-Watchdog" in alias
    assert modus == "Akku nur Laden"


# --- Paritaet: die Vorschau muss dasselbe sagen ----------------------------

def test_vorschau_spiegelt_den_gehaltenen_modus():
    """Der Vorschau-Sensor darf keinen Wechsel behaupten, den die Automation
    nicht macht - sonst laufen Soll und Ist auseinander."""
    hass = _hass({PRICE: "unavailable", "sensor.opti_soc": "60",
                  "sensor.opti_target_soc": "95", "sun.sun": "below_horizon",
                  MODUS: "Akku nur Entladen"})
    assert _vorschau(hass) == "Akku nur Entladen"
    assert "passiver Modus gehalten" in _vorschau(hass, "grund")


def test_vorschau_default_unveraendert_mit_preisniveau():
    hass = _hass({PRICE: "NORMAL", "sensor.opti_soc": "60",
                  "sensor.opti_target_soc": "95", "sun.sun": "below_horizon"})
    assert _vorschau(hass) == "Akku Dynamisch"
    assert "Default" in _vorschau(hass, "grund")


# --- Struktur: der Guard muss ueberhaupt ausgewertet werden ----------------

def test_modus_select_ist_trigger():
    """Review-Finding 25.07.2026: ohne den Modus-Select als Trigger wird der
    Guard NICHT ausgewertet, wenn ein Nutzer oder eine andere Automation einen
    Zwangsmodus setzt, waehrend das Preisniveau schon unavailable ist. Der
    Adapter uebernimmt ihn sofort, und er bliebe bis zum naechsten beliebigen
    Strategie-Trigger aktiv - waehrend die Vorschau bereits 'Akku Dynamisch'
    zeigt. Die uebrigen Tests hier werten die choose-Kette direkt aus und
    koennen das nicht sehen, deshalb dieser Strukturtest."""
    cfg = load_yaml(REPO / "automations" / "opti_strategie.yaml")
    haupt = next(a for a in cfg if a["id"] == "opti_canonical_strategie")
    entitaeten = set()
    for trigger in haupt["triggers"]:
        eid = trigger.get("entity_id")
        entitaeten.update(eid if isinstance(eid, list) else [eid] if eid else [])
    assert MODUS in entitaeten, "Modus-Select fehlt als Trigger"


def test_modus_trigger_hat_keine_to_einschraenkung():
    """Der Trigger muss JEDEN Moduswechsel sehen - eine to:-Einschraenkung wuerde
    genau die extern gesetzten Zwangsmodi wieder durchlassen."""
    cfg = load_yaml(REPO / "automations" / "opti_strategie.yaml")
    haupt = next(a for a in cfg if a["id"] == "opti_canonical_strategie")
    modus_trigger = [t for t in haupt["triggers"]
                     if MODUS in (t.get("entity_id") if isinstance(t.get("entity_id"), list)
                                  else [t.get("entity_id")])]
    assert modus_trigger, "kein Trigger auf dem Modus-Select"
    for t in modus_trigger:
        assert "to" not in t and "from" not in t, f"eingeschraenkter Trigger: {t}"
