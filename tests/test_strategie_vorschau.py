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
