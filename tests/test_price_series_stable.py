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
    """cache/stand modellieren den vorherigen this-Snapshot (RestoreEntity)."""
    this_attrs = {}
    if cache is not None:
        this_attrs["today"] = cache
    if cache_morgen is not None:
        this_attrs["tomorrow"] = cache_morgen
    if stand is not None:
        this_attrs["stand"] = stand
    return FakeHass(
        attrs={"sensor.opti_price_series": {"today": today,
                                            "tomorrow": tomorrow or []}},
        now=now,
        this_attributes=this_attrs,
    )


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


def test_rollover_erledigt_uebernimmt_neue_reihe():
    """Sobald die Quelle umschaltet (andere Liste als der Cache), greift der
    Halter wieder normal - die Wache darf nicht dauerhaft blockieren."""
    neue_reihe = [50.0 + i for i in range(24)]
    hass = _hass(neue_reihe, cache=REIHE, stand=GESTERN_STR,
                 now=dt.datetime(2026, 1, 15, 0, 8, tzinfo=TZ))
    assert _state(hass) == "frisch"
    assert _attr(hass, "today") == neue_reihe
    assert _attr(hass, "stand") == HEUTE_STR


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
    ueberschrieben werden (sonst springen Perzentil und Peak-Reserve)."""
    hass = _hass(REIHE, [], cache=REIHE, cache_morgen=MORGEN_REIHE,
                 stand=HEUTE_STR)
    assert _state(hass) == "frisch"
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
