"""Preisreihen-Halter (opti_derived.yaml Abschnitt 8b).

Ueberbrueckt kurze Ausfaelle des Preisreihen-Anbieters, damit die Strategie nicht
in den Default-Zweig kippt (Live-Befund 23./24.07.2026: bei einem Tibber-REST-
Timeout verloren today/tomorrow ihren Inhalt, opti_price_level und die
Peak-Reserve brachen gleichzeitig weg, der Modus sprang zwischen Peak-Leiter und
'Akku Dynamisch').

Zwei Entwurfsentscheidungen tragen die Tests hier:

1. HALTEFENSTER 15 min. Die beobachteten Ausfaelle dauerten 20-80 s. Ein kurzes
   Fenster ueberbrueckt sie vollstaendig und kann eine Tagesgrenze konstruktiv
   nicht ueberschreiten - damit entfaellt jede Datumslogik. Ein laengerer Ausfall
   endet fail-closed.
2. GEDAECHTNIS getrennt von der NUTZLAST. anker_* ueberlebt jeden Zustand,
   today/tomorrow gehen im Zustand 'leer' auf []. Lagen beide auf denselben
   Attributen, zerstoerte 'leer' das Gedaechtnis und der Halter verlor es nach
   einem Tick - ein Fehler, den nur Mehr-Tick-Sequenzen sichtbar machen.
"""
from __future__ import annotations

import datetime as dt

from .ha_harness import (REPO, TZ, FakeHass, find_template_entity, load_yaml,
                         render, render_native)

JETZT = dt.datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
TS = JETZT.timestamp()
REIHE = [30.0 + i for i in range(24)]
MORGEN_REIHE = [40.0 + i for i in range(24)]


def _entity():
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    return find_template_entity(cfg, "sensor", "opti_price_series_stable")


def _hass(today, tomorrow=None, *, anker=None, anker_morgen=None, anker_ts=None,
          now=JETZT):
    """Der vorherige this-Snapshot (RestoreEntity) liegt auf anker_*, nicht auf
    der Nutzlast - genau diese Trennung ist der Kern des Halters."""
    attrs = {}
    if anker is not None:
        attrs["anker_today"] = anker
    if anker_morgen is not None:
        attrs["anker_tomorrow"] = anker_morgen
    if anker_ts is not None:
        attrs["anker_ts"] = anker_ts
    return FakeHass(
        attrs={"sensor.opti_price_series": {"today": today,
                                            "tomorrow": tomorrow or []}},
        now=now,
        this_attributes=attrs,
    )


def _state(hass):
    return render(hass, _entity()["state"])


def _attr(hass, name):
    return render_native(hass, _entity()["attributes"][name])


def _tick(quelle_today, quelle_tomorrow=None, vorher=None, now=JETZT):
    """Ein Render-Durchlauf, der den vollstaendigen Attribut-Snapshot des
    Vortricks als this einspeist. Nur so werden Fehler sichtbar, die erst im
    Folgetick entstehen."""
    vorher = vorher or {}
    hass = FakeHass(
        attrs={"sensor.opti_price_series": {"today": quelle_today,
                                            "tomorrow": quelle_tomorrow or []}},
        now=now,
        this_attributes={k: v for k, v in vorher.items() if k != "state"},
    )
    ergebnis = {"state": _state(hass)}
    for name in ("today", "tomorrow", "anker_today", "anker_tomorrow",
                 "anker_ts", "gehalten_teil"):
        ergebnis[name] = _attr(hass, name)
    return ergebnis


# --- Normalbetrieb ---------------------------------------------------------

def test_frische_reihe_wird_uebernommen():
    hass = _hass(REIHE, MORGEN_REIHE)
    assert _state(hass) == "frisch"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "tomorrow") == MORGEN_REIHE
    assert _attr(hass, "anker_ts") == round(TS)


def test_frisch_ueberschreibt_alten_anker():
    hass = _hass(REIHE, anker=[1.0] * 24, anker_ts=TS - 60)
    assert _state(hass) == "frisch"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "anker_today") == REIHE


def test_leeres_tomorrow_ist_kein_ausfall():
    # Vor der Day-Ahead-Veroeffentlichung ist tomorrow legitim leer.
    hass = _hass(REIHE, [])
    assert _state(hass) == "frisch"
    assert _attr(hass, "tomorrow") == []


# --- Der eigentliche Zweck: kurzen Ausfall ueberbruecken -------------------

def test_leere_reihe_wird_im_fenster_gehalten():
    hass = _hass([], anker=REIHE, anker_morgen=MORGEN_REIHE, anker_ts=TS - 60)
    assert _state(hass) == "gehalten"
    assert _attr(hass, "gehalten_teil") == "reihe"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "tomorrow") == MORGEN_REIHE


def test_anker_ts_laeuft_beim_halten_nicht_mit():
    """Sonst wanderte der Stempel bei jedem Tick mit und das Fenster liefe nie
    ab - der Halter wuerde die Reihe unbegrenzt festhalten."""
    hass = _hass([], anker=REIHE, anker_ts=TS - 600)
    assert _attr(hass, "anker_ts") == round(TS - 600)


def test_haltefenster_laeuft_ab():
    """Die harte Schranke: nach 15 Minuten endet die Ueberbrueckung fail-closed,
    statt eine immer aeltere Reihe weiterzureichen."""
    knapp_drin = _hass([], anker=REIHE, anker_morgen=MORGEN_REIHE,
                       anker_ts=TS - 890)
    assert _state(knapp_drin) == "gehalten"

    abgelaufen = _hass([], anker=REIHE, anker_morgen=MORGEN_REIHE,
                       anker_ts=TS - 910)
    assert _state(abgelaufen) == "leer"
    assert _attr(abgelaufen, "today") == []
    assert _attr(abgelaufen, "tomorrow") == []


def test_ohne_anker_ts_wird_nichts_gehalten():
    # Erststart ohne Restore: kein Zeitstempel, also keine Halte-Berechtigung.
    hass = _hass([], anker=REIHE)
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []


def test_unbrauchbarer_anker_wird_nicht_gehalten():
    hass = _hass([], anker=[1.0, 2.0], anker_ts=TS - 60)
    assert _state(hass) == "leer"


def test_zu_kurze_reihe_gilt_nicht_als_frisch():
    hass = _hass([30.0, 31.0], anker=REIHE, anker_ts=TS - 60)
    assert _state(hass) == "gehalten"
    assert _attr(hass, "today") == REIHE


def test_nicht_numerische_reihe_gilt_nicht_als_frisch():
    hass = _hass(["a", "b", None, "c"], anker=REIHE, anker_ts=TS - 60)
    assert _state(hass) == "gehalten"
    assert _attr(hass, "today") == REIHE


def test_fehlendes_quellattribut_ist_kein_absturz():
    hass = FakeHass(attrs={}, now=JETZT, this_attributes={})
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []


# --- tomorrow: Teilausfall vs. Tageswechsel -------------------------------

def test_bekanntes_tomorrow_wird_nicht_ueberschrieben():
    """Faellt nur die Morgen-Liste kurz weg, waehrend today gueltig bleibt, darf
    ein bereits bekanntes tomorrow nicht mit [] ueberschrieben werden - sonst
    springen Perzentil und Peak-Reserve. Der Teilausfall MUSS als 'gehalten'
    sichtbar sein, sonst zaehlen ihn die history_stats nicht."""
    hass = _hass(REIHE, [], anker=REIHE, anker_morgen=MORGEN_REIHE,
                 anker_ts=TS - 60)
    assert _state(hass) == "gehalten"
    assert _attr(hass, "gehalten_teil") == "morgen"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "tomorrow") == MORGEN_REIHE


def test_frisches_tomorrow_schlaegt_den_anker():
    aktuell = [99.0] * 24
    hass = _hass(REIHE, aktuell, anker=REIHE, anker_morgen=MORGEN_REIHE,
                 anker_ts=TS - 60)
    assert _attr(hass, "tomorrow") == aktuell
    assert _attr(hass, "anker_tomorrow") == aktuell


def test_tageswechsel_verwirft_das_gestrige_tomorrow():
    """Wechselt die today-Liste, ist ein neuer Tag angebrochen: das gestrige
    tomorrow SIND die heutigen Preise und darf nicht als 'morgen' weiterleben."""
    neue_reihe = [50.0 + i for i in range(24)]
    hass = _hass(neue_reihe, [], anker=REIHE, anker_morgen=MORGEN_REIHE,
                 anker_ts=TS - 60)
    assert _state(hass) == "frisch"
    assert _attr(hass, "tomorrow") == []
    assert _attr(hass, "anker_tomorrow") == []


def test_halten_ist_alles_oder_nichts():
    """Unbrauchbarer today-Anker darf tomorrow nicht durchlassen: sonst
    berechnete opti_price_level ein Preisniveau allein aus MORGEN-Preisen,
    obwohl der Halter 'leer' meldet."""
    hass = _hass([], anker=[1.0, 2.0], anker_morgen=MORGEN_REIHE,
                 anker_ts=TS - 60)
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []
    assert _attr(hass, "tomorrow") == []


# --- Mehr-Tick-Sequenzen ---------------------------------------------------

def test_anker_ueberleben_den_zustand_leer():
    """Der Kern der Nutzlast/Gedaechtnis-Trennung: auch nach mehreren
    Leer-Ticks muss der Anker noch da sein, damit eine zurueckkehrende Quelle
    nicht als voellig neu behandelt wird."""
    zustand = {"anker_today": REIHE, "anker_tomorrow": MORGEN_REIHE,
               "anker_ts": round(TS - 1000)}
    for runde in range(4):
        zustand = _tick([], [], vorher=zustand)
        assert zustand["state"] == "leer", f"Tick {runde}"
        assert zustand["today"] == [], f"Tick {runde}"
        assert zustand["anker_today"] == REIHE, f"Anker weg in Tick {runde}"
        assert zustand["anker_tomorrow"] == MORGEN_REIHE, f"Tick {runde}"
        assert zustand["anker_ts"] == round(TS - 1000), f"Tick {runde}"


def test_normalbetrieb_ueber_mehrere_ticks():
    """Frisch -> kurzer Ausfall wird gehalten -> Quelle kehrt zurueck."""
    zustand = _tick(REIHE, MORGEN_REIHE)
    assert zustand["state"] == "frisch"
    zustand = _tick([], [], vorher=zustand)
    assert zustand["state"] == "gehalten"
    assert zustand["today"] == REIHE and zustand["tomorrow"] == MORGEN_REIHE
    zustand = _tick([], [], vorher=zustand)
    assert zustand["state"] == "gehalten", "auch der zweite Ausfall-Tick haelt"
    zustand = _tick(REIHE, MORGEN_REIHE, vorher=zustand)
    assert zustand["state"] == "frisch"
    assert zustand["anker_ts"] == round(TS)


def test_langer_ausfall_kippt_ins_fail_closed():
    """Die Sequenz, die das Haltefenster begrenzt: Ausfall-Ticks laufen weiter,
    bis das Fenster ueberschritten ist - dann leer statt immer aelterer Reihe."""
    zustand = _tick(REIHE, MORGEN_REIHE, now=JETZT)
    spaeter = JETZT + dt.timedelta(minutes=10)
    zustand = _tick([], [], vorher=zustand, now=spaeter)
    assert zustand["state"] == "gehalten"
    viel_spaeter = JETZT + dt.timedelta(minutes=20)
    zustand = _tick([], [], vorher=zustand, now=viel_spaeter)
    assert zustand["state"] == "leer"
    assert zustand["today"] == []
    # Und die Quelle kehrt zurueck: sofort wieder frisch.
    zustand = _tick(REIHE, MORGEN_REIHE, vorher=zustand, now=viel_spaeter)
    assert zustand["state"] == "frisch"


# --- Kette: Halter -> price_level -----------------------------------------

def test_gehaltene_reihe_traegt_das_preisniveau():
    """Der Zweck der ganzen Uebung: waehrend eines kurzen Quellausfalls bleibt
    opti_price_level verfuegbar und behaelt sein Niveau, statt unavailable zu
    werden und die Strategie in den Default-Zweig zu schicken."""
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    level = find_template_entity(cfg, "sensor", "opti_price_level")

    halter = _hass([], anker=REIHE, anker_ts=TS - 60)
    gehalten = _attr(halter, "today")
    assert gehalten == REIHE

    hass = FakeHass(
        states={"sensor.opti_price_current_ct_kwh": "53.0"},
        attrs={"sensor.opti_price_series_stable": {"today": gehalten,
                                                   "tomorrow": []}},
        now=JETZT,
    )
    assert render(hass, level["availability"]) == "True"
    assert render(hass, level["state"]) == "VERY_EXPENSIVE"


def test_abgelaufener_halter_macht_preisniveau_unavailable():
    """Die Gegenprobe: nach dem Haltefenster ist die Nutzlast leer und das
    Preisniveau faellt fail-closed aus, statt ein Niveau zu erfinden."""
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    level = find_template_entity(cfg, "sensor", "opti_price_level")

    halter = _hass([], anker=REIHE, anker_ts=TS - 1000)
    assert _state(halter) == "leer"

    hass = FakeHass(
        states={"sensor.opti_price_current_ct_kwh": "53.0"},
        attrs={"sensor.opti_price_series_stable": {
            "today": _attr(halter, "today"), "tomorrow": []}},
        now=JETZT,
    )
    assert render(hass, level["availability"]) == "False"
    assert render(hass, level["state"]) == "unavailable"
