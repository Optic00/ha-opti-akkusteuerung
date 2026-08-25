"""Entprellte Ueberschuss-Binärsensoren (Anti-Flatter, 2026-07-03).

Hintergrund: Die Ueberschuss-Regeln pruefen bis dahin Momentanwerte, und das
70%-Signal (opti_grid_export_w) ist ueber den Akku rueckgekoppelt: Laden drueckt
den Export unter die Grenze, die Regel kippt zurueck, Laden stoppt, Export
steigt - Selbstschwingung im Minutentakt (live 2026-07-03, 11:25-13:35).
Fix: akkuunabhaengiges Signal (Export + Batterieleistung signed = Export ohne
Akku-Eingriff) + Hysterese-Band + 30-s-Entprellung (delay_on/delay_off).
"""
from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

BASIS = {
    "sensor.opti_grid_export_w": "0",
    "sensor.opti_battery_power_w": "0",
    "sensor.opti_pv_power_w": "0",
    "input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": "18000",
    "input_number.akkusteuerung_wr_ac_ueberschuss_grenze": "9500",
}


def _entity(uid):
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    return find_template_entity(cfg, "binary_sensor", uid)


def _render(uid, overrides, this_state="off"):
    states = dict(BASIS)
    states.update(overrides)
    hass = FakeHass(states=states, this_state=this_state)
    return render(hass, _entity(uid)["state"])


def b70(this_state="off", **overrides):
    return _render("opti_ueberschuss_70_aktiv", overrides, this_state)


def bac(this_state="off", **overrides):
    return _render("opti_ueberschuss_ac_aktiv", overrides, this_state)


# --- 70%-Ueberschuss: akkuunabhaengiges Signal ---

def test_70_ein_bei_export_ueber_grenze():
    assert b70(**{"sensor.opti_grid_export_w": "19000"}) == "True"


def test_70_aus_weit_unter_grenze():
    assert b70(this_state="on", **{"sensor.opti_grid_export_w": "5000"}) == "False"


def test_70_akkuunabhaengig_laden_zaehlt_dazu():
    # DER Flatterfall: Akku laedt 5 kW, Export faellt dadurch auf 14 kW.
    # Ohne Akku waeren es 19 kW -> Signal muss AN bleiben.
    assert b70(this_state="on", **{"sensor.opti_grid_export_w": "14000",
                                   "sensor.opti_battery_power_w": "5000"}) == "True"


def test_70_entladen_wird_rausgerechnet():
    # Akku entlaedt 5 kW in den Export: gemessen 18.5 kW, ohne Akku 13.5 kW.
    assert b70(**{"sensor.opti_grid_export_w": "18500",
                  "sensor.opti_battery_power_w": "-5000"}) == "False"


def test_70_hysterese_haelt_im_band():
    # 17.5 kW liegt im Band (aus < 17000, ein > 18000): Zustand bleibt.
    zw = {"sensor.opti_grid_export_w": "17500"}
    assert b70(this_state="on", **zw) == "True"
    assert b70(this_state="off", **zw) == "False"


def test_70_kanten_exakt_auf_grenze():
    # Genau == ein (18000): noch KEIN Einschalten (Bedingung ist strikt >)...
    assert b70(**{"sensor.opti_grid_export_w": "18000"}) == "False"
    # ...aber Halten, wenn schon an (Band schliesst die Ein-Kante ein).
    assert b70(this_state="on", **{"sensor.opti_grid_export_w": "18000"}) == "True"
    # Genau == aus (17000): Band ist inklusiv -> an bleibt an.
    assert b70(this_state="on", **{"sensor.opti_grid_export_w": "17000"}) == "True"
    # Knapp darunter: aus.
    assert b70(this_state="on", **{"sensor.opti_grid_export_w": "16999"}) == "False"


def test_70_failsafe_bei_unavailable():
    assert b70(this_state="on",
               **{"sensor.opti_grid_export_w": "unavailable"}) == "False"
    assert b70(this_state="on",
               **{"sensor.opti_battery_power_w": "unavailable"}) == "False"


# --- AC-Ueberschuss (Hybrid-WR-AC-Limit): gleiche Mechanik ---

def test_ac_ein_bei_pv_ueber_grenze():
    assert bac(**{"sensor.opti_pv_power_w": "9700"}) == "True"


def test_ac_akkuunabhaengig_laden_zaehlt_dazu():
    # Akku laedt 4 kW DC-seitig, AC-Ausgang faellt auf 6 kW - ohne Akku 10 kW.
    assert bac(this_state="on", **{"sensor.opti_pv_power_w": "6000",
                                   "sensor.opti_battery_power_w": "4000"}) == "True"


def test_ac_hysterese_haelt_im_band():
    zw = {"sensor.opti_pv_power_w": "9300"}
    assert bac(this_state="on", **zw) == "True"
    assert bac(this_state="off", **zw) == "False"


def test_ac_aus_unter_band():
    assert bac(this_state="on", **{"sensor.opti_pv_power_w": "8000"}) == "False"


def test_ac_failsafe_bei_unavailable():
    assert bac(this_state="on",
               **{"sensor.opti_pv_power_w": "unavailable"}) == "False"


# --- Entprellung: 30 s beidseitig wie in der alten Opti-2.0-Automatik ---

def test_delay_on_off_30s():
    for uid in ("opti_ueberschuss_70_aktiv", "opti_ueberschuss_ac_aktiv"):
        entity = _entity(uid)
        assert entity["delay_on"] == {"seconds": 30}, uid
        assert entity["delay_off"] == {"seconds": 30}, uid


# --- Strategie-Automation konsumiert nur noch die entprellten Sensoren ---

def _automation():
    return load_yaml(REPO / "automations" / "opti_strategie.yaml")


def _flat(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def test_automation_trigger_nutzen_binaersensoren():
    triggers = _flat(_automation()[0]["triggers"])
    assert "binary_sensor.opti_ueberschuss_70_aktiv" in triggers
    assert "binary_sensor.opti_ueberschuss_ac_aktiv" in triggers
    # Keine Rohwert-Ueberschuss-Trigger mehr (Entprellung lebt im Sensor).
    assert "akkusteuerung_wr_70proz_ueberschuss_grenze" not in triggers
    assert "akkusteuerung_wr_ac_ueberschuss_grenze" not in triggers


def test_automation_conditions_nutzen_binaersensoren():
    from .ha_harness import find_automation_condition
    cfg = _automation()
    c70 = _flat(find_automation_condition(
        cfg, "Modus 'Dynamisch': 70% PV-Überschuss in den Akku (nur tagsüber)"))
    cac = _flat(find_automation_condition(
        cfg, "Modus 'Dynamisch': AC-PV-Überschuss in den Akku (nur tagsüber)"))
    assert "binary_sensor.opti_ueberschuss_70_aktiv" in c70
    assert "sensor.opti_grid_export_w" not in c70
    assert "binary_sensor.opti_ueberschuss_ac_aktiv" in cac
    assert "sensor.opti_pv_power_w" not in cac


# --- Erststart-Guard: Grenze 0 = nicht konfiguriert = Funktion aus (2026-08-25) ---
# Ein frisch angelegter input_number ohne `initial:` startet auf seinem Minimum.
# Bei den Ueberschuss-Grenzen ist das 0 - ohne Guard waere der Sensor damit
# dauerhaft an, sobald ueberhaupt exportiert wird, und der Override wuerde den
# Ziel-SoC bei jedem Erstaufsetzer stechen (Issue #64).

def test_70_grenze_null_ist_aus_trotz_export():
    assert b70(**{"input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": "0",
                  "sensor.opti_grid_export_w": "19000"}) == "False"


def test_ac_grenze_null_ist_aus_trotz_erzeugung():
    assert bac(**{"input_number.akkusteuerung_wr_ac_ueberschuss_grenze": "0",
                  "sensor.opti_pv_power_w": "10000"}) == "False"


def test_70_grenze_null_haelt_auch_nicht_nach():
    # Halteband darf einen bereits aktiven Sensor nicht am Leben halten,
    # wenn die Grenze auf 0 zurueckfaellt.
    assert b70(this_state="on",
               **{"input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": "0",
                  "sensor.opti_grid_export_w": "19000"}) == "False"


def test_ac_grenze_null_haelt_auch_nicht_nach():
    assert bac(this_state="on",
               **{"input_number.akkusteuerung_wr_ac_ueberschuss_grenze": "0",
                  "sensor.opti_pv_power_w": "10000"}) == "False"


def test_kleine_positive_grenze_funktioniert_weiter():
    # Der Guard darf nur 0 (bzw. negativ) abschneiden, nicht kleine Grenzen.
    assert b70(**{"input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": "500",
                  "sensor.opti_grid_export_w": "600"}) == "True"


def test_fehlender_helfer_bleibt_fail_safe():
    # Nicht existierender Helfer -> float(999999) -> praktisch nie an.
    assert b70(**{"input_number.akkusteuerung_wr_70proz_ueberschuss_grenze": "unknown",
                  "sensor.opti_grid_export_w": "19000"}) == "False"

# --- Wirtschaftliches Ueberschuss-Veto (2026-08-15) -------------------------
# Die beiden Sensoren oben sind Abregelungs-Waechter (18000 / 9500 W) und lagen
# am 15.08.2026 durchgehend off, waehrend 19,3 kWh ins Netz gingen und der
# Ziel-SoC-Deckel den Akku ~2,5 h auf Ladeleistung 0 zwang. Dieser Sensor ist
# das fehlende wirtschaftliche Signal.

VETO_BASIS = {
    "sensor.opti_grid_export_w": "0",
    "sensor.opti_grid_import_w": "0",
    "sensor.opti_battery_power_w": "0",
    "input_number.akkusteuerung_ueberschuss_veto_grenze": "500",
    "input_number.akkusteuerung_ueberschuss_veto_aus_grenze": "250",
    "input_number.akkusteuerung_ueberschuss_veto_knappheit_faktor": "3",
}


def bveto(this_state="off", forecast_attrs=None, **overrides):
    states = dict(VETO_BASIS)
    states.update(overrides)
    attrs = ({"sensor.opti_forecast_score": forecast_attrs}
             if forecast_attrs is not None else {})
    hass = FakeHass(states=states, attrs=attrs, this_state=this_state)
    return render(hass, _entity("opti_ueberschuss_veto_aktiv")["state"])


def test_veto_ein_ueber_grenze():
    assert bveto(**{"sensor.opti_grid_export_w": "600"}) == "True"


def test_veto_bleibt_aus_unter_grenze():
    assert bveto(**{"sensor.opti_grid_export_w": "400"}) == "False"


def test_veto_haltet_im_hysteresband():
    # Zwischen Aus- und Ein-Grenze bleibt ein bereits aktives Veto an ...
    assert bveto(this_state="on", **{"sensor.opti_grid_export_w": "300"}) == "True"
    # ... springt aber aus dem Aus-Zustand nicht an.
    assert bveto(this_state="off", **{"sensor.opti_grid_export_w": "300"}) == "False"


def test_veto_aus_unter_aus_grenze():
    assert bveto(this_state="on", **{"sensor.opti_grid_export_w": "100"}) == "False"


def test_veto_akkuunabhaengig_laden_zaehlt_dazu():
    # DER Rueckkopplungsfall: der Akku laedt mit 3 kW und drueckt den gemessenen
    # Export auf 0. Ohne Akku waeren es 3 kW -> das Signal darf sich nicht selbst
    # abschalten, sonst schwingt der Modus (Live-Bug vom 03.07.2026).
    assert bveto(this_state="on", **{"sensor.opti_grid_export_w": "0",
                                     "sensor.opti_battery_power_w": "3000"}) == "True"


def test_veto_entladen_wird_rausgerechnet():
    # Akku entlaedt 800 W in den Export: gemessen 900 W, ohne Akku nur 100 W.
    assert bveto(**{"sensor.opti_grid_export_w": "900",
                    "sensor.opti_battery_power_w": "-800"}) == "False"


def test_veto_fail_safe_bei_unavailable():
    for quelle in ("sensor.opti_grid_export_w", "sensor.opti_battery_power_w"):
        assert bveto(this_state="on", **{quelle: "unavailable"}) == "False", quelle


def test_veto_delay_on_off_60s():
    # 60 s statt 30 s: die Schwelle liegt unter der Hausgrundlast-Schwankung.
    entity = _entity("opti_ueberschuss_veto_aktiv")
    assert entity["delay_on"] == {"seconds": 60}
    assert entity["delay_off"] == {"seconds": 60}


def test_veto_trigger_und_condition_in_automation():
    from .ha_harness import find_automation_condition
    cfg = _automation()
    assert "binary_sensor.opti_ueberschuss_veto_aktiv" in _flat(cfg[0]["triggers"])
    cond = _flat(find_automation_condition(
        cfg, "Modus 'Dynamisch': echter Netz-Ueberschuss sticht Ziel-SoC-Deckel"))
    assert "binary_sensor.opti_ueberschuss_veto_aktiv" in cond
    # Kein Rohwert-Vergleich im Zweig (Entprellung lebt im Sensor).
    assert "sensor.opti_grid_export_w" not in cond
    # Harter maxsoc-Deckel bleibt gewahrt.
    assert "input_number.maxsoc" in cond


def test_veto_helfer_erststart_werte_brauchbar():
    """Review-Finding 15.08.2026: ein input_number ohne gespeicherten Zustand
    startet auf `min`. Ohne brauchbare Minima stuende das Veto beim Erststart auf
    einer unsinnigen Schwelle - und `aus = 0` haette das Halteband abgeschaltet.
    `initial:` ist bewusst KEINE Loesung (ueberschreibt den restaurierten Wert bei
    jedem Neustart), deshalb muessen die Minima selbst tragen."""
    cfg = load_yaml(REPO / "packages" / "sma_helpers.yaml")["input_number"]
    ein = cfg["akkusteuerung_ueberschuss_veto_grenze"]
    aus = cfg["akkusteuerung_ueberschuss_veto_aus_grenze"]
    assert "initial" not in ein and "initial" not in aus
    assert ein["min"] >= 200, "Erststart-Schwelle zu tief"
    assert aus["min"] > 0, "Halteband beim Erststart wirkungslos"
    assert aus["min"] < ein["min"], "Aus-Grenze muss unter der Ein-Grenze liegen"


def test_veto_knappheits_faktor_helfer_hat_konservativen_erststart():
    cfg = load_yaml(REPO / "packages" / "sma_helpers.yaml")["input_number"]
    faktor = cfg["akkusteuerung_ueberschuss_veto_knappheit_faktor"]
    assert "initial" not in faktor
    assert faktor["min"] == 1
    assert faktor["max"] == 10
    assert faktor["step"] == 0.5
    assert faktor["mode"] == "box"
    assert "unit_of_measurement" not in faktor


# --- Import-Term (Review Fable 5, 2026-08-15) -------------------------------
# Erst `export - import + battery` ist exakt `PV - Hauslast`. Ohne den
# Import-Term meldet der Sensor beim Netzladen Ueberschuss, wo gerade Strom
# gekauft wird.

def test_veto_netzladen_ist_kein_ueberschuss():
    """DER Fall: Akku laedt mit 3 kW aus dem Netz. Export 0, Import 3000,
    battery +3000. Ohne Import-Term ergaebe die Formel 3000 W 'Ueberschuss'."""
    assert bveto(**{"sensor.opti_grid_export_w": "0",
                    "sensor.opti_grid_import_w": "3000",
                    "sensor.opti_battery_power_w": "3000"}) == "False"


def test_veto_netzbezug_haus_ist_kein_ueberschuss():
    # Nacht: PV 0, Hauslast 1000 W aus dem Netz, Akku idle.
    assert bveto(this_state="on", **{"sensor.opti_grid_export_w": "0",
                                     "sensor.opti_grid_import_w": "1000",
                                     "sensor.opti_battery_power_w": "0"}) == "False"


def test_veto_import_sensor_unavailable_fail_safe():
    assert bveto(this_state="on", **{"sensor.opti_grid_export_w": "2000",
                                     "sensor.opti_grid_import_w": "unavailable"}) == "False"


def test_veto_im_zielregime_unveraendert():
    # Export > 0 => Import = 0: der Term aendert nichts am bisherigen Verhalten.
    assert bveto(**{"sensor.opti_grid_export_w": "600",
                    "sensor.opti_grid_import_w": "0"}) == "True"
    assert bveto(**{"sensor.opti_grid_export_w": "0",
                    "sensor.opti_grid_import_w": "0",
                    "sensor.opti_battery_power_w": "3000"}) == "True"


# --- Knappheits-Gate (2026-08-16) ------------------------------------------

def test_veto_knappheits_gate_zu_am_reichtag_trotz_export():
    attrs = {"pv_surplus_kwh": 51.5, "needed_full_kwh": 4.5}
    assert bveto(forecast_attrs=attrs,
                 **{"sensor.opti_grid_export_w": "600"}) == "False"


def test_veto_knappheits_gate_offen_bei_unterdeckung():
    attrs = {"pv_surplus_kwh": 10.0, "needed_full_kwh": 4.5}
    assert bveto(forecast_attrs=attrs,
                 **{"sensor.opti_grid_export_w": "600"}) == "True"


def test_veto_knappheits_gate_halteband_verhindert_selbstabschaltung():
    # Bei Faktor 3 liegt 15 kWh zwischen der Ein-Schwelle 13,5 kWh und der
    # Halte-Schwelle 16,2 kWh. Nur ein bereits aktives Veto darf offen bleiben.
    attrs = {"pv_surplus_kwh": 15.0, "needed_full_kwh": 4.5}
    assert bveto(this_state="on", forecast_attrs=attrs,
                 **{"sensor.opti_grid_export_w": "600"}) == "True"
    assert bveto(this_state="off", forecast_attrs=attrs,
                 **{"sensor.opti_grid_export_w": "600"}) == "False"


def test_veto_knappheits_gate_fail_open_bei_fehlendem_forecast():
    # Fehlende Attribute und explizite None-Werte duerfen den real gemessenen
    # Export nicht unterdruecken.
    assert bveto(**{"sensor.opti_grid_export_w": "600"}) == "True"
    attrs = {"pv_surplus_kwh": None, "needed_full_kwh": None}
    assert bveto(forecast_attrs=attrs,
                 **{"sensor.opti_grid_export_w": "600"}) == "True"


def test_veto_knappheits_gate_zu_wenn_akku_rechnerisch_voll():
    attrs = {"pv_surplus_kwh": 0.0, "needed_full_kwh": 0.0}
    assert bveto(this_state="on", forecast_attrs=attrs,
                 **{"sensor.opti_grid_export_w": "600"}) == "False"


def test_veto_knappheits_faktor_kommt_aus_helfer():
    attrs = {"pv_surplus_kwh": 10.0, "needed_full_kwh": 4.0}
    assert bveto(forecast_attrs=attrs,
                 **{"sensor.opti_grid_export_w": "600",
                    "input_number.akkusteuerung_ueberschuss_veto_knappheit_faktor": "2"}) == "False"
    assert bveto(forecast_attrs=attrs,
                 **{"sensor.opti_grid_export_w": "600",
                    "input_number.akkusteuerung_ueberschuss_veto_knappheit_faktor": "3"}) == "True"


def test_veto_knappheits_attribute_sind_beobachtbar():
    entity = _entity("opti_ueberschuss_veto_aktiv")
    hass = FakeHass(
        states=VETO_BASIS,
        attrs={"sensor.opti_forecast_score": {
            "pv_surplus_kwh": 15.0,
            "needed_full_kwh": 4.5,
        }},
        this_state="on",
    )
    assert render(hass, entity["attributes"]["knappheit_faktor"]) == "3.0"
    assert render(hass, entity["attributes"]["pv_surplus_kwh"]) == "15.0"
    assert render(hass, entity["attributes"]["needed_full_kwh"]) == "4.5"
    assert render(hass, entity["attributes"]["knappheit_gate_offen"]) == "True"
