from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

BASIS = {
    "sensor.opti_soc": "40",
    "sensor.opti_battery_capacity_kwh": "12.8",
    "sensor.opti_forecast_score": "5",
    "sensor.opti_forecast_score_tomorrow": "5",
    "sensor.opti_price_level": "NORMAL",
    "sensor.opti_target_soc": "60",
    "sensor.opti_price_current_ct_kwh": "30",
    "binary_sensor.opti_ueberschuss_70_aktiv": "off",
    "binary_sensor.opti_ueberschuss_ac_aktiv": "off",
    "sensor.opti_peak_reserve_soc": "unavailable",
    "binary_sensor.opti_peak_reserve_aktiv": "off",
    "binary_sensor.opti_winter_charging_allowed": "on",
    "input_number.minsoc": "10",
    "input_number.maxsoc": "95",
    "input_number.opti_einspeiseverguetung_ct": "8",
    "input_number.opti_netzlade_spread_ct": "10",
    # halte_spread "0" = alte Tests bleiben semantisch unveraendert (L3 haelt
    # sobald ve_avg gesetzt ist); neue Tests setzen halte_spread aktiv.
    "input_number.opti_halte_spread_ct": "0",
    "input_boolean.opti_prognose_netzladen": "on",
    "input_boolean.opti_pv_ueberschuss_ladung": "on",
    "input_select.akkusteuerung_modus": "Akku Dynamisch",
    "sun.sun": "below_horizon",
    # Balancing-Watchdog default aus (feuert nur, wenn Fixture ihn setzt).
    "sensor.opti_balancing_watchdog": "aus",
    # EV-Sperre default aus (Feature optional; Fixtures schalten sie gezielt an).
    "input_boolean.opti_ev_akku_pause": "off",
    "binary_sensor.opti_ev_schnellladung": "off",
}


def _render_vorschau(part, overrides):
    overrides = dict(overrides)
    attrs = overrides.pop("_attrs", None) or {}
    states = dict(BASIS)
    states.update(overrides)
    hass = FakeHass(states=states, attrs=attrs)
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    entity = find_template_entity(cfg, "sensor", "opti_strategie_vorschau")
    template = entity["state"] if part == "state" else entity["attributes"]["grund"]
    return render(hass, template)


def vorschau(**overrides):
    return _render_vorschau("state", overrides)


def grund(**overrides):
    return _render_vorschau("grund", overrides)


def reserve_attrs(ve=30.0, min_vor=None, avg=None, ve_avg=None):
    # ve_avg default = avg: alte Fixturen (nur avg gesetzt) nehmen an, dass die
    # Reserve rein aus VE-Stunden besteht, damit L3-Alt-Tests mit halte_spread
    # "0" unveraendert bleiben (ve_avg is not none noetig, Wert selbst ist bei
    # halte_spread 0 irrelevant, solange ve_avg >= cur).
    return {"sensor.opti_peak_reserve_soc": {
        "reserve_ve_soc": ve, "min_preis_vor_peak_ct": min_vor,
        "peak_preis_avg_ct": avg,
        "peak_preis_ve_avg_ct": ve_avg if ve_avg is not None else avg}}


# --- Task 4: Negativpreis-Laderegel ---

def test_negativpreis_laedt():
    # Preis 3 ct < EEG 8 ct, Prognose schlecht, kein guenstigeres Fenster bekannt.
    assert vorschau(**{"sensor.opti_price_current_ct_kwh": "3",
                       "sensor.opti_forecast_score": "1"}) == "Akku Netzladen"
    assert "Negativpreis" in grund(**{"sensor.opti_price_current_ct_kwh": "3",
                                      "sensor.opti_forecast_score": "1"})


def test_negativpreis_wartet_auf_guenstigeres_fenster():
    out = vorschau(**{"sensor.opti_price_current_ct_kwh": "3",
                      "sensor.opti_forecast_score": "1",
                      "sensor.opti_peak_reserve_soc": "35",
                      "_attrs": reserve_attrs(min_vor=-2.0, avg=40.0)})
    assert out != "Akku Netzladen"  # -2 ct kommt noch -> warten


def test_negativpreis_marge_gleiche_preise():
    # min_vor = 2.6, aktuell 3.0 -> innerhalb 0.5-ct-Marge -> laden.
    assert vorschau(**{"sensor.opti_price_current_ct_kwh": "3",
                       "sensor.opti_forecast_score": "1",
                       "sensor.opti_peak_reserve_soc": "35",
                       "_attrs": reserve_attrs(min_vor=2.6, avg=40.0)}) == "Akku Netzladen"


def test_negativpreis_nicht_bei_guter_prognose():
    assert vorschau(**{"sensor.opti_price_current_ct_kwh": "3",
                       "sensor.opti_forecast_score": "7"}) != "Akku Netzladen"


def test_negativpreis_nicht_ueber_eeg():
    # 12 ct > EEG 8 ct: der ALTE SOC<45-Block darf greifen, aber nicht die
    # Negativpreis-Regel - deshalb wird das grund-Attribut geprueft.
    g = grund(**{"sensor.opti_price_current_ct_kwh": "12",
                 "sensor.opti_forecast_score": "1",
                 "sensor.opti_price_level": "VERY_CHEAP"})
    assert "Negativpreis" not in g


def test_negativpreis_gate_aus():
    assert vorschau(**{"sensor.opti_price_current_ct_kwh": "3",
                       "sensor.opti_forecast_score": "1",
                       "input_boolean.opti_prognose_netzladen": "off"}) != "Akku Netzladen"


def test_minsoc_schutz_hat_vorrang():
    assert vorschau(**{"sensor.opti_soc": "5",
                       "sensor.opti_price_current_ct_kwh": "3"}) == "Akku nur Laden"


# --- Task 5: Peak-Vorladeregel ---

VORLADEN = {
    "sensor.opti_price_current_ct_kwh": "50",
    "sensor.opti_forecast_score": "1",
    "sensor.opti_forecast_score_tomorrow": "1",
    "sensor.opti_peak_reserve_soc": "35",
    "binary_sensor.opti_peak_reserve_aktiv": "on",
    "sensor.opti_soc": "15",
}


def test_vorladen_bei_grossem_spread():
    # Peak avg 200 ct - aktuell 50 ct = 150 >= 10 -> laden bis Reserve.
    out = vorschau(**VORLADEN, _attrs=reserve_attrs(ve=25.0, min_vor=50.0, avg=200.0))
    assert out == "Akku Netzladen"
    assert "Peak-Vorladen" in grund(**VORLADEN,
                                    _attrs=reserve_attrs(ve=25.0, min_vor=50.0, avg=200.0))


def test_vorladen_stoppt_bei_reserve():
    out = grund(**{**VORLADEN, "sensor.opti_soc": "36"},
                _attrs=reserve_attrs(ve=25.0, min_vor=50.0, avg=200.0))
    assert "Peak-Vorladen" not in out


def test_vorladen_nicht_bei_kleinem_spread():
    # Peak avg 32 ct - aktuell 25 ct = 7 < 10 -> nicht vorladen.
    out = grund(**{**VORLADEN, "sensor.opti_price_current_ct_kwh": "25"},
                _attrs=reserve_attrs(ve=25.0, min_vor=25.0, avg=32.0))
    assert "Peak-Vorladen" not in out


def test_vorladen_wartet_auf_guenstigstes_fenster():
    # Dip auf 45 ct kommt noch vor der Spitze -> jetzt (50 ct) nicht laden.
    out = grund(**VORLADEN, _attrs=reserve_attrs(ve=25.0, min_vor=45.0, avg=200.0))
    assert "Peak-Vorladen" not in out


def test_vorladen_nicht_ohne_gate():
    out = grund(**{**VORLADEN, "binary_sensor.opti_peak_reserve_aktiv": "off"},
                _attrs=reserve_attrs(ve=25.0, min_vor=50.0, avg=200.0))
    assert "Peak-Vorladen" not in out


def test_vorladen_nicht_ohne_netzladen_schalter():
    out = grund(**{**VORLADEN, "input_boolean.opti_prognose_netzladen": "off"},
                _attrs=reserve_attrs(ve=25.0, min_vor=50.0, avg=200.0))
    assert "Peak-Vorladen" not in out


def test_vorladen_nicht_ohne_preis():
    # M1: hv_cur-Guard - fehlt der aktuelle Preis, darf Peak-Vorladen nicht greifen
    # (cur faellt sonst per float(0) auf 0 zurueck und wuerde den Spread verfaelschen).
    out = grund(**{**VORLADEN, "sensor.opti_price_current_ct_kwh": "unavailable"},
                _attrs=reserve_attrs(ve=25.0, min_vor=50.0, avg=200.0))
    assert "Peak-Vorladen" not in out


def test_vorladen_haelt_bis_reserve_im_lademodus():
    # Stop-Kanten-Band (I2): im Modus 'Akku Netzladen' ist stopband 0, sonst 3.
    # soc 34, ges_res 35: im Lademodus laedt es weiter (34 < 35 - 0), im
    # Dynamisch-Modus nicht mehr (34 >= 35 - 3 = 32).
    fall = {**VORLADEN, "sensor.opti_soc": "34"}
    attrs = reserve_attrs(ve=25.0, min_vor=50.0, avg=200.0)
    out_lademodus = grund(**{**fall, "input_select.akkusteuerung_modus": "Akku Netzladen"},
                          _attrs=attrs)
    assert "Peak-Vorladen" in out_lademodus
    out_dynamisch = grund(**{**fall, "input_select.akkusteuerung_modus": "Akku Dynamisch"},
                          _attrs=attrs)
    assert "Peak-Vorladen" not in out_dynamisch


# --- Task 6: Peak-Leiter ---

LEITER = {
    "sensor.opti_forecast_score": "1",
    "sensor.opti_forecast_score_tomorrow": "1",
    "sensor.opti_peak_reserve_soc": "45",
    "binary_sensor.opti_peak_reserve_aktiv": "on",
    "sensor.opti_price_current_ct_kwh": "50",
    # alte Ladebloecke ruhigstellen: SoC hoch genug, dass keiner greift
    "sensor.opti_soc": "85",
}
LEITER_ATTRS = reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0)


def test_l1_very_expensive_entlaedt():
    out = vorschau(**{**LEITER, "sensor.opti_price_level": "VERY_EXPENSIVE"},
                   _attrs=LEITER_ATTRS)
    assert out == "Akku nur Entladen"


def test_l2_expensive_ueber_ve_reserve_entlaedt():
    # soc 85 > ve_res 30 + 3 -> entladen.
    out = vorschau(**{**LEITER, "sensor.opti_price_level": "EXPENSIVE"},
                   _attrs=LEITER_ATTRS)
    assert out == "Akku nur Entladen"


def test_l3_expensive_unter_ve_reserve_haelt():
    # L3/L4 stehen weiterhin HINTER den alten Ladebloecken (nur L1/L2 wurden
    # vor sie gezogen, Ben-Entscheidung 2026-07-02). Bei soc 31 wuerde ohne
    # prognose_netzladen=off der alte SOC<80-Winterblock VOR L3 greifen
    # (gleicher Modus, aber falscher Zweig) - grund pinnt den Zweig fest.
    fall = {**LEITER, "sensor.opti_price_level": "EXPENSIVE",
            "sensor.opti_soc": "31",
            "input_boolean.opti_prognose_netzladen": "off"}
    assert vorschau(**fall, _attrs=LEITER_ATTRS) == "Akku nur Laden"
    assert "Peak-Leiter L3" in grund(**fall, _attrs=LEITER_ATTRS)


def test_l2_schlaegt_alten_winterblock():
    # L2 steht jetzt VOR dem alten SOC<80-Winterblock: bei EXPENSIVE-Preis und
    # soc 55 > ve_res 30 + 3 gewinnt die Leiter, obwohl der alte Block (prog on,
    # winter on, soc<80, beide Scores <3, p_e) ebenfalls zutreffen wuerde.
    fall = {**LEITER, "sensor.opti_price_level": "EXPENSIVE",
            "sensor.opti_soc": "55",
            "sensor.opti_peak_reserve_soc": "45"}
    out = vorschau(**fall, _attrs=reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0))
    assert out == "Akku nur Entladen"
    assert "Peak-Leiter L2" in grund(**fall, _attrs=reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0))


def test_freigabeband_asymmetrisch():
    # L2 steht jetzt vor den alten Ladebloecken, daher gewinnt die Leiter auch
    # bei prog=on (kein Workaround mehr noetig). ges_res auf 34 gesetzt (statt
    # LEITER-Default 45), sonst wuerde die noch frueher stehende Peak-Vorlade-
    # regel greifen (soc 34 < ges_res waere sonst erfuellt und prog-gated).
    # soc 34, ve_res 30: beim Entladen (Band 3) -> 34 > 33 -> weiter entladen.
    fall = {**LEITER, "sensor.opti_price_level": "EXPENSIVE",
            "sensor.opti_soc": "34", "sensor.opti_peak_reserve_soc": "34"}
    out = vorschau(**{**fall, "input_select.akkusteuerung_modus": "Akku nur Entladen"},
                   _attrs=LEITER_ATTRS)
    assert out == "Akku nur Entladen"
    # gleicher SoC, aber gerade am Halten (Band 5) -> 34 <= 35 -> halten bleibt.
    out = vorschau(**{**fall, "input_select.akkusteuerung_modus": "Akku nur Laden"},
                   _attrs=LEITER_ATTRS)
    assert out == "Akku nur Laden"


def test_l4_normal_unter_gesamtreserve_haelt():
    # ges_res 45, soc 40 <= 45+3 -> halten. (SoC 40 statt 85, alte Bloecke:
    # Preis NORMAL + score 1 + soc < 75 wuerde alten Block treffen -> Toggle aus.)
    out = vorschau(**{**LEITER, "sensor.opti_price_level": "NORMAL",
                      "sensor.opti_soc": "40",
                      "input_boolean.opti_prognose_netzladen": "off"},
                   _attrs=LEITER_ATTRS)
    assert out == "Akku nur Laden"
    assert "Peak-Leiter L4" in grund(**{**LEITER, "sensor.opti_price_level": "NORMAL",
                                        "sensor.opti_soc": "40",
                                        "input_boolean.opti_prognose_netzladen": "off"},
                                     _attrs=LEITER_ATTRS)


def test_l4_normal_mit_genug_reserve_normalbetrieb():
    # soc 85 > 45+3 -> keine Leiter-Option, Ziel-SoC-Logik uebernimmt (85 > 60+3).
    out = vorschau(**{**LEITER, "sensor.opti_price_level": "NORMAL"},
                   _attrs=LEITER_ATTRS)
    assert out == "Akku nur Entladen"
    assert "ueber Ziel-SoC" in grund(**{**LEITER, "sensor.opti_price_level": "NORMAL"},
                                     _attrs=LEITER_ATTRS)


# --- Ziel-SoC-Anti-Flatter: asymmetrische Hysterese am "ueber Ziel"-Zweig ---
def test_ueber_ziel_hysterese_deadband():
    # soc 61 knapp ueber Ziel 60, Modus NICHT 'nur Entladen':
    # Eintrittsschwelle target+3=63 noch nicht erreicht -> kein Entladen.
    assert vorschau(**{"sensor.opti_soc": "61"}) == "Akku Dynamisch"


def test_ueber_ziel_hysterese_sticky():
    # Gleicher SoC 61, aber Modus schon 'Akku nur Entladen': Offset 0 ->
    # bleibt entladen bis target erreicht ist (verhindert Minutentakt-Flattern).
    assert vorschau(**{"sensor.opti_soc": "61",
                       "input_select.akkusteuerung_modus": "Akku nur Entladen"}) == "Akku nur Entladen"


def test_ueber_ziel_eintritt_unveraendert():
    # Eintrittsschwelle unveraendert: soc 64 > 60+3 -> Entladen, egal welcher Modus.
    assert vorschau(**{"sensor.opti_soc": "64"}) == "Akku nur Entladen"
    assert vorschau(**{"sensor.opti_soc": "64",
                       "input_select.akkusteuerung_modus": "Akku nur Entladen"}) == "Akku nur Entladen"


def test_ueber_ziel_hysterese_austritt():
    # Tragendes zweites Halbteil: Modus schon 'nur Entladen', soc genau am Ziel
    # (60) -> Freigabe zu Dynamisch, weil soc > target+0 falsch ist. Schuetzt
    # gegen einen '>'->'>=' Regressionsbug (der alle anderen Tests gruen liesse).
    assert vorschau(**{"sensor.opti_soc": "60",
                       "input_select.akkusteuerung_modus": "Akku nur Entladen"}) == "Akku Dynamisch"


def test_leiter_inaktiv_ohne_gate():
    out = grund(**{**LEITER, "sensor.opti_price_level": "EXPENSIVE",
                   "sensor.opti_soc": "31",
                   "binary_sensor.opti_peak_reserve_aktiv": "off"},
                _attrs=LEITER_ATTRS)
    assert "Peak-Leiter" not in out


# --- Tuning-Runde: Hebel 2 (L3-Halte-Spread) ---

L3_HALTE_FALL = {
    **LEITER, "sensor.opti_price_level": "EXPENSIVE",
    "sensor.opti_soc": "31",  # unter VE-Reserve (30 + Band)
    "input_boolean.opti_prognose_netzladen": "off",
    "input_number.opti_halte_spread_ct": "3",
}


def test_l3_halte_spread_zu_klein_faellt_durch():
    # cur=50 (LEITER), ve_avg=52 -> Spread 2 < halte_spread 3 -> KEIN Halten,
    # faellt zur restlichen Kette durch (hier: Default, da Nacht).
    attrs = reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0, ve_avg=52.0)
    assert vorschau(**L3_HALTE_FALL, _attrs=attrs) == "Akku Dynamisch"
    assert "Peak-Leiter L3" not in grund(**L3_HALTE_FALL, _attrs=attrs)


def test_l3_halte_spread_ausreichend_haelt():
    # cur=50, ve_avg=55 -> Spread 5 >= halte_spread 3 -> Halten wie bisher.
    attrs = reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0, ve_avg=55.0)
    assert vorschau(**L3_HALTE_FALL, _attrs=attrs) == "Akku nur Laden"
    assert "Peak-Leiter L3" in grund(**L3_HALTE_FALL, _attrs=attrs)


def test_l3_kein_halten_bei_unavailable_preissensor():
    # hv_cur guard: preis_current_ct_kwh unavailable -> L3 greift nicht.
    # Szenario: Fixture EXPENSIVE, SoC < VE-Reserve, halte_spread erfuellt waere,
    # ABER Sensor fehlt -> keine L3-Aktion, faellt durch zur Default-Kette.
    attrs = reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0, ve_avg=55.0)
    # price_current_ct_kwh ist unavailable (nicht gesetzt in overrides)
    out = grund(**{**L3_HALTE_FALL, "sensor.opti_price_current_ct_kwh": "unavailable"},
                _attrs=attrs)
    assert "Peak-Leiter L3" not in out


# --- Ueberschuss-Regeln (entprellt, 2026-07-03): Vorschau konsumiert Binaries ---

UEBERSCHUSS_TAG = {"sun.sun": "above_horizon", "sensor.opti_soc": "70",
                   "sensor.opti_target_soc": "50"}


def test_ueberschuss_70_schaltet_dynamisch():
    out = vorschau(**UEBERSCHUSS_TAG,
                   **{"binary_sensor.opti_ueberschuss_70_aktiv": "on"})
    assert out == "Akku Dynamisch"
    assert "70% Ueberschuss" in grund(
        **UEBERSCHUSS_TAG, **{"binary_sensor.opti_ueberschuss_70_aktiv": "on"})


def test_ueberschuss_ac_schaltet_dynamisch():
    assert "AC Ueberschuss" in grund(
        **UEBERSCHUSS_TAG, **{"binary_sensor.opti_ueberschuss_ac_aktiv": "on"})


def test_ueberschuss_nicht_nachts():
    g = grund(**{**UEBERSCHUSS_TAG, "sun.sun": "below_horizon",
                 "binary_sensor.opti_ueberschuss_70_aktiv": "on"})
    assert "Ueberschuss" not in g


def test_ueberschuss_gate_aus():
    g = grund(**{**UEBERSCHUSS_TAG,
                 "input_boolean.opti_pv_ueberschuss_ladung": "off",
                 "binary_sensor.opti_ueberschuss_70_aktiv": "on"})
    assert "Ueberschuss" not in g


def test_ueberschuss_nicht_bei_vollem_akku():
    g = grund(**{**UEBERSCHUSS_TAG, "sensor.opti_soc": "100",
                 "binary_sensor.opti_ueberschuss_70_aktiv": "on"})
    assert "Ueberschuss" not in g


def test_ueberschuss_binary_unavailable_ist_aus():
    # Fail-safe: unavailable darf nie wie "on" wirken.
    g = grund(**{**UEBERSCHUSS_TAG,
                 "binary_sensor.opti_ueberschuss_70_aktiv": "unavailable"})
    assert "Ueberschuss" not in g


# --- Feature #31: Balancing-/Deep-Charge-Watchdog ---
# Zwei Ebenen: (1) der abgeleitete sensor.opti_balancing_watchdog (aus/pv/netz)
# aus den Rohwerten, (2) die Vorschau-Kaskade, die diesen State auf einen Modus
# abbildet. Die End-to-End-Faelle rechnen zuerst den Watchdog-State und speisen
# ihn dann in die Vorschau (wie live: Sensor -> Strategie).

# Basis fuer den Watchdog-Sensor: faellig (Intervall 14, counter 14, soc 40<100).
# prog='on': das Netzlade-Gate ist offen, damit die 'netz'-Zweige greifen koennen.
WD_BASIS = {
    "sensor.opti_soc": "40",
    "counter.tage_seit_akku100": "14",
    "input_number.opti_balancing_intervall_tage": "14",
    "input_number.opti_balancing_karenz_tage": "3",
    "input_number.opti_balancing_max_ct": "25",
    "input_number.opti_balancing_done_soc": "98.5",
    "input_number.opti_einspeiseverguetung_ct": "8",
    "sensor.opti_price_current_ct_kwh": "30",
    "sensor.opti_price_level": "NORMAL",
    # Eigener Wartungs-Schalter fuers Balancing-Netzladen (entkoppelt von
    # opti_prognose_netzladen). Default aus; hier an, damit die netz-Faelle greifen.
    "input_boolean.opti_balancing_netzladen": "on",
    "sun.sun": "below_horizon",
}


def watchdog(**overrides):
    states = dict(WD_BASIS)
    states.update(overrides)
    hass = FakeHass(states=states)
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    entity = find_template_entity(cfg, "sensor", "opti_balancing_watchdog")
    return render(hass, entity["state"])


def vorschau_e2e(**overrides):
    """End-to-End: Watchdog-State berechnen und in die Vorschau einspeisen."""
    wd = watchdog(**overrides)
    return vorschau(**{**overrides, "sensor.opti_balancing_watchdog": wd})


# (a) faellig + Tag -> PV -> Vorschau "Akku nur Laden"
def test_watchdog_tag_pv():
    assert watchdog(**{"sun.sun": "above_horizon"}) == "pv"
    assert vorschau_e2e(**{"sun.sun": "above_horizon"}) == "Akku nur Laden"


# (b) faellig + Nacht + cur < eeg -> Gratis-Netz -> "Akku Netzladen"
def test_watchdog_gratis_netz():
    fall = {"sun.sun": "below_horizon", "sensor.opti_price_current_ct_kwh": "3"}
    assert watchdog(**fall) == "netz"
    assert vorschau_e2e(**fall) == "Akku Netzladen"


# (c) faellig + Nacht + nach Karenz + CHEAP + cur <= maxct -> bezahltes Netz
def test_watchdog_bezahltes_netz_nach_karenz():
    fall = {"sun.sun": "below_horizon", "counter.tage_seit_akku100": "17",
            "sensor.opti_price_level": "CHEAP",
            "sensor.opti_price_current_ct_kwh": "20"}
    assert watchdog(**fall) == "netz"
    assert vorschau_e2e(**fall) == "Akku Netzladen"


# (d) faellig + Nacht + CHEAP + cur > maxct -> faellt durch (kein Netzladen)
def test_watchdog_ueber_deckel_faellt_durch():
    fall = {"sun.sun": "below_horizon", "counter.tage_seit_akku100": "17",
            "sensor.opti_price_level": "CHEAP",
            "sensor.opti_price_current_ct_kwh": "30"}  # > maxct 25
    assert watchdog(**fall) == "aus"


# (e) faellig + Nacht + vor Karenz -> kein bezahltes Netzladen
def test_watchdog_vor_karenz_kein_bezahltes_netz():
    # counter 14 = faellig, aber < intervall+karenz (17) -> CHEAP-Fallback aus.
    fall = {"sun.sun": "below_horizon", "counter.tage_seit_akku100": "14",
            "sensor.opti_price_level": "CHEAP",
            "sensor.opti_price_current_ct_kwh": "20"}
    assert watchdog(**fall) == "aus"


# (f) counter < intervall -> Watchdog aus
def test_watchdog_nicht_faellig():
    assert watchdog(**{"counter.tage_seit_akku100": "13",
                       "sun.sun": "above_horizon"}) == "aus"


# (g) soc >= 100 -> Watchdog aus (auch bei Tag/faelligem Counter)
def test_watchdog_akku_voll_haelt_pv():
    # SoC 100, Counter noch nicht resettet -> Watchdog haelt 'pv'. Das fruehere
    # soc<100-Gate ist bewusst weg: sonst kappte der maxsoc-Deckel bei 100 % zu
    # frueh und der Balancing-Zyklus (Counter-Reset braucht 30 min > done_soc)
    # wurde nie fertig.
    assert watchdog(**{"sensor.opti_soc": "100",
                       "sun.sun": "above_horizon"}) == "pv"
    # Erst der Counter-Reset beendet den Zyklus wirklich.
    assert watchdog(**{"sensor.opti_soc": "100", "sun.sun": "above_horizon",
                       "counter.tage_seit_akku100": "0"}) == "aus"


# (h) L1/L2-Peak schlaegt den Watchdog (Watchdog steht in der Kaskade dahinter)
def test_watchdog_peak_hat_vorrang():
    # Watchdog waere 'netz' (Nacht, cur<eeg), aber ein aktiver VERY_EXPENSIVE-Peak
    # (L1) steht davor -> "Akku nur Entladen".
    fall = {"sun.sun": "below_horizon", "sensor.opti_price_current_ct_kwh": "3",
            "sensor.opti_soc": "85", "sensor.opti_price_level": "VERY_EXPENSIVE",
            "binary_sensor.opti_peak_reserve_aktiv": "on",
            "sensor.opti_peak_reserve_soc": "45",
            "_attrs": reserve_attrs(ve=30.0, min_vor=50.0, avg=200.0)}
    assert watchdog(**{k: v for k, v in fall.items() if k != "_attrs"}) == "netz"
    out = vorschau(**{**fall, "sensor.opti_balancing_watchdog": "netz"})
    assert out == "Akku nur Entladen"


# (i) maxct = 0 -> kein bezahltes Netzladen (fail-safe Erststart)
def test_watchdog_maxct_null_kein_bezahltes_netz():
    # cur 20 >= EEG 8, damit NICHT der Gratis-Netz-Zweig greift und wirklich der
    # bezahlte Fallback getestet wird: maxct 0 -> kein Netzladen.
    fall = {"sun.sun": "below_horizon", "counter.tage_seit_akku100": "17",
            "sensor.opti_price_level": "CHEAP",
            "sensor.opti_price_current_ct_kwh": "20",
            "input_number.opti_balancing_max_ct": "0"}
    assert watchdog(**fall) == "aus"


# Intervall 0 -> Watchdog global aus (auch bei faelligem Counter)
def test_watchdog_intervall_null_global_aus():
    assert watchdog(**{"input_number.opti_balancing_intervall_tage": "0",
                       "sun.sun": "above_horizon"}) == "aus"


# Netzlade-Gate: 'netz' respektiert den EIGENEN Schalter opti_balancing_netzladen
# (entkoppelt von opti_prognose_netzladen). Default aus -> Balancing rein per PV.
def test_watchdog_netz_respektiert_balancing_schalter():
    # Gratis-Netz (cur 3 < EEG 8) waere 'netz', aber Schalter aus -> 'aus'
    # (harte Netzlade-Garantie bleibt erhalten).
    fall = {"sun.sun": "below_horizon", "sensor.opti_price_current_ct_kwh": "3"}
    assert watchdog(**{**fall, "input_boolean.opti_balancing_netzladen": "off"}) == "aus"
    # Gegentest: Schalter an -> 'netz'.
    assert watchdog(**{**fall, "input_boolean.opti_balancing_netzladen": "on"}) == "netz"


def test_watchdog_bezahltes_netz_respektiert_balancing_schalter():
    # Auch der bezahlte Fallback haengt am eigenen Schalter.
    fall = {"sun.sun": "below_horizon", "counter.tage_seit_akku100": "17",
            "sensor.opti_price_level": "CHEAP", "sensor.opti_price_current_ct_kwh": "20"}
    assert watchdog(**{**fall, "input_boolean.opti_balancing_netzladen": "off"}) == "aus"
    assert watchdog(**{**fall, "input_boolean.opti_balancing_netzladen": "on"}) == "netz"


def test_watchdog_netz_unabhaengig_von_prognose_gate():
    # Der Balancing-Netzschalter ist ENTKOPPELT von opti_prognose_netzladen:
    # Balancing darf ans Netz, auch wenn die allgemeine Prognose-Netzladung aus ist.
    fall = {"sun.sun": "below_horizon", "sensor.opti_price_current_ct_kwh": "3",
            "input_boolean.opti_balancing_netzladen": "on",
            "input_boolean.opti_prognose_netzladen": "off"}
    assert watchdog(**fall) == "netz"


def test_watchdog_pv_ungegatet_vom_netzschalter():
    # 'pv' zieht keinen Netzstrom -> vom Netzlade-Schalter unberuehrt.
    assert watchdog(**{"sun.sun": "above_horizon",
                       "input_boolean.opti_balancing_netzladen": "off"}) == "pv"


# Vorschau-Mapping direkt: pv -> nur Laden, netz -> Netzladen.
def test_watchdog_vorschau_mapping():
    assert vorschau(**{"sensor.opti_balancing_watchdog": "pv"}) == "Akku nur Laden"
    assert vorschau(**{"sensor.opti_balancing_watchdog": "netz"}) == "Akku Netzladen"
    assert "Balancing-Watchdog (PV" in grund(**{"sensor.opti_balancing_watchdog": "pv"})
    assert "Balancing-Watchdog (Netz" in grund(**{"sensor.opti_balancing_watchdog": "netz"})
