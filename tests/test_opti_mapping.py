import re

import pytest

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

SOURCE = "sensor.solcast_pv_forecast_prognose_verbleibende_leistung_heute"

MAPPING_PATH = REPO / "packages" / "opti_mapping.yaml"


def test_getrackter_testcode_enthaelt_keine_wr_seriennummern():
    source = (REPO / "tests" / "test_opti_mapping.py").read_text()
    assert re.findall(r"sensor\.sn_\d{6,}", source) == []


# packages/opti_mapping.yaml ist bewusst gitignored (private Entitäts-IDs).
# Nur Tests, die diese Datei wirklich lesen, werden in öffentlichen Checkouts
# übersprungen. Der Privacy-Guard oben muss dagegen immer laufen.
requires_private_mapping = pytest.mark.skipif(
    not MAPPING_PATH.exists(),
    reason="privates packages/opti_mapping.yaml nicht vorhanden (gitignored)",
)


def _mapping_cfg():
    return load_yaml(MAPPING_PATH)


def _remaining_today_estimate10(hass):
    cfg = _mapping_cfg()
    entity = find_template_entity(cfg, "sensor", "opti_mapping_forecast_remaining_today_kwh")
    return render(hass, entity["attributes"]["estimate10"])


def _pv_yield_entity():
    return find_template_entity(
        _mapping_cfg(), "sensor", "opti_mapping_pv_yield_today"
    )


def _pv_wr_entities():
    entity = _pv_yield_entity()
    templates = f"{entity['availability']} {entity['state']}"
    entity_ids = sorted(set(
        re.findall(r"sensor\.sn_\d+_daily_yield", templates)
    ))
    assert len(entity_ids) == 2
    return entity_ids


@requires_private_mapping
def test_mapping_remaining_today_reicht_estimate10_durch():
    hass = FakeHass(
        states={SOURCE: "22.26"},
        attrs={SOURCE: {"estimate10": 9.46}},
    )
    assert float(_remaining_today_estimate10(hass)) == 9.46


@requires_private_mapping
def test_mapping_remaining_today_estimate10_fehlt_bleibt_none():
    # Kontrakt (canonical-layer.md): fehlt das P10 an der Quelle, bleibt das
    # Attribut none - NICHT 0, denn 0 waere von "echtes P10 = 0 kWh" nicht
    # unterscheidbar. Seit 2026-07-05 ist die private opti_mapping.yaml auf
    # die none-Variante des Examples angeglichen.
    hass = FakeHass(states={SOURCE: "22.26"})
    assert _remaining_today_estimate10(hass) == "None"


@requires_private_mapping
def test_mapping_pv_ertrag_summiert_beide_wechselrichter():
    entity = _pv_yield_entity()
    pv_wr_1, pv_wr_2 = _pv_wr_entities()
    hass = FakeHass(states={pv_wr_1: "33679", pv_wr_2: "32479"})
    assert float(render(hass, entity["state"])) == 66.158
    assert render(hass, entity["availability"]) == "True"


@requires_private_mapping
def test_mapping_pv_ertrag_braucht_beide_wechselrichter():
    entity = _pv_yield_entity()
    pv_wr_1, pv_wr_2 = _pv_wr_entities()
    hass = FakeHass(states={pv_wr_1: "33679", pv_wr_2: "unavailable"})
    assert render(hass, entity["availability"]) == "False"


@requires_private_mapping
def test_taeglicher_pv_aggregat_hat_keine_total_increasing_statistik():
    entity = _pv_yield_entity()
    assert "state_class" not in entity
