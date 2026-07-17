"""Tests fuer packages/byd_monitoring.yaml (BYD-Monitoring nativ ueber Modbus):
Ruhefenster-Gate, Frische-Binaries, Ruhe-Median-3, Temperatur-Spreizung,
Zell-Ausreisser, Balancing-Binary und die beiden unique_id-Carries.

GRENZE DIESER HARNESS (bewusst dokumentiert): die Harness rendert ausschliesslich
Jinja-Templates aus dem YAML. Die eigentliche Trigger-/for:-Semantik von HA ist
hier NICHT abbildbar - also weder "eingefrorene Werte erfuellen for: per
Stagnation", noch das Verwerfen von for:-Timern beim Reload, noch der Gate-
Wechsel nach Schwellen-Ueberschreitung. Genau fuer diese Klasse existieren die
Frische-Binaries (als Bedingung in der Automation), der Dead-Man-Watchdog und
der E2E-Livetest - nicht diese Datei.
"""
import datetime as dt

import jinja2

from .ha_harness import (REPO, TZ, FakeHass, find_template_entity, load_yaml,
                         render, render_native)

PACKAGE = REPO / "packages" / "byd_monitoring.yaml"


def _entity(kind, unique_id):
    return find_template_entity(load_yaml(PACKAGE), kind, unique_id)


# ---------------------------------------------------------------------------
# Jinja-Parse-Check ueber die ganze Package-Struktur (Repo-Konvention)
# ---------------------------------------------------------------------------

def _walk_templates(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_templates(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_templates(v, f"{path}[{i}]")
    elif isinstance(node, str) and ("{{" in node or "{%" in node):
        yield path, node


def _parse_fails(env, template_str):
    try:
        env.parse(template_str)
        return False
    except jinja2.TemplateSyntaxError:
        return True


def test_package_jinja_parst():
    env = jinja2.Environment()
    fehler = [p for p, t in _walk_templates(load_yaml(PACKAGE)) if _parse_fails(env, t)]
    assert fehler == []


# ---------------------------------------------------------------------------
# unique_id-Carries: DIE Registry-/Historien-Stabilitaets-Garantie.
# Bleiben diese IDs gleich, behalten sensor.byd_zellspreizung_ruhe (4x mit
# Steuerwirkung in opti_derived!) und sensor.byd_temperatur_spreizung ihre
# entity_id - kein Konsument muss angefasst werden.
# ---------------------------------------------------------------------------

def test_unique_id_carries_vorhanden():
    assert _entity("sensor", "byd_bmu_zellspreizung_ruhe_mv")["name"] == "BYD Zellspreizung Ruhe"
    assert _entity("sensor", "byd_bmu_temp_spreizung_k")["name"] == "BYD Temperatur-Spreizung"


# ---------------------------------------------------------------------------
# Ruhefenster: |bmu_power| < 300 W UND 25 < SoC < 85
# ---------------------------------------------------------------------------

def _ruhefenster(power=None, soc=None, bmu_frisch="on"):
    # bmu_frisch defaultet auf "on": die Frische ist eine eigene Bedingung und
    # wird gezielt in test_ruhefenster_ohne_frische_bmu geprueft.
    states = {"binary_sensor.byd_bmu_frisch": bmu_frisch}
    if power is not None:
        states["sensor.bmu_power"] = power
    if soc is not None:
        states["sensor.battery_management_unit_state_of_charge"] = soc
    return render(FakeHass(states=states), _entity("binary_sensor", "byd_ruhefenster")["state"])


def test_ruhefenster_normal():
    assert _ruhefenster(power="0.0", soc="53") == "True"


def test_ruhefenster_leistungs_grenzen():
    # Vorzeichen egal (negativ = Laden), Betrag zaehlt.
    assert _ruhefenster(power="299", soc="53") == "True"
    assert _ruhefenster(power="-299", soc="53") == "True"
    assert _ruhefenster(power="300", soc="53") == "False"
    assert _ruhefenster(power="-300", soc="53") == "False"
    assert _ruhefenster(power="-2600", soc="53") == "False"


def test_ruhefenster_ohne_frische_bmu():
    # Driver-Haertung: BMU-Poll eingefroren, BMS pollt weiter. power/SoC zeigen
    # dann stale "Ruhe"-Werte, obwohl der Akku unter Last stehen kann. Ohne
    # diese Bedingung wuerde der Ruhe-Sensor ein Last-Delta als Ruhewert latchen
    # - mit Steuerwirkung (Bedarfs-Balancing) und 1-h-Alarm.
    assert _ruhefenster(power="0.0", soc="53", bmu_frisch="off") == "False"
    # unknown/unavailable des Binaries zaehlt ebenfalls als nicht frisch.
    assert _ruhefenster(power="0.0", soc="53", bmu_frisch="unavailable") == "False"
    assert _ruhefenster(power="0.0", soc="53", bmu_frisch="unknown") == "False"


def test_ruhefenster_soc_grenzen():
    # Knie-Bereiche der LFP-Kennlinie bleiben draussen (exklusive Grenzen).
    assert _ruhefenster(power="0", soc="26") == "True"
    assert _ruhefenster(power="0", soc="84") == "True"
    assert _ruhefenster(power="0", soc="25") == "False"
    assert _ruhefenster(power="0", soc="85") == "False"
    assert _ruhefenster(power="0", soc="99") == "False"
    assert _ruhefenster(power="0", soc="17") == "False"


def test_ruhefenster_fehlende_werte_off():
    # Kein Sample ist besser als ein falsches Sample.
    assert _ruhefenster(power="0") == "False"
    assert _ruhefenster(soc="53") == "False"
    assert _ruhefenster() == "False"
    assert _ruhefenster(power="unavailable", soc="53") == "False"
    assert _ruhefenster(power="0", soc="unknown") == "False"


# ---------------------------------------------------------------------------
# Frische-Binaries: now() - updated < 3 min (BMU) bzw. < 25 min (Zelldaten).
# Die updated-Sensoren liefern NAIVE Lokalzeit-Strings; as_timestamp(x, 0)
# interpretiert sie als Lokalzeit, Muell/unavailable -> 0 -> Alter riesig.
# ---------------------------------------------------------------------------

NOW = dt.datetime(2026, 7, 17, 11, 45, 0, tzinfo=TZ)


def _frisch(unique_id, entity_id, updated):
    hass = FakeHass(states={entity_id: updated} if updated is not None else {}, now=NOW)
    return render(hass, _entity("binary_sensor", unique_id)["state"])


def _bmu_frisch(updated):
    return _frisch("byd_bmu_frisch", "sensor.battery_management_unit_updated", updated)


def _zelldaten_frisch(updated):
    return _frisch("byd_zelldaten_frisch",
                   "sensor.battery_management_system_1_bms_1_updated", updated)


def test_bmu_frisch_bei_normalem_poll():
    # Poll alle 30 s - typisches Alter live 17,5 s.
    assert _bmu_frisch("2026-07-17 11:44:42.207325") == "True"
    # 60 s = 2 verpasste Polls, noch frisch.
    assert _bmu_frisch("2026-07-17 11:44:00.000000") == "True"


def test_bmu_frisch_grenze_90_sekunden():
    # Das Fenster MUSS schmaler sein als die kuerzeste Haltezeit, die es
    # schuetzt (zell_hoch: for 2 min) - sonst ist der Guard wirkungslos:
    # eingefrorene Daten haetten nach 2 min erst 120 s Alter und wuerden mit
    # einem 180-s-Fenster noch als "frisch" durchgehen (Codex-Review 17.7.).
    # Untergrenze: breiter als der 30-s-Poll, sonst Flattern im Normalbetrieb.
    assert _bmu_frisch("2026-07-17 11:43:31.000000") == "True"   # 89 s
    assert _bmu_frisch("2026-07-17 11:43:30.000000") == "False"  # 90 s exakt
    assert _bmu_frisch("2026-07-17 11:43:00.000000") == "False"  # 120 s = zell_hoch-Fall
    assert _bmu_frisch("2026-07-17 11:41:59.000000") == "False"


def test_bmu_frisch_unavailable_und_muell_sind_nicht_frisch():
    # as_timestamp(x, 0) -> 0 -> Alter riesig -> off. Fail-Safe-Richtung:
    # lieber "keine Daten" als ein eingefrorener Wert, der Alarme traegt.
    assert _bmu_frisch("unavailable") == "False"
    assert _bmu_frisch("unknown") == "False"
    assert _bmu_frisch("kaputt") == "False"
    assert _bmu_frisch("") == "False"
    assert _bmu_frisch(None) == "False"


def test_zelldaten_frisch_grenze_15_minuten():
    # Gleiche Logik wie beim BMU-Fenster: 900 s muss schmaler sein als die
    # 21-min-Haltezeit von zell_bms_grenze_praezise (sonst Guard wirkungslos)
    # und breiter als der 630-s-Detail-Zyklus (sonst Flattern).
    assert _zelldaten_frisch("2026-07-17 11:42:18.000000") == "True"   # 162 s, live
    assert _zelldaten_frisch("2026-07-17 11:34:30.000000") == "True"   # 630 s = 1 Zyklus
    assert _zelldaten_frisch("2026-07-17 11:30:01.000000") == "True"   # 899 s
    assert _zelldaten_frisch("2026-07-17 11:30:00.000000") == "False"  # 900 s exakt
    assert _zelldaten_frisch("2026-07-17 11:24:00.000000") == "False"  # 21 min = praezise-Fall
    assert _zelldaten_frisch("unavailable") == "False"


# ---------------------------------------------------------------------------
# Ruhe-Spreizung: Median der letzten 3 Gate-Messungen (this.attributes)
# ---------------------------------------------------------------------------

DELTA = "sensor.battery_management_system_1_bms_1_cells_voltage_delta"


def _ruhe():
    return _entity("sensor", "byd_bmu_zellspreizung_ruhe_mv")


def test_ruhe_median_kappt_einzel_ausreisser():
    # Historie unauffaellig (2-3 mV), ein Ausreisser von 40 mV kommt rein:
    # der Median bleibt beim echten Niveau statt den Ausreisser zu latchen.
    hass = FakeHass(states={DELTA: "40"}, this_attributes={"messreihe": [2.0, 3.0]})
    assert float(render(hass, _ruhe()["state"])) == 3.0


def test_ruhe_median_laesst_echtes_niveau_durch():
    hass = FakeHass(states={DELTA: "61"}, this_attributes={"messreihe": [55.0, 58.0]})
    assert float(render(hass, _ruhe()["state"])) == 58.0


def test_ruhe_median_erste_messung_ohne_historie():
    hass = FakeHass(states={DELTA: "4"}, this_attributes={})
    assert float(render(hass, _ruhe()["state"])) == 4.0


def test_ruhe_messreihe_rolliert_auf_drei():
    hass = FakeHass(states={DELTA: "6"}, this_attributes={"messreihe": [3.0, 4.0, 5.0]})
    assert render(hass, _ruhe()["attributes"]["messreihe"]) == "[4.0, 5.0, 6.0]"


def test_ruhe_attribute_kontext():
    hass = FakeHass(states={DELTA: "3",
                            "sensor.battery_management_unit_state_of_charge": "53",
                            "sensor.bmu_power": "0.0"},
                    this_attributes={"messreihe": [2.0, 3.0]}, now=NOW)
    attrs = _ruhe()["attributes"]
    assert render(hass, attrs["soc"]) == "53"
    assert render(hass, attrs["leistung_w"]) == "0.0"
    assert render(hass, attrs["gemessen"]).startswith("2026-07-17T11:45:00")


def test_ruhe_median_ueber_mehrere_trigger_zyklen():
    # Simuliert aufeinanderfolgende Gate-Zyklen: das messreihe-Attribut aus
    # Zyklus n wird (wie HAs this-Snapshot) zum Input von Zyklus n+1.
    entity = _ruhe()
    messreihe = []
    verlauf = []
    for roh in ["2", "3", "40", "3", "2", "2"]:
        hass = FakeHass(states={DELTA: roh}, this_attributes={"messreihe": messreihe})
        verlauf.append(float(render(hass, entity["state"])))
        messreihe = render_native(hass, entity["attributes"]["messreihe"])
        assert isinstance(messreihe, list)
        assert all(isinstance(v, float) for v in messreihe)
    # Der 40er-Ausreisser drueckt nie durch; nach 3 weiteren Messungen ist er
    # aus dem Fenster. Fenster 3 reagiert schneller als das alte Fenster 5 -
    # bewusst, siehe Plan F2 (Einzel-Sample-Robustheit, nicht Skew-Filter).
    assert verlauf == [2.0, 2.0, 3.0, 3.0, 3.0, 2.0]
    assert messreihe == [3.0, 2.0, 2.0]


# ---------------------------------------------------------------------------
# Temperatur-Spreizung: BMU-Temp max - min (ein Poll, 30 s)
# ---------------------------------------------------------------------------

TMAX = "sensor.battery_management_unit_bmu_cell_temperature_max"
TMIN = "sensor.battery_management_unit_bmu_cell_temperature_min"


def _temp_entity():
    return _entity("sensor", "byd_bmu_temp_spreizung_k")


def test_temp_spreizung_normal():
    hass = FakeHass(states={TMAX: "33", TMIN: "27"})
    assert float(render(hass, _temp_entity()["state"])) == 6.0
    assert render(hass, _temp_entity()["availability"]) == "True"


def test_temp_spreizung_live_stichprobe():
    # Live 17.7.: BMU max 32 / min 29 -> 3 K.
    hass = FakeHass(states={TMAX: "32", TMIN: "29"})
    assert float(render(hass, _temp_entity()["state"])) == 3.0


def test_temp_spreizung_negativ_geklemmt():
    hass = FakeHass(states={TMAX: "20", TMIN: "25"})
    assert float(render(hass, _temp_entity()["state"])) == 0.0


def test_temp_spreizung_unavailable_ohne_quelle():
    assert render(FakeHass(states={TMAX: "33"}), _temp_entity()["availability"]) == "False"
    assert render(FakeHass(states={TMIN: "27"}), _temp_entity()["availability"]) == "False"
    assert render(FakeHass(states={TMAX: "33", TMIN: "unavailable"}),
                  _temp_entity()["availability"]) == "False"


# ---------------------------------------------------------------------------
# Balancing-Binary: history_stats-Quelle der KI-Analyse (braucht on/off)
# ---------------------------------------------------------------------------

BAL = "sensor.bms_1_cells_balancing"


def _balancing():
    return _entity("binary_sensor", "byd_balancing_aktiv_nativ")


def test_balancing_binary_aus_zaehlwert():
    assert render(FakeHass(states={BAL: "0"}), _balancing()["state"]) == "False"
    assert render(FakeHass(states={BAL: "1"}), _balancing()["state"]) == "True"
    assert render(FakeHass(states={BAL: "6"}), _balancing()["state"]) == "True"


def test_balancing_binary_availability():
    assert render(FakeHass(states={BAL: "0"}), _balancing()["availability"]) == "True"
    assert render(FakeHass(states={}), _balancing()["availability"]) == "False"
    assert render(FakeHass(states={BAL: "unavailable"}),
                  _balancing()["availability"]) == "False"


# ---------------------------------------------------------------------------
# Zell-Ausreisser: max |Zelle - Median(160)| aus dem cell_voltages-Attribut
# ---------------------------------------------------------------------------

AVG = "sensor.bms_1_cells_average_voltage"


def _cell_voltages(ausreisser=None):
    """5 Module x 32 Zellen (echtes Live-Layout), alle 3302 mV.
    ausreisser: (modul_1basiert, zelle_1basiert, wert_mv)."""
    daten = [{"m": m, "v": [3302] * 32} for m in range(1, 6)]
    if ausreisser:
        m, z, wert = ausreisser
        daten[m - 1]["v"][z - 1] = wert
    return daten


def _ausreisser():
    return _entity("sensor", "byd_zell_ausreisser")


def _render_ausreisser(feld, daten):
    hass = FakeHass(states={AVG: "3.302"}, attrs={AVG: {"cell_voltages": daten}})
    entity = _ausreisser()
    tpl = entity["state"] if feld == "state" else entity["attributes"][feld]
    return render(hass, tpl)


def test_ausreisser_findet_absackende_zelle():
    # Modul 3, Zelle 7 sackt auf 3250 mV ab: Median der 160 Zellen = 3302,
    # groesste Abweichung 52 mV nach unten.
    daten = _cell_voltages((3, 7, 3250))
    assert float(_render_ausreisser("state", daten)) == 52.0
    assert _render_ausreisser("modul", daten) == "3"
    assert _render_ausreisser("zelle", daten) == "7"
    assert _render_ausreisser("richtung", daten) == "tief"
    assert float(_render_ausreisser("median_mv", daten)) == 3302.0


def test_ausreisser_findet_hohe_zelle():
    daten = _cell_voltages((5, 32, 3390))
    assert float(_render_ausreisser("state", daten)) == 88.0
    assert _render_ausreisser("modul", daten) == "5"
    assert _render_ausreisser("zelle", daten) == "32"
    assert _render_ausreisser("richtung", daten) == "hoch"


def test_ausreisser_live_stichprobe_unauffaellig():
    # Live 17.7.: alle Zellen 3301-3303 -> Ausreisser ~1 mV, kein Befund.
    daten = _cell_voltages((5, 22, 3303))
    assert float(_render_ausreisser("state", daten)) == 1.0
    assert _render_ausreisser("richtung", daten) == "hoch"


def test_ausreisser_ohne_abweichung():
    daten = _cell_voltages()
    assert float(_render_ausreisser("state", daten)) == 0.0
    assert _render_ausreisser("richtung", daten) == "keiner"


def test_ausreisser_unavailable_ohne_attribut():
    hass = FakeHass(states={AVG: "3.302"})
    assert render(hass, _ausreisser()["availability"]) == "False"
    hass_ok = FakeHass(states={AVG: "3.302"},
                       attrs={AVG: {"cell_voltages": _cell_voltages()}})
    assert render(hass_ok, _ausreisser()["availability"]) == "True"


# ---------------------------------------------------------------------------
# Alarm-Automation: Variablen-Block
# ---------------------------------------------------------------------------

TEMP_AVG = "sensor.bms_1_cells_average_temperature"


def _alarm_variables():
    cfg = load_yaml(PACKAGE)
    auto = next(a for a in cfg["automation"] if a["id"] == "byd_akku_alarme_nativ")
    return next(a for a in auto["actions"] if "variables" in a)["variables"]


def test_modul_hinweis_wird_vor_alarm_text_definiert():
    # HA rendert variables: SEQUENZIELL und reicht nur die bereits gerenderten
    # Variablen weiter. Steht modul_hinweis hinter alarm_text, rendert es dort
    # still zu einem Leerstring - kein Fehler, nur ein kaputter Alarmtext.
    # Diese Reihenfolge ist deshalb bindend, nicht kosmetisch.
    keys = list(_alarm_variables())
    assert keys.index("modul_hinweis") < keys.index("alarm_text")


def _alarm_automation():
    cfg = load_yaml(PACKAGE)
    return next(a for a in cfg["automation"] if a["id"] == "byd_akku_alarme_nativ")


def _trigger_ids():
    return [t["id"] for t in _alarm_automation()["triggers"]]


def _condition_trigger_ids():
    """Alle in den conditions referenzierten Trigger-IDs (flach)."""
    gefunden = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("condition") == "trigger":
                ids = node["id"]
                gefunden.extend([ids] if isinstance(ids, str) else ids)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(_alarm_automation()["conditions"])
    return gefunden


def test_jede_regel_hat_einen_alarmtext():
    # alarm_text nutzt .get(trigger.id, 'BYD-Alarm: ' ~ trigger.id) - eine
    # vertippte oder vergessene ID faellt also nicht auf, sondern pusht still
    # einen generischen Text. Deshalb hier hart gegenpruefen.
    text = _alarm_variables()["alarm_text"]
    fehlend = [tid for tid in _trigger_ids() if f"'{tid}':" not in text]
    assert fehlend == []


def test_jede_regel_ist_in_den_conditions_abgedeckt():
    # Jede Trigger-ID muss in genau einem condition-Zweig auftauchen: fehlt
    # eine, laesst der or-Block sie durchfallen und die Regel feuert NIE
    # (fail-silent) - der gefaehrlichste Fehler in dieser Automation.
    abgedeckt = _condition_trigger_ids()
    fehlend = [tid for tid in _trigger_ids() if tid not in abgedeckt]
    assert fehlend == []
    # Keine Karteileiche: nichts in den conditions, was es als Trigger nicht gibt.
    assert [tid for tid in abgedeckt if tid not in _trigger_ids()] == []


def test_frische_bedingung_fuer_jeden_physikalischen_alarm():
    # Grundprinzip 2 des Plans: eingefrorene Werte erfuellen for: per
    # Stagnation. Jede physikalische BMU-Regel MUSS deshalb byd_bmu_frisch
    # als Bedingung haben, die BMS-Praezisionsregel byd_zelldaten_frisch.
    bmu_regeln = {"zell_hoch", "zell_kritisch", "zell_bms_grenze", "zell_niedrig",
                  "temp_hoch", "temp_kritisch", "temp_delta"}
    for zweig in _alarm_automation()["conditions"][0]["conditions"]:
        ids = set(_condition_trigger_ids_von(zweig))
        entities = _condition_entities_von(zweig)
        if ids & bmu_regeln:
            assert "binary_sensor.byd_bmu_frisch" in entities, ids
        if "zell_bms_grenze_praezise" in ids:
            assert "binary_sensor.byd_zelldaten_frisch" in entities, ids


def _condition_trigger_ids_von(zweig):
    gefunden = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("condition") == "trigger":
                ids = node["id"]
                gefunden.extend([ids] if isinstance(ids, str) else ids)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(zweig)
    return gefunden


def _condition_entities_von(zweig):
    gefunden = []

    def walk(node):
        if isinstance(node, dict):
            if "entity_id" in node and isinstance(node["entity_id"], str):
                gefunden.append(node["entity_id"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(zweig)
    return gefunden


def _cell_temps(waermstes_modul=None, tmax=None):
    """5 Module x 12 Fuehler (echtes Live-Layout), alle 31 C."""
    daten = [{"m": m, "t": [31] * 12} for m in range(1, 6)]
    if waermstes_modul:
        daten[waermstes_modul - 1]["t"][5] = tmax
    return daten


def test_modul_hinweis_findet_waermstes_modul():
    hass = FakeHass(states={TEMP_AVG: "31.3"},
                    attrs={TEMP_AVG: {"cell_temps": _cell_temps(4, 47)}})
    assert render(hass, _alarm_variables()["modul_hinweis"]) == "Modul 4 mit 47 C"


def test_modul_hinweis_ohne_attribut():
    # Zelldaten weg (Detail-Poll haengt) -> der Temp-Alarm feuert trotzdem,
    # nur ohne Modul-Identifikation.
    hass = FakeHass(states={TEMP_AVG: "31.3"})
    assert render(hass, _alarm_variables()["modul_hinweis"]) == "unbekannt"
