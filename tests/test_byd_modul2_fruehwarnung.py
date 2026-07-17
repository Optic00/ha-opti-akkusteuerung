"""Tests fuer packages/byd_modul2_fruehwarnung.yaml (BYD Modul-2 Fruehwarnung
nativ ueber Modbus): Zellmin, Zell-Absackung (Metrik A), Entladeband,
Bestaetigungszaehler mit Mess-Anker, Netto-Sensor, Latch-Snapshot (Metrik B)
und die Struktur-Garantien der 5 Automationen.

GRENZE DIESER HARNESS (bewusst, wie test_byd_monitoring.py): die Harness rendert
ausschliesslich Jinja-Templates aus dem YAML. Die eigentliche Trigger-/for:-/
Restore-/Listener-Reihenfolge-Semantik von HA, die utility_meter-Baselines und
das Registry-Verhalten sind hier NICHT abbildbar - dafuer existieren die
Struktur-Asserts (unten) und der E2E-Livetest.
"""
import datetime as dt
import re

import jinja2

from .ha_harness import (REPO, TZ, FakeHass, find_template_entity, load_yaml,
                         render)

PACKAGE = REPO / "packages" / "byd_modul2_fruehwarnung.yaml"
AVG = "sensor.bms_1_cells_average_voltage"


def _entity(kind, unique_id):
    return find_template_entity(load_yaml(PACKAGE), kind, unique_id)


def _auto(auto_id):
    return next(a for a in load_yaml(PACKAGE)["automation"] if a["id"] == auto_id)


def _slug(name):
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_")


# ---------------------------------------------------------------------------
# 1) Jinja-Parse-Walk ueber das ganze Package (Repo-Konvention)
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
# 2) Slug-Test: unique_id (_nativ) -> erwartete kanonische entity_id fuer ALLE
# Template-Entities. Nach dem Orphan-Purge beweist das die Live-IDs, an denen
# die Dashboard-Karten und die Automationen haengen.
# ---------------------------------------------------------------------------

NAMING = [
    ("binary_sensor", "byd_entladeband_nativ", "byd_entladeband"),
    ("sensor", "byd_modul2_zellmin_nativ", "byd_modul_2_zellmin"),
    ("sensor", "byd_netto_energie_seit_voll_nativ", "byd_nettoenergie_seit_voll"),
    ("sensor", "byd_modul2_zell_absackung_nativ", "byd_modul_2_zell_absackung"),
    ("sensor", "byd_knie_bestaetigungen_nativ", "byd_knie_bestaetigungen"),
    ("sensor", "byd_modul2_netto_bis_knie_nativ", "byd_modul_2_nettoenergie_bis_knie"),
]


def test_name_slugt_zur_kanonischen_entity_id():
    for domain, unique_id, erwartet in NAMING:
        name = _entity(domain, unique_id)["name"]
        assert _slug(name) == erwartet, (
            f"{domain}/{unique_id}: name '{name}' -> {domain}.{_slug(name)}, "
            f"aber referenziert wird {domain}.{erwartet}")


# ---------------------------------------------------------------------------
# 3) Zellmin: min(cell_voltages m:2)/1000, 3 Dezimalen, Availability
# ---------------------------------------------------------------------------

def _cv(m2_min_mv=3250, m2_min_zelle=23, extra=None):
    """5 Module x 32 Zellen, Grundniveau 3300 mV. Modul 2 hat sein Minimum
    an Zelle m2_min_zelle. extra=(modul, zelle, mv) setzt einen weiteren Wert."""
    daten = [{"m": m, "v": [3300] * 32} for m in range(1, 6)]
    daten[1]["v"][m2_min_zelle - 1] = m2_min_mv
    if extra:
        m, z, mv = extra
        daten[m - 1]["v"][z - 1] = mv
    return daten


def _cv_ohne_m2():
    return [{"m": m, "v": [3300] * 32} for m in (1, 3, 4, 5)]


def _zellmin():
    return _entity("sensor", "byd_modul2_zellmin_nativ")


def _render_zellmin(feld, daten):
    hass = FakeHass(states={AVG: "3.300"}, attrs={AVG: {"cell_voltages": daten}})
    ent = _zellmin()
    return render(hass, ent["state"] if feld == "state" else ent[feld])


def test_zellmin_extrahiert_modul2_minimum():
    assert float(_render_zellmin("state", _cv(m2_min_mv=3190))) == 3.19


def test_zellmin_rundet_auf_drei_dezimalen():
    assert _render_zellmin("state", _cv(m2_min_mv=3187)) == "3.187"


def test_zellmin_availability():
    assert _render_zellmin("availability", _cv()) == "True"
    # Ohne Attribut ueberhaupt.
    hass = FakeHass(states={AVG: "3.300"})
    assert render(hass, _zellmin()["availability"]) == "False"
    # Attribut da, aber kein m:2-Eintrag.
    hass2 = FakeHass(states={AVG: "3.300"}, attrs={AVG: {"cell_voltages": _cv_ohne_m2()}})
    assert render(hass2, _zellmin()["availability"]) == "False"


# ---------------------------------------------------------------------------
# 4) Zell-Absackung (Metrik A): Median(160) - min(m:2) in mV
# ---------------------------------------------------------------------------

def _absackung():
    return _entity("sensor", "byd_modul2_zell_absackung_nativ")


def _render_absackung(feld, daten):
    hass = FakeHass(states={AVG: "3.300",
                            "sensor.battery_management_unit_state_of_charge": "60",
                            "sensor.bmu_power": "800"},
                    attrs={AVG: {"cell_voltages": daten}})
    ent = _absackung()
    tpl = ent["state"] if feld == "state" else ent["attributes"][feld]
    return render(hass, tpl)


def test_absackung_state_und_zell_id():
    daten = _cv(m2_min_mv=3250, m2_min_zelle=23)
    assert float(_render_absackung("state", daten)) == 50.0
    assert _render_absackung("zelle", daten) == "23"
    assert _render_absackung("schwaechstes_modul", daten) == "2"
    assert float(_render_absackung("median_mv", daten)) == 3300.0


def test_absackung_schwaechstes_modul_wechselt():
    # Modul 4 stellt mit 3200 mV das globale Minimum, Modul 2 bleibt bei 3250:
    # die Absackung misst weiter Modul 2, schwaechstes_modul zeigt aber Modul 4.
    daten = _cv(m2_min_mv=3250, m2_min_zelle=23, extra=(4, 10, 3200))
    assert float(_render_absackung("state", daten)) == 50.0
    assert _render_absackung("schwaechstes_modul", daten) == "4"
    assert _render_absackung("zelle", daten) == "23"


def test_absackung_kontext_attribute():
    daten = _cv(m2_min_mv=3250)
    assert _render_absackung("soc", daten) == "60"
    assert _render_absackung("leistung_w", daten) == "800"


def test_absackung_availability_ohne_attribut_oder_ohne_m2():
    # Finding 6: ohne Attribut UND ohne m:2-Eintrag unavailable (sonst laeuft
    # min() im State auf einer leeren Liste in einen Template-Error).
    assert render(FakeHass(states={AVG: "3.300"}), _absackung()["availability"]) == "False"
    hass_m2 = FakeHass(states={AVG: "3.300"}, attrs={AVG: {"cell_voltages": _cv_ohne_m2()}})
    assert render(hass_m2, _absackung()["availability"]) == "False"
    hass_ok = FakeHass(states={AVG: "3.300"}, attrs={AVG: {"cell_voltages": _cv()}})
    assert render(hass_ok, _absackung()["availability"]) == "True"


# ---------------------------------------------------------------------------
# 5) Entladeband: 500 <= bmu_power <= 1500 UND bmu_frisch
# ---------------------------------------------------------------------------

def _entladeband(power=None, bmu_frisch="on"):
    states = {"binary_sensor.byd_bmu_frisch": bmu_frisch}
    if power is not None:
        states["sensor.bmu_power"] = power
    return render(FakeHass(states=states), _entity("binary_sensor", "byd_entladeband_nativ")["state"])


def test_entladeband_grenzen():
    assert _entladeband(power="500") == "True"
    assert _entladeband(power="1500") == "True"
    assert _entladeband(power="800") == "True"
    assert _entladeband(power="499") == "False"
    assert _entladeband(power="1501") == "False"


def test_entladeband_laden_nie_im_band():
    # Negativ = Laden - darf nie im Entladeband liegen.
    assert _entladeband(power="-800") == "False"
    assert _entladeband(power="-1500") == "False"


def test_entladeband_ohne_frische_bmu():
    # Eingefrorene 800 W duerfen kein Band halten.
    assert _entladeband(power="800", bmu_frisch="off") == "False"
    assert _entladeband(power="800", bmu_frisch="unavailable") == "False"


def test_entladeband_fehlende_werte_off():
    assert _entladeband() == "False"
    assert _entladeband(power="unavailable") == "False"


# ---------------------------------------------------------------------------
# 6) Bestaetigungszaehler: konsekutive qualifizierende Zyklen + Mess-Anker
# ---------------------------------------------------------------------------

NOW = dt.datetime(2026, 7, 17, 21, 0, 0, tzinfo=TZ)


def _counter():
    return _entity("sensor", "byd_knie_bestaetigungen_nativ")


def _counter_hass(*, m2min_mv=3190, status="armed", frisch="on", band="on",
                  ref="3.20", cycle="C1", netto="5.0", absackung="30.0",
                  prev="0", seen="C1", abstand=None,
                  netto_anker=None, absackung_anker=None, now=NOW):
    this_attrs = {}
    if seen is not None:
        this_attrs["cycle_id_gesehen"] = seen
    if abstand is not None:
        this_attrs["letzter_zyklus"] = now.timestamp() - abstand
    if netto_anker is not None:
        this_attrs["netto_bei_erstem_sample"] = netto_anker
    if absackung_anker is not None:
        this_attrs["absackung_bei_erstem_sample"] = absackung_anker
    states = {
        "input_select.byd_knie_zyklus_status": status,
        "binary_sensor.byd_zelldaten_frisch": frisch,
        "binary_sensor.byd_entladeband": band,
        "input_number.byd_knie_ref_frozen": ref,
        "input_text.byd_knie_cycle_id": cycle,
        "sensor.byd_nettoenergie_seit_voll": netto,
        "sensor.byd_modul_2_zell_absackung": absackung,
    }
    return FakeHass(states=states, attrs={AVG: {"cell_voltages": _cv(m2_min_mv=m2min_mv)}},
                    this_attributes=this_attrs, this_state=prev, now=now)


def _count(**kw):
    return render(_counter_hass(**kw), _counter()["state"])


def _count_attr(feld, **kw):
    return render(_counter_hass(**kw), _counter()["attributes"][feld])


def test_zaehler_erstes_qualifizierendes_sample():
    # Selber Zyklus (seen == cycle), noch kein Streak (letzter fehlt -> Abstand
    # riesig), qualifizierend -> Streak-Start bei 1.
    assert _count(prev="0", seen="C1", cycle="C1", abstand=None) == "1"


def test_zaehler_zweites_qualifizierendes_sample():
    # Gleiche cycle_id, Abstand im gueltigen Fenster -> Inkrement auf 2.
    assert _count(prev="1", seen="C1", cycle="C1", abstand=630) == "2"


def test_zaehler_cycle_id_wechsel_nullt_bedingungslos():
    # Finding 3: der cycle_id-Wechsel ist das Reset-Event, KEIN Sample. Auch
    # qualifizierend darf er nicht auf 1 zaehlen (sonst latcht das erste echte
    # Folge-Sample bereits bei Stand 2).
    assert _count(prev="2", seen="C1", cycle="C2", abstand=630) == "0"
    # ... und selbst mit riesigem Abstand bleibt es beim Reset auf 0.
    assert _count(prev="0", seen="C1", cycle="C2", abstand=None) == "0"


def test_zaehler_reset_dann_erstes_echtes_sample_ist_eins():
    # Nach dem Reset (seen wurde auf C2 gesetzt, letzter=0) erzeugt das erste
    # echte cells_average_voltage-Sample im selben Zyklus Stand 1 - nicht mehr.
    assert _count(prev="0", seen="C2", cycle="C2", abstand=None) == "1"


def test_zaehler_qual_ohne_netto_zaehlt_nicht():
    # Finding 2: ist der Netto-Sensor unavailable, ist das Sample nicht
    # qualifizierend (kein gueltiger Mess-Anker moeglich) -> 0.
    assert _count(prev="1", seen="C1", cycle="C1", abstand=630, netto="unavailable") == "0"


def test_zaehler_band_bruch_nullt():
    assert _count(prev="1", seen="C1", cycle="C1", abstand=630, band="off") == "0"


def test_zaehler_frisch_off_nullt():
    assert _count(prev="1", seen="C1", cycle="C1", abstand=630, frisch="off") == "0"


def test_zaehler_ueber_referenz_nullt():
    # m2min 3250 mV = 3,25 V >= ref 3,20 V -> nicht qualifizierend.
    assert _count(prev="1", seen="C1", cycle="C1", abstand=630, m2min_mv=3250) == "0"


def test_zaehler_doppel_event_dedupe():
    # Zwei Events desselben Samples (< 300 s Abstand) -> kein Inkrement.
    assert _count(prev="1", seen="C1", cycle="C1", abstand=100) == "1"


def test_zaehler_luecke_startet_neu_bei_eins():
    # > 1500 s Abstand = Kontinuitaet gebrochen -> Neustart bei 1.
    assert _count(prev="1", seen="C1", cycle="C1", abstand=2000) == "1"


def test_zaehler_cycle_id_gesehen_immer_aktuell():
    # Auch nicht-qualifizierend wird die aktuelle cycle_id gemerkt.
    assert _count_attr("cycle_id_gesehen", cycle="C7", band="off") == "C7"


def test_mess_anker_wird_bei_stand_eins_gesetzt():
    # Frischer Uebergang auf 1 (selber Zyklus, letzter=0): netto aus dem
    # Momentanwert, Absackung/Zell-Nr/Modul aus DEMSELBEN cell_voltages-Attribut.
    kw = dict(prev="0", seen="C1", cycle="C1", abstand=None, netto="5.0", m2min_mv=3190)
    assert _count_attr("netto_bei_erstem_sample", **kw) == "5.0"
    # Median(160)=3300, min(m:2)=3190 -> 110 mV; Zelle 23 (Default-Fixture); Modul 2.
    assert float(_count_attr("absackung_bei_erstem_sample", **kw)) == 110.0
    assert _count_attr("zelle_bei_erstem_sample", **kw) == "23"
    assert _count_attr("schwaechstes_modul_bei_erstem_sample", **kw) == "2"


def test_mess_anker_absackung_aus_attribut_nicht_aus_gegatetem_sensor():
    # Finding 1: der Anker rechnet die Absackung aus dem eigenen Attribut, nicht
    # aus states('sensor.byd_modul_2_zell_absackung'). Der (bewusst abweichende)
    # Momentanwert des gegateten Sensors darf keine Rolle spielen.
    # m2min 3195 mV = 3,195 V < ref 3,20 -> qualifiziert; Absackung = 3300-3195.
    assert float(_count_attr("absackung_bei_erstem_sample",
                             prev="0", seen="C1", cycle="C1", abstand=None,
                             m2min_mv=3195, absackung="999")) == 105.0


def test_mess_anker_wird_bei_stand_zwei_nicht_ueberschrieben():
    # Inkrement auf 2: die Momentanwerte duerfen die am ersten Sample geankerten
    # Werte NICHT ueberschreiben.
    assert _count_attr("netto_bei_erstem_sample",
                       prev="1", seen="C1", cycle="C1", abstand=630,
                       netto="9.9", netto_anker="5.0") == "5.0"
    assert _count_attr("absackung_bei_erstem_sample",
                       prev="1", seen="C1", cycle="C1", abstand=630,
                       m2min_mv=3100, absackung_anker="30.0") == "30.0"


# ---------------------------------------------------------------------------
# 7) Netto-Sensor: entladen - geladen, Availability
# ---------------------------------------------------------------------------

GEL = "sensor.byd_geladen_seit_voll"
ENT = "sensor.byd_entladen_seit_voll"


def _netto():
    return _entity("sensor", "byd_netto_energie_seit_voll_nativ")


def test_netto_differenz():
    hass = FakeHass(states={ENT: "6.2", GEL: "0.3"})
    assert float(render(hass, _netto()["state"])) == 5.9


def test_netto_availability():
    assert render(FakeHass(states={ENT: "6.2", GEL: "0.3"}), _netto()["availability"]) == "True"
    assert render(FakeHass(states={ENT: "6.2"}), _netto()["availability"]) == "False"
    assert render(FakeHass(states={GEL: "0.3"}), _netto()["availability"]) == "False"
    assert render(FakeHass(states={ENT: "unavailable", GEL: "0.3"}),
                  _netto()["availability"]) == "False"


# ---------------------------------------------------------------------------
# 8) Latch-Snapshot (Metrik B): Mess-Anker-Werte, nicht der Momentanwert
# ---------------------------------------------------------------------------

COUNTER = "sensor.byd_knie_bestaetigungen"


def _latch():
    return _entity("sensor", "byd_modul2_netto_bis_knie_nativ")


def _latch_hass():
    return FakeHass(states={
        "sensor.byd_nettoenergie_seit_voll": "9.9",     # Momentanwert (NICHT geankert)
        "sensor.byd_modul_2_zell_absackung": "44.0",    # Momentanwert (NICHT geankert)
        "sensor.byd_geladen_seit_voll": "0.2",
        "sensor.byd_entladen_seit_voll": "6.1",
        "sensor.battery_management_unit_state_of_charge": "22",
        "sensor.bmu_power": "700",
        "sensor.byd_modul_2_zellmin": "3.19",
        "input_number.byd_knie_ref_frozen": "3.20",
        "input_text.byd_knie_cycle_id": "C1",
    }, attrs={COUNTER: {"netto_bei_erstem_sample": "5.8",
                        "absackung_bei_erstem_sample": "38.0",
                        "zelle_bei_erstem_sample": "23",
                        "schwaechstes_modul_bei_erstem_sample": "2"}}, now=NOW)


def test_latch_state_aus_mess_anker():
    # State kommt aus dem Zaehler-Anker (5.8), NICHT aus dem Momentanwert (9.9).
    assert render(_latch_hass(), _latch()["state"]) == "5.8"


def test_latch_attribute_aus_mess_anker():
    hass = _latch_hass()
    attrs = _latch()["attributes"]
    assert render(hass, attrs["netto_kwh"]) == "5.8"
    assert render(hass, attrs["a_absackung_mv"]) == "38.0"
    # Zell-Nr und Modul kommen ebenfalls aus dem Anker (gleicher Zyklus wie die
    # Absackung), nicht vom gegateten Momentan-Sensor.
    assert render(hass, attrs["modul2_schwaechste_zelle"]) == "23"
    assert render(hass, attrs["schwaechstes_modul"]) == "2"
    # Kontext kommt weiter aus den Momentanwerten.
    assert render(hass, attrs["entladen_inkrement_kwh"]) == "6.1"
    assert render(hass, attrs["sauberer_zyklus"]) == "True"   # geladen 0.2 < 0.5
    assert render(hass, attrs["cycle_id"]) == "C1"


# ---------------------------------------------------------------------------
# 9) Struktur-Asserts: die Trigger-/for:-Semantik ist nicht testbar, ihre
# STRUKTUR aber schon - genau die Punkte, an denen der Plan (E2) haengt.
# ---------------------------------------------------------------------------

def _entities_in(node):
    gefunden = []

    def walk(n):
        if isinstance(n, dict):
            e = n.get("entity_id")
            if isinstance(e, str):
                gefunden.append(e)
            elif isinstance(e, list):
                gefunden.extend(x for x in e if isinstance(x, str))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return gefunden


def _templates_in(node):
    gefunden = []

    def walk(n):
        if isinstance(n, dict):
            t = n.get("value_template")
            if isinstance(t, str):
                gefunden.append(t)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return gefunden


def test_voll_anker_prueft_beide_frische_binaries_und_meter_has_value():
    va = _auto("byd_voll_anker")
    entities = _entities_in(va["conditions"])
    assert "binary_sensor.byd_bmu_frisch" in entities
    assert "binary_sensor.byd_zelldaten_frisch" in entities
    joined = " ".join(_templates_in(va["conditions"]))
    for meter in ("sensor.byd_geladen_seit_voll", "sensor.byd_entladen_seit_voll",
                  "sensor.byd_nettoenergie_seit_voll"):
        assert f"has_value('{meter}')" in joined, meter


def _turn_off_targets(node):
    """entity_ids aller input_boolean.turn_off-Aktionen unter node."""
    gefunden = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("action") == "input_boolean.turn_off":
                gefunden.extend(_entities_in(n.get("target", {})))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return gefunden


def test_latch_ist_state_trigger_auf_zaehler_mit_ge_zwei_und_anker_guard():
    latch = _auto("byd_knie_latch")
    trigger = latch["triggers"]
    assert any(t.get("trigger") == "state"
               and t.get("entity_id") == COUNTER for t in trigger)
    # KEIN numeric_state-Trigger (der saehe nur die Flanke, nicht den Retry).
    assert all(t.get("trigger") != "numeric_state" for t in trigger)
    cond_texts = " ".join(_templates_in(latch["conditions"]))
    assert ">= 2" in cond_texts
    # Finding 2: der Mess-Anker muss numerisch sein, sonst kein Latch.
    assert "netto_bei_erstem_sample" in cond_texts and "|float(none)) is not none" in cond_texts


def test_latch_setzt_ueberschwelle_guard_zurueck():
    # Finding 7: sonst bliebe der Guard dauerhaft an (vestigial).
    assert "input_boolean.byd_knie_ueberschwelle_gesehen" in _turn_off_targets(_auto("byd_knie_latch")["actions"])


def test_invalid_naehe_check_ist_fail_safe_float_null():
    inv = _auto("byd_knie_invalid")
    # Naehe-Check liegt jetzt in den Aktionen (choose-Sequenz), nicht in conditions.
    texts = " ".join(_templates_in(inv["actions"]))
    # float(0) statt float(9): unbekannt zaehlt als nah am Knie -> verwerfen.
    assert "sensor.byd_modul_2_zellmin')|float(0)" in texts
    assert "float(9)" not in texts


def test_invalid_neustart_wartet_auf_frische_zelldaten():
    # Finding 5: der neustart-Zweig darf den Naehe-Check nicht sofort werten
    # (zellmin nach Boot unavailable -> float(0) invalidierte jeden Restart).
    inv = _auto("byd_knie_invalid")

    def find_waits(node):
        found = []

        def walk(n):
            if isinstance(n, dict):
                if "wait_template" in n:
                    found.append(n)
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)

        walk(node)
        return found

    waits = find_waits(inv["actions"])
    assert any("has_value('sensor.byd_modul_2_zellmin')" in w["wait_template"]
               and w.get("continue_on_timeout") is True for w in waits)


def test_invalid_schaltet_armed_und_ueberschwelle_ab():
    # Finding 4 + 7: status=invalid muss armed loeschen (Zustandswiderspruch)
    # und den Ueberschwelle-Guard zuruecksetzen - in BEIDEN choose-Zweigen.
    off = _turn_off_targets(_auto("byd_knie_invalid")["actions"])
    assert off.count("input_boolean.byd_knie_armed") >= 2
    assert off.count("input_boolean.byd_knie_ueberschwelle_gesehen") >= 2


def test_invalid_datenluecke_deckt_beide_frische_binaries_ab():
    inv = _auto("byd_knie_invalid")
    luecke = [t.get("entity_id") for t in inv["triggers"] if t.get("id") == "datenluecke"]
    assert "binary_sensor.byd_bmu_frisch" in luecke
    assert "binary_sensor.byd_zelldaten_frisch" in luecke


# Selbst-Verdrahtung: jede byd_-prefixte Entity, die das Package referenziert,
# muss hier definiert sein ODER aus dem gepinnten byd_monitoring-Vertrag stammen.
# Faengt Tippfehler wie byd_modul2_zellmin statt byd_modul_2_zellmin.
DEFINIERT = {
    # Template-Slugs
    "sensor.byd_modul_2_zellmin", "sensor.byd_nettoenergie_seit_voll",
    "sensor.byd_modul_2_zell_absackung", "sensor.byd_knie_bestaetigungen",
    "sensor.byd_modul_2_nettoenergie_bis_knie", "binary_sensor.byd_entladeband",
    # utility_meter (entity_id = Konfig-Key)
    "sensor.byd_geladen_seit_voll", "sensor.byd_entladen_seit_voll",
    # Helfer
    "input_number.byd_knie_referenzspannung", "input_number.byd_knie_ref_frozen",
    "input_boolean.byd_knie_armed", "input_boolean.byd_knie_ueberschwelle_gesehen",
    "input_boolean.byd_top_erreicht", "input_select.byd_knie_zyklus_status",
    "input_text.byd_knie_cycle_id", "input_text.byd_knie_invalid_grund",
    "input_datetime.byd_voll_anker_zeit",
}
# Aus packages/byd_monitoring.yaml (gepinnter Vertrag).
VERTRAG = {"binary_sensor.byd_bmu_frisch", "binary_sensor.byd_zelldaten_frisch"}
REF_RE = re.compile(
    r"(?:sensor|binary_sensor|input_number|input_boolean|input_select|"
    r"input_text|input_datetime)\.byd_[a-z0-9_]+")


def _alle_strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _alle_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _alle_strings(v)
    elif isinstance(node, str):
        yield node


def test_alle_byd_referenzen_sind_definiert_oder_im_vertrag():
    # Ueber die GEPARSTE Struktur laufen, nicht ueber den Rohtext - so bleiben
    # die im Header dokumentierten Orphan-Purge-IDs (Kommentare) aussen vor.
    erlaubt = DEFINIERT | VERTRAG
    refs = set()
    for s in _alle_strings(load_yaml(PACKAGE)):
        refs.update(REF_RE.findall(s))
    unbekannt = sorted(r for r in refs if r not in erlaubt)
    assert unbekannt == [], f"undefinierte byd-Referenzen: {unbekannt}"
