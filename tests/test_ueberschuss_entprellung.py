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


# --- Wirtschaftliches Ueberschuss-Veto (2026-08-15) -------------------------
# Die beiden Sensoren oben sind Abregelungs-Waechter (18000 / 9500 W) und lagen
# am 15.08.2026 durchgehend off, waehrend 19,3 kWh ins Netz gingen und der
# Ziel-SoC-Deckel den Akku ~2,5 h auf Ladeleistung 0 zwang. Dieser Sensor ist
# das fehlende wirtschaftliche Signal.

VETO_BASIS = {
    "sensor.opti_grid_export_w": "0",
    "sensor.opti_battery_power_w": "0",
    "input_number.akkusteuerung_ueberschuss_veto_grenze": "500",
    "input_number.akkusteuerung_ueberschuss_veto_aus_grenze": "250",
}


def bveto(this_state="off", **overrides):
    states = dict(VETO_BASIS)
    states.update(overrides)
    hass = FakeHass(states=states, this_state=this_state)
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
