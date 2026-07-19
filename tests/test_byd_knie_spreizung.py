"""Tests fuer packages/byd_knie_spreizung.yaml.

Die Harness rendert die echten Jinja-Templates gegen synthetische HA-States.
Aufeinanderfolgende Trigger werden simuliert, indem state/attributes aus
Zyklus n als ``this``-Snapshot in Zyklus n+1 eingehen. Damit sind Latch und
Restore-Verhalten pruefbar; die eigentliche HA-State-Trigger-Ausfuehrung wird
wie in den Nachbartests ueber Struktur-Asserts abgesichert.
"""
import datetime as dt
import re

import jinja2

from .ha_harness import REPO, TZ, FakeHass, find_template_entity, load_yaml, render, render_native

PACKAGE = REPO / "packages" / "byd_knie_spreizung.yaml"
AVG = "sensor.bms_1_cells_average_voltage"
SOC = "sensor.battery_management_unit_state_of_charge"
BMU_UPDATED = "sensor.battery_management_unit_updated"
NOW = dt.datetime(2026, 7, 18, 14, 0, 0, tzinfo=TZ)


def _config():
    return load_yaml(PACKAGE)


def _entity():
    return find_template_entity(_config(), "sensor", "byd_knie_spreizung_peak_mv")


def _slug(name):
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_")


def _cell_voltages(overrides=None, basis=3494):
    """Fuenf Module mit je 32 Zellen; overrides: (Modul, Zelle) -> mV."""
    overrides = overrides or {}
    return [
        {
            "m": modul,
            "v": [overrides.get((modul, zelle), basis) for zelle in range(1, 33)],
        }
        for modul in range(1, 6)
    ]


def _sample(spread_mv=40):
    halb = spread_mv // 2
    return _cell_voltages({(1, 1): 3494 - halb, (1, 2): 3494 + spread_mv - halb})


def _live_peak_sample():
    # Live-Bild 18.7. nachgestellt: min 3373, max 3630 -> 257 mV.
    # Median 3494; M5/Z28 liegt +136 mV darueber und ist der groesste
    # absolute Median-Ausreisser (Gegenseite: -121 mV).
    return _cell_voltages({(2, 17): 3373, (5, 28): 3630})


def _hass(daten, soc, *, prev_state="unknown", prev_attrs=None, now=NOW,
          updated_age_s=30):
    updated = (now - dt.timedelta(seconds=updated_age_s)).replace(tzinfo=None).isoformat(sep=" ")
    return FakeHass(
        states={AVG: "3.494", SOC: str(soc), BMU_UPDATED: updated},
        attrs={AVG: {"cell_voltages": daten}},
        this_state=prev_state,
        this_attributes=prev_attrs,
        now=now,
    )


def _step(daten, soc, *, prev_state="unknown", prev_attrs=None, now=NOW):
    entity = _entity()
    hass = _hass(daten, soc, prev_state=prev_state, prev_attrs=prev_attrs, now=now)
    # Echtes HA wertet Zustand und Attribute gegen denselben alten this-Snapshot
    # aus. Deshalb erst alles rendern, danach als neuen Snapshot zusammenbauen.
    state = render_native(hass, entity["state"])
    attrs = {name: render_native(hass, template) for name, template in entity["attributes"].items()}
    return state, attrs


def _walk_templates(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_templates(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_templates(value, f"{path}[{index}]")
    elif isinstance(node, str) and ("{{" in node or "{%" in node):
        yield path, node


def _trigger_block():
    for block in _config()["template"]:
        if any(e.get("unique_id") == "byd_knie_spreizung_peak_mv" for e in block.get("sensor", [])):
            return block
    raise AssertionError("Trigger-Block der Knie-Spreizung fehlt")


def test_package_existiert_und_jinja_parst():
    assert PACKAGE.exists()
    env = jinja2.Environment()
    fehler = []
    for path, template in _walk_templates(_config()):
        try:
            env.parse(template)
        except jinja2.TemplateSyntaxError:
            fehler.append(path)
    assert fehler == []


def test_name_erzeugt_stabile_entity_id_und_keine_helfer():
    assert _slug(_entity()["name"]) == "byd_knie_spreizung_peak"
    assert set(_config()) == {"template"}


def test_trigger_kommt_direkt_vom_cell_voltages_attributtraeger():
    block = _trigger_block()
    assert block["triggers"] == [{"trigger": "state", "entity_id": AVG}]


def test_sample_condition_verlangt_160_zellen_plausiblen_soc_und_frische_bmu():
    template = _trigger_block()["conditions"][0]["value_template"]
    assert render(_hass(_sample(), 95), template) == "True"

    nur_159 = _sample()
    nur_159[-1]["v"].pop()
    assert render(_hass(nur_159, 95), template) == "False"
    nichtnumerisch = _sample()
    nichtnumerisch[2]["v"][7] = "kaputt"
    assert render(_hass(nichtnumerisch, 95), template) == "False"
    assert render(_hass(_sample(), "kaputt"), template) == "False"
    assert render(_hass(_sample(), 101), template) == "False"
    assert render(_hass(_sample(), 95, updated_age_s=89), template) == "True"
    assert render(_hass(_sample(), 95, updated_age_s=90), template) == "False"


def test_kaltstart_unter_95_liefert_none_statt_nichtnumerischem_state():
    # Ein measurement-Sensor mit Einheit darf vor der ersten Vollladung nicht
    # den restaurierten HA-String "unknown" als Zustand ausgeben.
    state, attrs = _step(_sample(20), 80)
    assert state is None
    assert attrs["episode_aktiv"] is False


def test_latch_steigt_beim_neuen_maximum_und_snapshottet_ausreisser():
    state, attrs = _step(_sample(40), 95)
    assert float(state) == 40
    start = attrs["episode_gestartet"]

    peak_time = NOW + dt.timedelta(minutes=11)
    state, attrs = _step(
        _live_peak_sample(), 99, prev_state=state, prev_attrs=attrs, now=peak_time
    )
    assert float(state) == 257
    assert attrs["episode_aktiv"] is True
    assert attrs["episode_gestartet"] == start
    assert attrs["ausreisser_modul"] == 5
    assert attrs["ausreisser_zelle"] == 28
    assert float(attrs["ausreisser_zellspannung_mv"]) == 3630
    assert float(attrs["median_mv"]) == 3494
    assert float(attrs["delta_zum_median_mv"]) == 136
    assert attrs["peak_gemessen"].startswith("2026-07-18T14:11:00")


def test_latch_faellt_bei_relaxation_nicht_und_haelt_peak_kontext():
    state, attrs = _step(_live_peak_sample(), 99)
    peak_attrs = dict(attrs)

    state, attrs = _step(
        _sample(20), 100, prev_state=state, prev_attrs=attrs,
        now=NOW + dt.timedelta(minutes=11),
    )
    assert float(state) == 257
    for name in (
        "peak_gemessen", "ausreisser_modul", "ausreisser_zelle",
        "ausreisser_zellspannung_mv", "median_mv", "delta_zum_median_mv",
    ):
        assert attrs[name] == peak_attrs[name]


def test_latch_ueberlebt_restaurierten_this_snapshot():
    state, attrs = _step(_live_peak_sample(), 99)

    # Ein neu erzeugter FakeHass bekommt nur den von HA restaurierten
    # Sensorzustand/-attribute-Snapshot. Ein kleineres Folgesample bleibt inert.
    restored_state, restored_attrs = _step(
        _sample(30), 99, prev_state=str(state), prev_attrs=dict(attrs),
        now=NOW + dt.timedelta(minutes=11),
    )
    assert float(restored_state) == 257
    assert restored_attrs["peak_gemessen"] == attrs["peak_gemessen"]
    assert restored_attrs["episode_aktiv"] is True


def test_hysterese_archiviert_einmal_und_naechste_episode_reset():
    state, attrs = _step(_sample(40), 95)
    erster_start = attrs["episode_gestartet"]

    # 94 % beendet die Episode nicht. Beim Wiederanstieg ist es dieselbe
    # Episode; ein hoeherer Peak darf normal uebernommen werden.
    state, attrs = _step(_sample(20), 94, prev_state=state, prev_attrs=attrs,
                         now=NOW + dt.timedelta(minutes=11))
    assert attrs["episode_aktiv"] is True
    state, attrs = _step(_sample(50), 95, prev_state=state, prev_attrs=attrs,
                         now=NOW + dt.timedelta(minutes=22))
    assert float(state) == 50
    assert attrs["episode_gestartet"] == erster_start
    peak_kontext = {
        "ausreisser_modul": attrs["ausreisser_modul"],
        "ausreisser_zelle": attrs["ausreisser_zelle"],
        "delta_zum_median_mv": attrs["delta_zum_median_mv"],
        "peak_gemessen": attrs["peak_gemessen"],
    }

    # Erst < 93 % schliesst und archiviert. Ein weiteres tiefes Sample darf
    # Abschlusszeit und Archiv nicht erneut schreiben.
    close_time = NOW + dt.timedelta(minutes=33)
    state, attrs = _step(_sample(10), 92.9, prev_state=state, prev_attrs=attrs,
                         now=close_time)
    assert float(state) == 50
    assert attrs["episode_aktiv"] is False
    assert float(attrs["letzte_vollladung_peak_mv"]) == 50
    assert attrs["letzte_vollladung_ausreisser_modul"] == peak_kontext["ausreisser_modul"]
    assert attrs["letzte_vollladung_ausreisser_zelle"] == peak_kontext["ausreisser_zelle"]
    assert attrs["letzte_vollladung_delta_zum_median_mv"] == peak_kontext["delta_zum_median_mv"]
    assert attrs["letzte_vollladung_peak_gemessen"] == peak_kontext["peak_gemessen"]
    assert attrs["letzte_vollladung_beendet"].startswith("2026-07-18T14:33:00")
    archiv = {k: v for k, v in attrs.items() if k.startswith("letzte_vollladung_")}

    state, attrs = _step(_sample(5), 90, prev_state=state, prev_attrs=attrs,
                         now=NOW + dt.timedelta(minutes=44))
    assert {k: v for k, v in attrs.items() if k.startswith("letzte_vollladung_")} == archiv

    # Das erste Sample >=95 startet neu und darf niedriger als der alte Peak
    # sein. Das Archiv der abgeschlossenen Episode bleibt vergleichbar stehen.
    state, attrs = _step(_sample(30), 95, prev_state=state, prev_attrs=attrs,
                         now=NOW + dt.timedelta(days=1))
    assert float(state) == 30
    assert attrs["episode_aktiv"] is True
    assert attrs["episode_gestartet"] != erster_start
    assert {k: v for k, v in attrs.items() if k.startswith("letzte_vollladung_")} == archiv
