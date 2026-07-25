"""Preisreihen-Halter (opti_derived.yaml Abschnitt 8b).

Ueberbrueckt kurze Ausfaelle des Preisreihen-Anbieters, damit die Strategie nicht
in den Default-Zweig kippt (Live-Befund 23./24.07.2026: bei einem Tibber-REST-
Timeout verloren today/tomorrow ihren Inhalt, opti_price_level und die
Peak-Reserve brachen gleichzeitig weg, der Modus sprang zwischen Peak-Leiter und
'Akku Dynamisch').

Vier Eigenschaften tragen die Tests hier, jede aus einem konkreten Fehlerbild:

1. HALTEFENSTER 15 min, minuetlich abgetastet. Die beobachteten Ausfaelle
   dauerten 20-80 s. Ein laengerer Ausfall endet fail-closed.
2. TAGESGRENZE. Ein 15-min-Fenster kann Mitternacht sehr wohl ueberschreiten
   (frisch 23:55, Ausfall 00:00). Gehalten wird nur, wenn der Anker am heutigen
   Tag erfasst wurde - sonst wanderte die gestrige Reihe in den neuen Tag.
3. GEDAECHTNIS getrennt von der NUTZLAST. 'anker' ueberlebt jeden Zustand,
   today/tomorrow gehen im Zustand 'leer' auf []. Lagen beide auf denselben
   Attributen, verlor der Halter sein Gedaechtnis nach einem Tick - sichtbar nur
   in Mehr-Tick-Sequenzen.
4. EIGENER ZEITSTEMPEL FUER DIE MORGEN-LISTE. Bei einem reinen Morgen-Ausfall
   bleibt today frisch; ein gemeinsamer Zeitstempel wurde dabei jeden Tick
   erneuert und das Fenster lief nie ab.
"""
from __future__ import annotations

import datetime as dt

from .ha_harness import (REPO, TZ, FakeHass, find_template_entity, load_yaml,
                         render, render_native)

JETZT = dt.datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
TS = round(JETZT.timestamp())
HEUTE_STR = "2026-01-15"
GESTERN_STR = "2026-01-14"
REIHE = [30.0 + i for i in range(24)]
MORGEN_REIHE = [40.0 + i for i in range(24)]


def _entity():
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    return find_template_entity(cfg, "sensor", "opti_price_series_stable")


def _anker(today=None, tomorrow=None, ts=None, m_ts=None, datum=HEUTE_STR):
    return {"today": today if today is not None else [],
            "tomorrow": tomorrow if tomorrow is not None else [],
            "ts": ts if ts is not None else 0,
            "m_ts": m_ts if m_ts is not None else 0,
            "datum": datum}


def _hass(today, tomorrow=None, *, anker=None, now=JETZT):
    return FakeHass(
        attrs={"sensor.opti_price_series": {"today": today,
                                            "tomorrow": tomorrow or []}},
        now=now,
        this_attributes={"anker": anker} if anker is not None else {},
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
        this_attributes={"anker": vorher["anker"]} if "anker" in vorher else {},
    )
    ergebnis = {"state": _state(hass)}
    for name in ("today", "tomorrow", "anker", "gehalten_teil"):
        ergebnis[name] = _attr(hass, name)
    return ergebnis


# --- Normalbetrieb ---------------------------------------------------------

def test_frische_reihe_wird_uebernommen():
    hass = _hass(REIHE, MORGEN_REIHE)
    assert _state(hass) == "frisch"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "tomorrow") == MORGEN_REIHE
    anker = _attr(hass, "anker")
    assert anker["today"] == REIHE and anker["tomorrow"] == MORGEN_REIHE
    assert anker["ts"] == TS and anker["m_ts"] == TS
    assert anker["datum"] == HEUTE_STR


def test_frisch_ueberschreibt_alten_anker():
    hass = _hass(REIHE, anker=_anker([1.0] * 24, ts=TS - 60))
    assert _state(hass) == "frisch"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "anker")["today"] == REIHE


def test_leeres_tomorrow_ist_kein_ausfall():
    # Vor der Day-Ahead-Veroeffentlichung ist tomorrow legitim leer.
    hass = _hass(REIHE, [])
    assert _state(hass) == "frisch"
    assert _attr(hass, "tomorrow") == []


def test_fehlendes_quellattribut_ist_kein_absturz():
    hass = FakeHass(attrs={}, now=JETZT, this_attributes={})
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []


# --- Haltefenster ----------------------------------------------------------

def test_leere_reihe_wird_im_fenster_gehalten():
    hass = _hass([], anker=_anker(REIHE, MORGEN_REIHE, ts=TS - 60,
                                  m_ts=TS - 60))
    assert _state(hass) == "gehalten"
    assert _attr(hass, "gehalten_teil") == "reihe"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "tomorrow") == MORGEN_REIHE


def test_anker_ts_laeuft_beim_halten_nicht_mit():
    """Sonst wanderte der Stempel bei jedem Tick mit und das Fenster liefe nie
    ab - der Halter wuerde die Reihe unbegrenzt festhalten."""
    hass = _hass([], anker=_anker(REIHE, ts=TS - 600))
    assert _attr(hass, "anker")["ts"] == TS - 600


def test_haltefenster_laeuft_ab():
    knapp_drin = _hass([], anker=_anker(REIHE, MORGEN_REIHE, ts=TS - 890,
                                        m_ts=TS - 890))
    assert _state(knapp_drin) == "gehalten"

    abgelaufen = _hass([], anker=_anker(REIHE, MORGEN_REIHE, ts=TS - 910,
                                        m_ts=TS - 910))
    assert _state(abgelaufen) == "leer"
    assert _attr(abgelaufen, "today") == []
    assert _attr(abgelaufen, "tomorrow") == []


def test_zukunfts_zeitstempel_haelt_nicht_dauerhaft():
    """Ein restaurierter Zukunftsstempel erfuellte ohne Untergrenze dauerhaft
    das Fenster - die Reihe waere unbegrenzt gehalten worden."""
    hass = _hass([], anker=_anker(REIHE, ts=TS + 100000))
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []


def test_ohne_zeitstempel_wird_nichts_gehalten():
    hass = _hass([], anker=_anker(REIHE))
    assert _state(hass) == "leer"


def test_unbrauchbarer_anker_wird_nicht_gehalten():
    hass = _hass([], anker=_anker([1.0, 2.0], ts=TS - 60))
    assert _state(hass) == "leer"


def test_zu_kurze_reihe_gilt_nicht_als_frisch():
    hass = _hass([30.0, 31.0], anker=_anker(REIHE, ts=TS - 60))
    assert _state(hass) == "gehalten"
    assert _attr(hass, "today") == REIHE


def test_nicht_numerische_reihe_gilt_nicht_als_frisch():
    hass = _hass(["a", "b", None, "c"], anker=_anker(REIHE, ts=TS - 60))
    assert _state(hass) == "gehalten"
    assert _attr(hass, "today") == REIHE


# --- Tagesgrenze -----------------------------------------------------------

def test_anker_von_gestern_wird_nicht_gehalten():
    """Die Tagesgrenze: ein 15-min-Fenster kann Mitternacht ueberschreiten, aber
    die gestrige Reihe darf nicht in den neuen Tag wandern."""
    kurz_nach_mitternacht = dt.datetime(2026, 1, 15, 0, 2, tzinfo=TZ)
    ts = round(kurz_nach_mitternacht.timestamp()) - 420  # 23:55 des Vortags
    hass = _hass([], anker=_anker(REIHE, MORGEN_REIHE, ts=ts, m_ts=ts,
                                  datum=GESTERN_STR),
                 now=kurz_nach_mitternacht)
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []
    assert _attr(hass, "tomorrow") == [], "gestriges tomorrow sind heutige Preise"


def test_tageswechsel_im_fenster_ueber_mehrere_ticks():
    """Die Sequenz aus dem Review: frisch um 23:55, Quelle ab 00:00 weg. Der
    Ausfall liegt im 15-min-Fenster, die Tagesgrenze muss trotzdem greifen."""
    vor_mitternacht = dt.datetime(2026, 1, 14, 23, 55, tzinfo=TZ)
    zustand = _tick(REIHE, MORGEN_REIHE, now=vor_mitternacht)
    assert zustand["state"] == "frisch"
    assert zustand["anker"]["datum"] == GESTERN_STR

    noch_gestern = _tick([], [], vorher=zustand,
                         now=dt.datetime(2026, 1, 14, 23, 58, tzinfo=TZ))
    assert noch_gestern["state"] == "gehalten", "im selben Tag wird gehalten"

    nach_mitternacht = _tick([], [], vorher=zustand,
                             now=dt.datetime(2026, 1, 15, 0, 2, tzinfo=TZ))
    assert nach_mitternacht["state"] == "leer"
    assert nach_mitternacht["today"] == []
    assert nach_mitternacht["tomorrow"] == []


def test_tageswechsel_verwirft_das_gestrige_tomorrow():
    """Wechselt die today-Liste, ist ein neuer Tag angebrochen: das gestrige
    tomorrow SIND die heutigen Preise und darf nicht als 'morgen' weiterleben."""
    neue_reihe = [50.0 + i for i in range(24)]
    hass = _hass(neue_reihe, [], anker=_anker(REIHE, MORGEN_REIHE, ts=TS - 60,
                                              m_ts=TS - 60))
    assert _state(hass) == "frisch"
    assert _attr(hass, "tomorrow") == []
    anker = _attr(hass, "anker")
    assert anker["tomorrow"] == [] and anker["m_ts"] == 0


# --- tomorrow: Teilausfall -------------------------------------------------

def test_bekanntes_tomorrow_wird_nicht_ueberschrieben():
    """Faellt nur die Morgen-Liste kurz weg, waehrend today gueltig bleibt, darf
    ein bekanntes tomorrow nicht mit [] ueberschrieben werden - sonst springen
    Perzentil und Peak-Reserve. Der Teilausfall MUSS 'gehalten' sein, sonst
    zaehlen ihn die history_stats nicht."""
    hass = _hass(REIHE, [], anker=_anker(REIHE, MORGEN_REIHE, ts=TS - 60,
                                         m_ts=TS - 60))
    assert _state(hass) == "gehalten"
    assert _attr(hass, "gehalten_teil") == "morgen"
    assert _attr(hass, "today") == REIHE
    assert _attr(hass, "tomorrow") == MORGEN_REIHE


def test_morgen_ausfall_laeuft_ab_obwohl_today_frisch_bleibt():
    """Der Review-Fund: bei einem reinen Morgen-Ausfall bleibt today frisch und
    erneuerte ts bei jedem Tick - mit gemeinsamem Zeitstempel lief das Fenster
    nie ab und das alte tomorrow blieb unbegrenzt stehen."""
    zustand = _tick(REIHE, MORGEN_REIHE, now=JETZT)
    assert zustand["state"] == "frisch"

    for minuten in (5, 10, 14):
        zwischen = _tick(REIHE, [], vorher=zustand,
                         now=JETZT + dt.timedelta(minutes=minuten))
        assert zwischen["state"] == "gehalten", f"nach {minuten} min"
        assert zwischen["tomorrow"] == MORGEN_REIHE, f"nach {minuten} min"
        zustand = zwischen

    danach = _tick(REIHE, [], vorher=zustand,
                   now=JETZT + dt.timedelta(minutes=20))
    assert danach["state"] == "frisch", "Morgen-Fenster ist abgelaufen"
    assert danach["today"] == REIHE
    assert danach["tomorrow"] == [], "altes tomorrow darf nicht ewig stehen"


def test_frisches_tomorrow_schlaegt_den_anker():
    aktuell = [99.0] * 24
    hass = _hass(REIHE, aktuell, anker=_anker(REIHE, MORGEN_REIHE, ts=TS - 60,
                                              m_ts=TS - 60))
    assert _attr(hass, "tomorrow") == aktuell
    anker = _attr(hass, "anker")
    assert anker["tomorrow"] == aktuell and anker["m_ts"] == TS


def test_halten_ist_alles_oder_nichts():
    """Unbrauchbarer today-Anker darf tomorrow nicht durchlassen: sonst
    berechnete opti_price_level ein Preisniveau allein aus MORGEN-Preisen,
    obwohl der Halter 'leer' meldet."""
    hass = _hass([], anker=_anker([1.0, 2.0], MORGEN_REIHE, ts=TS - 60,
                                  m_ts=TS - 60))
    assert _state(hass) == "leer"
    assert _attr(hass, "today") == []
    assert _attr(hass, "tomorrow") == []


# --- Mehr-Tick-Sequenzen ---------------------------------------------------

def test_anker_ueberleben_den_zustand_leer():
    """Der Kern der Nutzlast/Gedaechtnis-Trennung: auch nach mehreren Leer-Ticks
    muss der today-Anker noch da sein. Die Morgen-Liste wird dagegen bewusst
    vergessen, sobald ihr eigenes Fenster abgelaufen ist - sonst taucht sie beim
    naechsten Komplettausfall wieder auf."""
    zustand = {"anker": _anker(REIHE, MORGEN_REIHE, ts=TS - 1000,
                               m_ts=TS - 1000)}
    for runde in range(4):
        zustand = _tick([], [], vorher=zustand)
        assert zustand["state"] == "leer", f"Tick {runde}"
        assert zustand["today"] == [], f"Tick {runde}"
        assert zustand["anker"]["today"] == REIHE, f"Anker weg in Tick {runde}"
        assert zustand["anker"]["ts"] == TS - 1000, f"Tick {runde}"
        assert zustand["anker"]["tomorrow"] == [], f"Tick {runde}"
        assert zustand["anker"]["m_ts"] == 0, f"Tick {runde}"


def test_abgelaufenes_tomorrow_wird_nicht_wiederbelebt():
    """Review-Finding 25.07.2026: das Ganzreihen-Halten gab a_m ohne Pruefung des
    Morgen-Fensters aus. Reproduziert war: tomorrow nach 20 min korrekt leer,
    eine Minute spaeter bei Komplettausfall wieder da."""
    zustand = _tick(REIHE, MORGEN_REIHE, now=JETZT)
    assert zustand["tomorrow"] == MORGEN_REIHE

    # Morgen-Liste faellt weg, today bleibt frisch -> Fenster laeuft ab.
    zustand = _tick(REIHE, [], vorher=zustand,
                    now=JETZT + dt.timedelta(minutes=20))
    assert zustand["tomorrow"] == [], "Morgen-Fenster abgelaufen"

    # Jetzt der Komplettausfall: today wird gehalten, tomorrow darf NICHT
    # zurueckkommen.
    zustand = _tick([], [], vorher=zustand,
                    now=JETZT + dt.timedelta(minutes=21))
    assert zustand["state"] == "gehalten"
    assert zustand["today"] == REIHE
    assert zustand["tomorrow"] == [], "abgelaufene Morgen-Liste wiederbelebt"


def test_mitternachts_folgetick_reaktiviert_gestriges_tomorrow_nicht():
    """Review-Finding 25.07.2026: liefert die Quelle nach Mitternacht unveraendert
    das gestrige today und kein tomorrow, wurde im ersten Tick 'datum' auf heute
    gesetzt, waehrend der alte Morgen-Anker erhalten blieb - im zweiten Tick galt
    er dann als heutiges 'morgen'. Das geht ueber die dokumentierte, unvermeidbare
    Umdatierung des Quell-today hinaus."""
    vor_mitternacht = dt.datetime(2026, 1, 14, 23, 58, tzinfo=TZ)
    zustand = _tick(REIHE, MORGEN_REIHE, now=vor_mitternacht)
    assert zustand["anker"]["datum"] == GESTERN_STR
    assert zustand["anker"]["tomorrow"] == MORGEN_REIHE

    nach = dt.datetime(2026, 1, 15, 0, 2, tzinfo=TZ)
    erst = _tick(REIHE, [], vorher=zustand, now=nach)
    assert erst["state"] == "frisch"
    assert erst["anker"]["datum"] == HEUTE_STR, "Quell-today wird umdatiert"
    assert erst["anker"]["tomorrow"] == [], "gestriges tomorrow muss weg sein"
    assert erst["tomorrow"] == []

    zweit = _tick(REIHE, [], vorher=erst,
                  now=dt.datetime(2026, 1, 15, 0, 3, tzinfo=TZ))
    assert zweit["tomorrow"] == [], "gestriges tomorrow reaktiviert"


def test_zukunfts_zeitstempel_wird_nicht_konserviert():
    """Review-Finding 25.07.2026: die Untergrenze wies den Zukunftsstempel nur
    ab, solange er in der Zukunft lag - der Leerpfad bewahrte ihn auf, und beim
    Erreichen der Stempelzeit begann das Halten."""
    zustand = {"anker": _anker(REIHE, ts=TS + 600)}
    erst = _tick([], [], vorher=zustand, now=JETZT)
    assert erst["state"] == "leer"
    assert erst["anker"]["ts"] == 0, "Zukunftsstempel muss verworfen werden"

    # Uhr erreicht die alte Stempelzeit - es darf trotzdem nicht gehalten werden.
    spaeter = _tick([], [], vorher=erst, now=JETZT + dt.timedelta(minutes=11))
    assert spaeter["state"] == "leer"
    assert spaeter["today"] == []


def test_nicht_nativer_anker_faellt_fail_closed():
    """Review-Finding 25.07.2026: a.get(...) setzt ein Mapping voraus. Ein als
    String oder Liste restaurierter Anker (gescheiterte literal_eval, aeltere
    Attributform) darf keinen Renderfehler werfen, sondern fail-closed laufen."""
    for kaputt in ("keine-map", ["nur", "liste"], 42):
        hass = FakeHass(
            attrs={"sensor.opti_price_series": {"today": [], "tomorrow": []}},
            now=JETZT,
            this_attributes={"anker": kaputt},
        )
        assert _state(hass) == "leer", f"anker={kaputt!r}"
        assert _attr(hass, "today") == [], f"anker={kaputt!r}"


def test_normalbetrieb_ueber_mehrere_ticks():
    zustand = _tick(REIHE, MORGEN_REIHE)
    assert zustand["state"] == "frisch"
    zustand = _tick([], [], vorher=zustand)
    assert zustand["state"] == "gehalten"
    assert zustand["today"] == REIHE and zustand["tomorrow"] == MORGEN_REIHE
    zustand = _tick([], [], vorher=zustand)
    assert zustand["state"] == "gehalten", "auch der zweite Ausfall-Tick haelt"
    zustand = _tick(REIHE, MORGEN_REIHE, vorher=zustand)
    assert zustand["state"] == "frisch"
    assert zustand["anker"]["ts"] == TS


def test_langer_ausfall_kippt_ins_fail_closed_und_erholt_sich():
    zustand = _tick(REIHE, MORGEN_REIHE, now=JETZT)
    zustand = _tick([], [], vorher=zustand, now=JETZT + dt.timedelta(minutes=10))
    assert zustand["state"] == "gehalten"
    zustand = _tick([], [], vorher=zustand, now=JETZT + dt.timedelta(minutes=20))
    assert zustand["state"] == "leer"
    assert zustand["today"] == []
    zustand = _tick(REIHE, MORGEN_REIHE, vorher=zustand,
                    now=JETZT + dt.timedelta(minutes=21))
    assert zustand["state"] == "frisch"


# --- Kette: Halter -> price_level -----------------------------------------

def _level_entity():
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    return find_template_entity(cfg, "sensor", "opti_price_level")


def _level_hass(nutzlast):
    return FakeHass(
        states={"sensor.opti_price_current_ct_kwh": "53.0"},
        attrs={"sensor.opti_price_series_stable": {"today": nutzlast,
                                                   "tomorrow": []}},
        now=JETZT,
    )


def test_gehaltene_reihe_traegt_das_preisniveau():
    """Der Zweck der ganzen Uebung: waehrend eines kurzen Quellausfalls bleibt
    opti_price_level verfuegbar und behaelt sein Niveau, statt unavailable zu
    werden und die Strategie in den Default-Zweig zu schicken."""
    halter = _hass([], anker=_anker(REIHE, ts=TS - 60))
    gehalten = _attr(halter, "today")
    assert gehalten == REIHE

    level = _level_entity()
    hass = _level_hass(gehalten)
    assert render(hass, level["availability"]) == "True"
    assert render(hass, level["state"]) == "VERY_EXPENSIVE"


def test_abgelaufener_halter_macht_preisniveau_unavailable():
    """Die Gegenprobe: nach dem Haltefenster ist die Nutzlast leer und das
    Preisniveau faellt fail-closed aus, statt ein Niveau zu erfinden."""
    halter = _hass([], anker=_anker(REIHE, ts=TS - 1000))
    assert _state(halter) == "leer"

    level = _level_entity()
    hass = _level_hass(_attr(halter, "today"))
    assert render(hass, level["availability"]) == "False"
    assert render(hass, level["state"]) == "unavailable"


def test_beschaedigtes_tomorrow_unterfeld_faellt_fail_closed():
    """Review-Finding 25.07.2026 (Minor): der Mapping-Guard war flach. Ein
    beschaedigtes tomorrow-Unterfeld wie "1234" wurde von |list zu
    ['1','2','3','4'] normalisiert, konserviert und im Folgetick als 'gehalten'
    wieder ausgegeben; ein Skalar wie 42 warf einen Renderfehler."""
    for kaputt in ("1234", 42, [1.0, 2.0, "x"], {"a": 1}):
        zustand = {"anker": _anker(REIHE, kaputt, ts=TS - 60, m_ts=TS - 60)}
        erst = _tick([], [], vorher=zustand)
        assert erst["state"] == "gehalten", f"tomorrow={kaputt!r}"
        assert erst["today"] == REIHE, f"tomorrow={kaputt!r}"
        assert erst["tomorrow"] == [], f"tomorrow={kaputt!r}"
        assert erst["anker"]["tomorrow"] == [], f"tomorrow={kaputt!r}"

        zweit = _tick([], [], vorher=erst)
        assert zweit["tomorrow"] == [], f"Folgetick, tomorrow={kaputt!r}"


def test_beschaedigte_quell_morgenliste_wird_nicht_uebernommen():
    """Symmetrisch zur Ankerseite: eine unbrauchbare Morgen-Liste aus der Quelle
    darf nicht ins Gedaechtnis wandern."""
    hass = _hass(REIHE, [1.0, "x", 3.0])
    assert _state(hass) == "frisch"
    assert _attr(hass, "tomorrow") == []
    assert _attr(hass, "anker")["tomorrow"] == []
