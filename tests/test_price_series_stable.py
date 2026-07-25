"""Preisreihen-Halter (opti_derived.yaml Abschnitt 8b).

Ueberbrueckt kurze Ausfaelle des Preisreihen-Anbieters, damit die Strategie
nicht in den Default-Zweig kippt (Live-Befund 23./24.07.2026). Die Tagesreihe
ist ein Fahrplan, kein Messwert - halten ist deshalb zulaessig, ABER nur
innerhalb desselben Kalendertags: today/tomorrow sind Index-Listen ohne
Zeitstempel, ueber Mitternacht gehalten wuerde 'today' stillschweigend zu
'gestern'. Genau diese Schranke pinnen die Tests hier.
"""
from __future__ import annotations

import datetime as dt

from .ha_harness import (REPO, TZ, FakeHass, find_template_entity, load_yaml,
                         render, render_native)

HEUTE = dt.datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
HEUTE_STR = "2026-01-15"
GESTERN_STR = "2026-01-14"
REIHE = [30.0 + i for i in range(24)]
MORGEN_REIHE = [40.0 + i for i in range(24)]


def _entity():
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    return find_template_entity(cfg, "sensor", "opti_price_series_stable")


def _hass(today, tomorrow=None, cache=None, cache_morgen=None, stand=None,
          now=HEUTE):
    """cache/stand modellieren den vorherigen this-Snapshot (RestoreEntity).
    Wichtig: das Gedaechtnis liegt auf anker_today/anker_tomorrow, NICHT auf der
    Nutzlast today/tomorrow - genau diese Trennung ist der Kern des Halters."""
    this_attrs = {}
    if cache is not None:
        this_attrs["anker_today"] = cache
    if cache_morgen is not None:
        this_attrs["anker_tomorrow"] = cache_morgen
    if stand is not None:
        this_attrs["stand"] = stand
    return FakeHass(
        attrs={"sensor.opti_price_series": {"today": today,
                                            "tomorrow": tomorrow or []}},
        now=now,
        this_attributes=this_attrs,
    )


def _tick(quelle_today, quelle_tomorrow=None, vorher=None, now=HEUTE):
    """Ein Render-Durchlauf. 'vorher' ist das Ergebnis-dict des letzten Ticks,
    sodass echte Mehr-Tick-Sequenzen gefahren werden koennen - ein Einzeltick
    verdeckt sonst Fehler, die erst im Folgetick auftreten."""
    vorher = vorher or {}
    hass = FakeHass(
        attrs={"sensor.opti_price_series": {"today": quelle_today,
                                            "tomorrow": quelle_tomorrow or []}},
        now=now,
        this_attributes={k: v for k, v in vorher.items() if k != "state"},
    )
    ergebnis = {"state": _state(hass)}
    for name in ("today", "tomorrow", "anker_today", "anker_tomorrow", "stand",
                 "gehalten_teil"):
        ergebnis[name] = _attr(hass, name)
    return ergebnis


def _state(hass):
    return render(hass, _entity()["state"])


def _attr(hass, name):
    return render_native(hass, _entity()["attributes"][name])


# --- Normalbetrieb ---------------------------------------------------------

def test_frische_reihe_wird_uebernommen():
    hass = _hass(REIHE, MORGEN_REIHE)
    assert _state(hass) == "frisch"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "tomorrow") == MORGEN_REIHE
    assert _attr(hass, "stand") == HEUTE_STR


def test_frisch_ueberschreibt_alten_cache():
    hass = _hass(REIHE, cache=[1.0] * 24, stand=HEUTE_STR)
    assert _state(hass) == "frisch"
    assert _attr(hass, "today") == REIHE


def test_leeres_tomorrow_ist_kein_ausfall():
    # Vor der Day-Ahead-Veroeffentlichung ist tomorrow legitim leer.
    hass = _hass(REIHE, [])
    assert _state(hass) == "frisch"
    assert _attr(hass, "tomorrow") == []


# --- Der eigentliche Zweck: Ausfall ueberbruecken --------------------------

def test_leere_reihe_haelt_cache_vom_selben_tag():
    hass = _hass([], cache=REIHE, cache_morgen=MORGEN_REIHE, stand=HEUTE_STR)
    assert _state(hass) == "gehalten"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "tomorrow") == MORGEN_REIHE


def test_stand_wird_beim_halten_nicht_fortgeschrieben():
    """Sonst wanderte der Stempel bei jedem Tick mit und ein Ausfall ueber
    Mitternacht wuerde nie verfallen."""
    hass = _hass([], cache=REIHE, stand=HEUTE_STR)
    assert _attr(hass, "stand") == HEUTE_STR


def test_halten_ueber_mehrere_ticks_bleibt_stabil():
    # Zweiter Tick liest den gehaltenen Snapshot des ersten.
    erst = _hass([], cache=REIHE, stand=HEUTE_STR)
    gehalten, stand = _attr(erst, "today"), _attr(erst, "stand")
    zweit = _hass([], cache=gehalten, stand=stand)
    assert _state(zweit) == "gehalten"
    assert _attr(zweit, "today") == REIHE


# --- Harte Schranke: Tagesgrenze ------------------------------------------

def test_cache_von_gestern_verfaellt():
    hass = _hass([], cache=REIHE, cache_morgen=MORGEN_REIHE,
                 stand=GESTERN_STR)
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []
    assert _attr(hass, "tomorrow") == []


def test_rollover_offen_uebernimmt_gestrige_reihe_nicht():
    """Review-Finding 25.07.2026: direkt nach Mitternacht liefert die Quelle noch
    die GESTRIGE Reihe. Numerisch gueltig, aber nicht von heute - wird sie als
    'frisch' auf das neue Datum gestempelt, koennte sie danach einen ganzen Tag
    gehalten werden und falsche Preiszweige freigeben."""
    hass = _hass(REIHE, cache=REIHE, cache_morgen=MORGEN_REIHE,
                 stand=GESTERN_STR,
                 now=dt.datetime(2026, 1, 15, 0, 3, tzinfo=TZ))
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []
    # Entscheidend: der Stempel darf NICHT auf heute wandern.
    assert _attr(hass, "stand") == GESTERN_STR


def test_rollover_erkannt_wenn_quelle_gestriges_tomorrow_zeigt():
    """Der positive Rollover-Beweis: das gestrige 'tomorrow' SIND die heutigen
    Preise. Zeigt die Quelle sie, hat sie umgeschaltet - ohne jede Uhrzeit-
    Heuristik, auch um 00:01."""
    hass = _hass(MORGEN_REIHE, [], cache=REIHE, cache_morgen=MORGEN_REIHE,
                 stand=GESTERN_STR,
                 now=dt.datetime(2026, 1, 15, 0, 1, tzinfo=TZ))
    assert _state(hass) == "frisch"
    assert _attr(hass, "today") == MORGEN_REIHE
    assert _attr(hass, "stand") == HEUTE_STR


def test_flattarif_blockiert_nicht():
    """Re-Review-Finding 1: wertgleiche Folgetagsreihe (wiederholter Flattarif).
    Sie ist == gestriges tomorrow, also erkennbar umgeschaltet - und weil die
    Werte identisch sind, sind sie ohnehin die richtigen. Keine Dauerblockade."""
    flach = [30.0] * 24
    hass = _hass(flach, cache=flach, cache_morgen=flach, stand=GESTERN_STR,
                 now=dt.datetime(2026, 1, 15, 0, 30, tzinfo=TZ))
    assert _state(hass) == "frisch"
    assert _attr(hass, "today") == flach
    assert _attr(hass, "stand") == HEUTE_STR


def test_ohne_vergleichsanker_wird_nicht_haltbar():
    """Re-Review-Finding 2 (zweite Runde): fehlt das gestrige tomorrow als Anker,
    ist nicht entscheidbar, ob die Quelle umgeschaltet hat. Vor 06:00 bleibt es
    zu; danach wird ausgeliefert, aber OHNE Stempel - sonst haette ein
    anschliessender Ausfall die stale Reihe bis Tagesende gehalten, was
    schlechter waere als ohne Halter."""
    frueh = _hass(REIHE, cache=REIHE, stand=GESTERN_STR,
                  now=dt.datetime(2026, 1, 15, 3, 0, tzinfo=TZ))
    assert _state(frueh) == "leer"
    assert _attr(frueh, "today") == []

    spaet = _hass(REIHE, cache=REIHE, stand=GESTERN_STR,
                  now=dt.datetime(2026, 1, 15, 7, 0, tzinfo=TZ))
    assert _state(spaet) == "unsicher"
    assert _attr(spaet, "today") == REIHE
    assert _attr(spaet, "stand") == GESTERN_STR, "darf nicht haltbar werden"


def test_unsichere_reihe_wird_nicht_gehalten():
    """Der Zweischritt aus dem Re-Review: erst wird die unsichere Reihe
    ausgeliefert, dann faellt die Quelle aus. Weil kein Stempel gesetzt wurde,
    greift kein Cache - fail-closed statt Persistenz einer falschen Reihe."""
    erst = _hass(REIHE, cache=REIHE, stand=GESTERN_STR,
                 now=dt.datetime(2026, 1, 15, 7, 0, tzinfo=TZ))
    geliefert, stand = _attr(erst, "today"), _attr(erst, "stand")
    assert geliefert == REIHE and stand == GESTERN_STR

    zweit = _hass([], cache=geliefert, stand=stand,
                  now=dt.datetime(2026, 1, 15, 7, 5, tzinfo=TZ))
    assert _state(zweit) == "leer"
    assert _attr(zweit, "today") == []


def test_gestriges_tomorrow_verfaellt_beim_tageswechsel():
    """Das gestrige 'tomorrow' sind die HEUTIGEN Preise - es darf nicht als
    'morgen' weiterleben, wenn die Quelle auf den neuen Tag umschaltet."""
    neue_reihe = [50.0 + i for i in range(24)]
    hass = _hass(neue_reihe, [], cache=REIHE, cache_morgen=MORGEN_REIHE,
                 stand=GESTERN_STR,
                 now=dt.datetime(2026, 1, 15, 0, 8, tzinfo=TZ))
    assert _state(hass) == "frisch"
    assert _attr(hass, "tomorrow") == []


def test_bekanntes_tomorrow_wird_nicht_ueberschrieben():
    """Review-Finding 25.07.2026: faellt nur die Morgen-Liste kurz weg, waehrend
    today gueltig bleibt, darf ein bereits bekanntes tomorrow nicht mit []
    ueberschrieben werden (sonst springen Perzentil und Peak-Reserve).
    Re-Review: dieser Teilausfall MUSS als 'gehalten' sichtbar sein, sonst
    zaehlen ihn die history_stats nicht."""
    hass = _hass(REIHE, [], cache=REIHE, cache_morgen=MORGEN_REIHE,
                 stand=HEUTE_STR)
    assert _state(hass) == "gehalten"
    assert _attr(hass, "gehalten_teil") == "morgen"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "tomorrow") == MORGEN_REIHE


def test_frisches_tomorrow_schlaegt_den_cache():
    aktuell = [99.0] * 24
    hass = _hass(REIHE, aktuell, cache=REIHE, cache_morgen=MORGEN_REIHE,
                 stand=HEUTE_STR)
    assert _attr(hass, "tomorrow") == aktuell


def test_ausfall_ueber_mitternacht_verfaellt():
    """Derselbe Cache, nur die Uhr ist weiter: nach Mitternacht darf die
    gestrige Reihe nicht mehr als 'heute' gelten."""
    vor = _hass([], cache=REIHE, stand=HEUTE_STR,
                now=dt.datetime(2026, 1, 15, 23, 58, tzinfo=TZ))
    nach = _hass([], cache=REIHE, stand=HEUTE_STR,
                 now=dt.datetime(2026, 1, 16, 0, 2, tzinfo=TZ))
    assert _state(vor) == "gehalten"
    assert _state(nach) == "leer"
    assert _attr(nach, "today") == []


# --- Kein Cache / unbrauchbare Daten --------------------------------------

def test_erststart_ohne_cache_ist_leer():
    hass = _hass([])
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []
    assert _attr(hass, "stand") == "none"


def test_zu_kurze_reihe_gilt_nicht_als_frisch():
    hass = _hass([30.0, 31.0], cache=REIHE, stand=HEUTE_STR)
    assert _state(hass) == "gehalten"
    assert _attr(hass, "today") == REIHE


def test_nicht_numerische_reihe_gilt_nicht_als_frisch():
    hass = _hass(["a", "b", None, "c"], cache=REIHE, stand=HEUTE_STR)
    assert _state(hass) == "gehalten"
    assert _attr(hass, "today") == REIHE


def test_unbrauchbarer_cache_wird_nicht_gehalten():
    hass = _hass([], cache=[1.0, 2.0], stand=HEUTE_STR)
    assert _state(hass) == "leer"


def test_halten_ist_alles_oder_nichts():
    """Unbrauchbarer today-Cache darf tomorrow nicht durchlassen: sonst
    berechnete opti_price_level ein Preisniveau allein aus MORGEN-Preisen,
    obwohl der Halter 'leer' meldet."""
    hass = _hass([], cache=[1.0, 2.0], cache_morgen=MORGEN_REIHE,
                 stand=HEUTE_STR)
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []
    assert _attr(hass, "tomorrow") == []


def test_fehlendes_quellattribut_ist_kein_absturz():
    hass = FakeHass(attrs={}, now=HEUTE, this_attributes={})
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []


# --- Kette: Halter -> price_level -----------------------------------------

def test_gehaltene_reihe_traegt_das_preisniveau():
    """Der Zweck der ganzen Uebung: waehrend eines Quellausfalls bleibt
    opti_price_level verfuegbar und behaelt sein Niveau, statt unavailable zu
    werden und die Strategie in den Default zu schicken."""
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    level = find_template_entity(cfg, "sensor", "opti_price_level")

    halter = _hass([], cache=REIHE, stand=HEUTE_STR)
    gehalten = _attr(halter, "today")

    hass = FakeHass(
        states={"sensor.opti_price_current_ct_kwh": "53.0"},
        attrs={"sensor.opti_price_series_stable": {"today": gehalten,
                                                   "tomorrow": []}},
        now=HEUTE,
    )
    assert render(hass, level["availability"]) == "True"
    assert render(hass, level["state"]) == "VERY_EXPENSIVE"


# ---------------------------------------------------------------------------
# Mehr-Tick-Sequenzen (Re-Review-Finding 25.07.2026, dritte Runde).
# Einzeltick-Tests waren hier false-green: der Fehler lag im FOLGETICK, weil
# der Zustand 'leer' die Nutzlast today/tomorrow auf [] setzte und damit die
# Vergleichsanker mit zerstoerte. Dieselbe gestrige Quelle sah dann veraendert
# aus, wurde gestempelt und war wieder haltbar - die Wache hielt einen Tick.
# ---------------------------------------------------------------------------

def _vorstand(cache, cache_morgen, stand):
    return {"anker_today": cache, "anker_tomorrow": cache_morgen,
            "stand": stand}


def test_rollover_bleibt_ueber_mehrere_ticks_zu():
    """Die gestrige Quelle darf auch nach beliebig vielen Ticks nicht als frisch
    gestempelt werden - die Anker muessen den Zustand 'leer' ueberleben."""
    nacht = dt.datetime(2026, 1, 15, 0, 3, tzinfo=TZ)
    zustand = _vorstand(REIHE, MORGEN_REIHE, GESTERN_STR)
    for runde in range(4):
        zustand = _tick(REIHE, [], vorher=zustand, now=nacht)
        assert zustand["state"] == "leer", f"Tick {runde}"
        assert zustand["today"] == [], f"Tick {runde}"
        assert zustand["stand"] == GESTERN_STR, f"Tick {runde}"
        assert zustand["anker_today"] == REIHE, f"Anker verloren in Tick {runde}"
        assert zustand["anker_tomorrow"] == MORGEN_REIHE, f"Tick {runde}"


def test_rollover_zu_dann_umschaltung_wird_uebernommen():
    """Gegenprobe: sobald die Quelle wirklich umschaltet (== gestriges tomorrow),
    greift der Halter nach der Blockade sofort wieder normal."""
    nacht = dt.datetime(2026, 1, 15, 0, 3, tzinfo=TZ)
    zustand = _vorstand(REIHE, MORGEN_REIHE, GESTERN_STR)
    zustand = _tick(REIHE, [], vorher=zustand, now=nacht)
    assert zustand["state"] == "leer"
    zustand = _tick(MORGEN_REIHE, [], vorher=zustand, now=nacht)
    assert zustand["state"] == "frisch"
    assert zustand["today"] == MORGEN_REIHE
    assert zustand["stand"] == HEUTE_STR
    # Das gestrige tomorrow ist jetzt today - der Morgen-Anker muss mitgehen.
    assert zustand["anker_tomorrow"] == []


def test_unsicher_persistiert_auch_ueber_ausfall_und_rueckkehr_nicht():
    """Der vom Reviewer reproduzierte Pfad: unsicher -> Ausfall -> Quelle kehrt
    unveraendert zurueck. Ohne erhaltene Anker waere daraus 'frisch' mit Stempel
    geworden und der naechste Ausfall haette die falsche Reihe gehalten."""
    spaet = dt.datetime(2026, 1, 15, 7, 0, tzinfo=TZ)
    zustand = {"anker_today": REIHE, "stand": GESTERN_STR}

    zustand = _tick(REIHE, [], vorher=zustand, now=spaet)
    assert zustand["state"] == "unsicher"
    assert zustand["stand"] == GESTERN_STR

    zustand = _tick([], [], vorher=zustand, now=spaet)
    assert zustand["state"] == "leer"
    assert zustand["today"] == []

    zustand = _tick(REIHE, [], vorher=zustand, now=spaet)
    assert zustand["state"] == "unsicher", "darf nicht zu 'frisch' kippen"
    assert zustand["stand"] == GESTERN_STR, "darf nie haltbar werden"

    zustand = _tick([], [], vorher=zustand, now=spaet)
    assert zustand["state"] == "leer", "fail-closed statt Persistenz"
    assert zustand["today"] == []


def test_normalbetrieb_ueber_mehrere_ticks():
    """Gegenprobe zur Blockade-Kette: im Normalbetrieb bleibt alles frisch,
    ein kurzer Ausfall wird gehalten, danach geht es frisch weiter."""
    zustand = _tick(REIHE, MORGEN_REIHE)
    assert zustand["state"] == "frisch"
    zustand = _tick([], [], vorher=zustand)
    assert zustand["state"] == "gehalten"
    assert zustand["gehalten_teil"] == "reihe"
    assert zustand["today"] == REIHE and zustand["tomorrow"] == MORGEN_REIHE
    zustand = _tick([], [], vorher=zustand)
    assert zustand["state"] == "gehalten", "auch der zweite Ausfall-Tick haelt"
    assert zustand["today"] == REIHE
    zustand = _tick(REIHE, MORGEN_REIHE, vorher=zustand)
    assert zustand["state"] == "frisch"


def test_mehrtaegige_blockade_stempelt_keine_alte_reihe():
    """Re-Review-Finding 25.07.2026 (vierte Runde): stand=14., Anker fuer 14./15.
    Die Quelle bleibt am 15. blockiert und schaltet erst am 16. auf die
    15.-Reihe. Die ist dann einen Tag zu alt - der Rollover-Beweis
    'Quelle == anker_tomorrow' darf hier NICHT greifen, sonst wird sie als
    16. gestempelt und beim naechsten Ausfall gehalten."""
    reihe_14, reihe_15 = REIHE, MORGEN_REIHE
    zustand = {"anker_today": reihe_14, "anker_tomorrow": reihe_15,
               "stand": "2026-01-14"}

    # 15.01: Quelle haengt auf der 14.-Reihe -> blockiert, Anker bleiben.
    am_15 = dt.datetime(2026, 1, 15, 8, 0, tzinfo=TZ)
    zustand = _tick(reihe_14, [], vorher=zustand, now=am_15)
    assert zustand["state"] == "leer"
    assert zustand["stand"] == "2026-01-14"

    # 16.01: Quelle schaltet auf die 15.-Reihe um - einen Tag zu alt.
    am_16_frueh = dt.datetime(2026, 1, 16, 3, 0, tzinfo=TZ)
    frueh = _tick(reihe_15, [], vorher=zustand, now=am_16_frueh)
    assert frueh["state"] == "leer"
    assert frueh["today"] == []
    assert frueh["stand"] == "2026-01-14"

    am_16_spaet = dt.datetime(2026, 1, 16, 9, 0, tzinfo=TZ)
    spaet = _tick(reihe_15, [], vorher=zustand, now=am_16_spaet)
    assert spaet["state"] == "unsicher", "ausliefern ja, stempeln nein"
    assert spaet["stand"] == "2026-01-14", "darf nicht haltbar werden"

    # Und der Folgeausfall darf die Reihe nicht halten.
    danach = _tick([], [], vorher=spaet, now=am_16_spaet)
    assert danach["state"] == "leer"
    assert danach["today"] == []


def test_eintaegiger_rollover_bleibt_beweisbar():
    """Gegenprobe zur Altersbedingung: der normale Tageswechsel (Anker genau
    einen Tag alt) muss weiterhin sofort als umgeschaltet erkannt werden."""
    zustand = {"anker_today": REIHE, "anker_tomorrow": MORGEN_REIHE,
               "stand": GESTERN_STR}
    ergebnis = _tick(MORGEN_REIHE, [], vorher=zustand,
                     now=dt.datetime(2026, 1, 15, 0, 1, tzinfo=TZ))
    assert ergebnis["state"] == "frisch"
    assert ergebnis["today"] == MORGEN_REIHE
    assert ergebnis["stand"] == HEUTE_STR
