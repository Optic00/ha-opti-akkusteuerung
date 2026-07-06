import pytest

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

SOURCE = "sensor.solcast_pv_forecast_prognose_verbleibende_leistung_heute"

MAPPING_PATH = REPO / "packages" / "opti_mapping.yaml"

# packages/opti_mapping.yaml ist bewusst gitignored (private Entitäts-IDs).
# In Checkouts ohne die private Datei werden diese Tests übersprungen.
pytestmark = pytest.mark.skipif(
    not MAPPING_PATH.exists(),
    reason="privates packages/opti_mapping.yaml nicht vorhanden (gitignored)",
)


def _mapping_cfg():
    return load_yaml(MAPPING_PATH)


def _remaining_today_estimate10(hass):
    cfg = _mapping_cfg()
    entity = find_template_entity(cfg, "sensor", "opti_mapping_forecast_remaining_today_kwh")
    return render(hass, entity["attributes"]["estimate10"])


def test_mapping_remaining_today_reicht_estimate10_durch():
    hass = FakeHass(
        states={SOURCE: "22.26"},
        attrs={SOURCE: {"estimate10": 9.46}},
    )
    assert float(_remaining_today_estimate10(hass)) == 9.46


def test_mapping_remaining_today_estimate10_fehlt_bleibt_none():
    # Kontrakt (canonical-layer.md): fehlt das P10 an der Quelle, bleibt das
    # Attribut none - NICHT 0, denn 0 waere von "echtes P10 = 0 kWh" nicht
    # unterscheidbar. Seit 2026-07-05 ist die private opti_mapping.yaml auf
    # die none-Variante des Examples angeglichen.
    hass = FakeHass(states={SOURCE: "22.26"})
    assert _remaining_today_estimate10(hass) == "None"
