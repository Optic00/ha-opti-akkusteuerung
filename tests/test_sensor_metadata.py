"""HA-Kompatibilitaet und Trend-Semantik der BYD-Diagnosewerte (#64)."""
import pytest

from .ha_harness import REPO, find_template_entity, load_yaml


@pytest.mark.parametrize("path", sorted((REPO / "packages").glob("*.yaml")))
def test_energiezaehler_haben_keine_measurement_klasse(path):
    for block in load_yaml(path).get("template", []):
        for entity in block.get("sensor", []):
            if entity.get("device_class") == "energy":
                assert entity.get("state_class") in (None, "total", "total_increasing"), (
                    path.name, entity.get("unique_id"))


@pytest.mark.parametrize("uid", [
    "byd_netto_energie_seit_voll_nativ", "byd_modul2_netto_bis_knie_nativ",
])
def test_byd_zykluswerte_bleiben_vergleichbare_absolutwerte(uid):
    entity = find_template_entity(load_yaml(REPO / "packages/byd_modul2_fruehwarnung.yaml"),
                                  "sensor", uid)
    # Eine fallende Knie-Energie muss als kleinerer Messwert erhalten bleiben;
    # total/total_increasing wuerden Differenzen/Zaehlzyklen summieren.
    assert entity.get("state_class") == "measurement"
    assert entity["unit_of_measurement"] == "kWh"
    assert entity.get("device_class") is None
