"""Restenergie ist Kapazitaet mal SoC-Differenz, kein normiertes SoC-Fenster."""
import pytest

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render_native


BASE = {"sensor.opti_soc": "50", "sensor.opti_battery_capacity_kwh": "10",
        "sensor.opti_house_consumption_w": "1000", "sensor.opti_pv_power_w": "0",
        "input_number.minsoc": "10", "input_number.maxsoc": "95"}


@pytest.fixture(params=[False, True], ids=["canonical", "legacy"])
def layer(request):
    return request.param


@pytest.fixture
def runtime(layer):
    def evaluate(overrides, field="state"):
        states = {**BASE, **overrides}
        if layer:
            mapping = {
                "sensor.opti_soc": "sensor.DEINE_BATTERIE_SOC",
                "sensor.opti_battery_capacity_kwh": "sensor.sma_stp_se_40187_batterie_nennkapazitaet",
                "sensor.opti_house_consumption_w": "sensor.house_battery_load_30_mins",
                "sensor.opti_pv_power_w": "sensor.DEIN_PV_POWER",
            }
            cap = states["sensor.opti_battery_capacity_kwh"]
            try:
                states["sensor.opti_battery_capacity_kwh"] = str(float(cap) * 1000)
            except ValueError:
                pass
            states = {mapping.get(k, k): v for k, v in states.items()}
        entity = find_template_entity(load_yaml(REPO / "packages/opti_derived.yaml"),
                                      "sensor", "opti_runtime_h")
        if layer:
            entity = find_template_entity(load_yaml(REPO / "packages/sma_templates.yaml"),
                                          "sensor", "house_battery_runtime_raw")
        return render_native(FakeHass(states=states), entity[field])
    return evaluate


@pytest.mark.parametrize("source", [
    "sensor.opti_soc", "sensor.opti_battery_capacity_kwh",
    "sensor.opti_house_consumption_w", "sensor.opti_pv_power_w", "input_number.minsoc",
])
@pytest.mark.parametrize("bad", ["unknown", "unavailable", "kaputt", "nan", "inf"])
def test_fehlende_oder_ungueltige_quelle_wird_nicht_zur_zahl(runtime, source, bad):
    assert runtime({source: bad}, "availability") is False
    assert runtime({source: bad}) is None


@pytest.mark.parametrize("soc,expected", [(5, 0), (10, 0), (50, 4), (95, 8.5), (100, 9)])
def test_restenergie_bezieht_sich_auf_nennkapazitaet(runtime, soc, expected):
    assert runtime({"sensor.opti_soc": str(soc)}) == expected


def test_ladeobergrenze_veraendert_nicht_vorhandene_energie(runtime):
    assert runtime({"input_number.maxsoc": "80"}) == 4
    assert runtime({"input_number.maxsoc": "unavailable"}) == 4


@pytest.mark.parametrize("overrides,expected", [
    ({"sensor.opti_house_consumption_w": "0"}, 999),
    ({"sensor.opti_house_consumption_w": "10"}, 24),
    ({"sensor.opti_pv_power_w": "100"}, 0),
    ({"sensor.opti_battery_capacity_kwh": "0"}, None),
    ({"sensor.opti_soc": "101"}, None),
])
def test_grenzen_und_bisheriges_anzeigeverhalten(runtime, overrides, expected):
    assert runtime(overrides) == expected


def test_signierte_batterielast_nur_im_legacy_layer(runtime, layer):
    assert runtime({"sensor.opti_house_consumption_w": "-1000"}) == (4 if layer else None)
