from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

BASIS = {
    "sensor.opti_soc": "40",
    "sensor.opti_battery_capacity_kwh": "12.8",
    "sensor.opti_forecast_score": "5",
    "sensor.opti_forecast_score_tomorrow": "5",
    "sensor.opti_price_level": "NORMAL",
    "sensor.opti_target_soc": "60",
    "sensor.opti_price_current_ct_kwh": "30",
    "sensor.opti_grid_export_w": "0",
    "sensor.opti_pv_power_w": "0",
    "sensor.opti_peak_reserve_soc": "unavailable",
    "binary_sensor.opti_peak_reserve_aktiv": "off",
    "binary_sensor.opti_winter_charging_allowed": "on",
    "input_number.minsoc": "10",
    "input_number.maxsoc": "95",
    "input_number.opti_einspeiseverguetung_ct": "8",
    "input_number.opti_netzlade_spread_ct": "10",
    "input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": "500",
    "input_number.akkusteuerung_wr_ac_ueberschuss_grenze": "4500",
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


def reserve_attrs(ve=30.0, min_vor=None, avg=None):
    return {"sensor.opti_peak_reserve_soc": {
        "reserve_ve_soc": ve, "min_preis_vor_peak_ct": min_vor,
        "peak_preis_avg_ct": avg}}


# --- Task 4: Negativpreis-Laderegel ---

def test_negativpreis_laedt():
    # Preis 3 ct < EEG 8 ct, Prognose schlecht, kein guenstigeres Fenster bekannt.
    assert vorschau(**{"sensor.opti_price_current_ct_kwh": "3",
                       "sensor.opti_forecast_score": "1"}) == "Akku nur Laden"
    assert "Negativpreis" in grund(**{"sensor.opti_price_current_ct_kwh": "3",
                                      "sensor.opti_forecast_score": "1"})


def test_negativpreis_wartet_auf_guenstigeres_fenster():
    out = vorschau(**{"sensor.opti_price_current_ct_kwh": "3",
                      "sensor.opti_forecast_score": "1",
                      "sensor.opti_peak_reserve_soc": "35",
                      "_attrs": reserve_attrs(min_vor=-2.0, avg=40.0)})
    assert out != "Akku nur Laden"  # -2 ct kommt noch -> warten


def test_negativpreis_marge_gleiche_preise():
    # min_vor = 2.6, aktuell 3.0 -> innerhalb 0.5-ct-Marge -> laden.
    assert vorschau(**{"sensor.opti_price_current_ct_kwh": "3",
                       "sensor.opti_forecast_score": "1",
                       "sensor.opti_peak_reserve_soc": "35",
                       "_attrs": reserve_attrs(min_vor=2.6, avg=40.0)}) == "Akku nur Laden"


def test_negativpreis_nicht_bei_guter_prognose():
    assert vorschau(**{"sensor.opti_price_current_ct_kwh": "3",
                       "sensor.opti_forecast_score": "7"}) != "Akku nur Laden"


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
                       "input_boolean.opti_prognose_netzladen": "off"}) != "Akku nur Laden"


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
    assert out == "Akku nur Laden"
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
