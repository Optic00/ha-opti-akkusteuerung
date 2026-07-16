"""Tests fuer die BYD-Monitoring-Templates (byd_bmu.yaml + byd_modul2_fruehwarnung.yaml):
Zellspreizung-Klemme, Ruhe-Median gegen Einzel-Ausreisser, Temp-Spreizung, Absackung."""

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render


def _bmu_entity(kind, unique_id):
    return find_template_entity(load_yaml(REPO / "packages" / "byd_bmu.yaml"), kind, unique_id)


def _fw_entity(kind, unique_id):
    return find_template_entity(
        load_yaml(REPO / "packages" / "byd_modul2_fruehwarnung.yaml"), kind, unique_id)


# ---------------------------------------------------------------------------
# Zellspreizung: max - min in mV, negativ auf 0 geklemmt
# ---------------------------------------------------------------------------

def test_zellspreizung_normal():
    hass = FakeHass(states={"sensor.byd_zellspannung_max": "3.300",
                            "sensor.byd_zellspannung_min": "3.280"})
    entity = _bmu_entity("sensor", "byd_bmu_zellspreizung_mv")
    assert float(render(hass, entity["state"])) == 20.0
    assert render(hass, entity["availability"]) == "True"


def test_zellspreizung_negativ_geklemmt():
    # MQTT-Skew-Artefakt: min-Topic frischer als max -> rechnerisch negativ.
    hass = FakeHass(states={"sensor.byd_zellspannung_max": "3.280",
                            "sensor.byd_zellspannung_min": "3.300"})
    entity = _bmu_entity("sensor", "byd_bmu_zellspreizung_mv")
    assert float(render(hass, entity["state"])) == 0.0


def test_zellspreizung_unavailable_ohne_min():
    hass = FakeHass(states={"sensor.byd_zellspannung_max": "3.300"})
    entity = _bmu_entity("sensor", "byd_bmu_zellspreizung_mv")
    assert render(hass, entity["availability"]) == "False"


# ---------------------------------------------------------------------------
# Ruhe-Spreizung: Median der letzten 5 Gate-Messungen (this.attributes.messreihe)
# ---------------------------------------------------------------------------

def _ruhe_entity():
    return _bmu_entity("sensor", "byd_bmu_zellspreizung_ruhe_mv")


def test_ruhe_median_kappt_einzel_ausreisser():
    # Historie unauffaellig (2-3 mV), ein Skew-Artefakt von 40 mV kommt rein:
    # Median bleibt bei 3 statt den Ausreisser zu latchen.
    hass = FakeHass(states={"sensor.byd_zellspreizung": "40"},
                    this_attributes={"messreihe": [2.0, 3.0, 2.0, 3.0]})
    assert float(render(hass, _ruhe_entity()["state"])) == 3.0


def test_ruhe_median_laesst_echte_hohe_spreizung_durch():
    # Persistent hohe Werte in der Historie: der Median folgt dem echten Niveau.
    hass = FakeHass(states={"sensor.byd_zellspreizung": "61"},
                    this_attributes={"messreihe": [30.0, 60.0, 55.0, 58.0]})
    assert float(render(hass, _ruhe_entity()["state"])) == 58.0


def test_ruhe_median_erste_messung_ohne_historie():
    hass = FakeHass(states={"sensor.byd_zellspreizung": "4"}, this_attributes={})
    assert float(render(hass, _ruhe_entity()["state"])) == 4.0


def test_ruhe_messreihe_rolliert_auf_fuenf():
    hass = FakeHass(states={"sensor.byd_zellspreizung": "6"},
                    this_attributes={"messreihe": [1.0, 2.0, 3.0, 4.0, 5.0]})
    reihe = render(hass, _ruhe_entity()["attributes"]["messreihe"])
    assert reihe == "[2.0, 3.0, 4.0, 5.0, 6.0]"


# ---------------------------------------------------------------------------
# Temperatur-Spreizung: Klemme + Availability auf min UND max
# ---------------------------------------------------------------------------

def _temp_states(tmax, tmin):
    states = {}
    for i in range(1, 6):
        states[f"sensor.byd_modul_{i}_temp_max"] = str(tmax)
        states[f"sensor.byd_modul_{i}_temp_min"] = str(tmin)
    return states


def test_temp_spreizung_normal():
    hass = FakeHass(states=_temp_states(33, 27))
    entity = _bmu_entity("sensor", "byd_bmu_temp_spreizung_k")
    assert float(render(hass, entity["state"])) == 6.0
    assert render(hass, entity["availability"]) == "True"


def test_temp_spreizung_negativ_geklemmt():
    # Teil-Ausfall-Artefakt: min-Werte ueber max-Werten -> 0 statt negativ.
    hass = FakeHass(states=_temp_states(20, 25))
    entity = _bmu_entity("sensor", "byd_bmu_temp_spreizung_k")
    assert float(render(hass, entity["state"])) == 0.0


def test_temp_spreizung_unavailable_ohne_min_sensoren():
    states = _temp_states(33, 27)
    del states["sensor.byd_modul_1_temp_min"]
    hass = FakeHass(states=states)
    entity = _bmu_entity("sensor", "byd_bmu_temp_spreizung_k")
    assert render(hass, entity["availability"]) == "False"


# ---------------------------------------------------------------------------
# Fruehwarnung: Absackung (Peer-Median) + Entladeband
# ---------------------------------------------------------------------------

def _modul_min_states():
    return {"sensor.byd_modul_1_zellspannung_min": "3.212",
            "sensor.byd_modul_2_zellspannung_min": "3.189",
            "sensor.byd_modul_3_zellspannung_min": "3.212",
            "sensor.byd_modul_4_zellspannung_min": "3.211",
            "sensor.byd_modul_5_zellspannung_min": "3.209"}


def test_absackung_gegen_peer_median():
    # Peers sortiert [3.209, 3.211, 3.212, 3.212] -> Median 3.2115;
    # (3.2115 - 3.189) * 1000 = 22.5 mV.
    hass = FakeHass(states=_modul_min_states())
    entity = _fw_entity("sensor", "byd_modul2_absackung")
    assert float(render(hass, entity["state"])) == 22.5
    assert render(hass, entity["availability"]) == "True"


def test_absackung_schwaechstes_modul_attribut():
    hass = FakeHass(states=_modul_min_states())
    entity = _fw_entity("sensor", "byd_modul2_absackung")
    assert render(hass, entity["attributes"]["schwaechstes_modul"]) == "2"


def test_entladeband_grenzen():
    entity = _fw_entity("binary_sensor", "byd_entladeband")
    assert render(FakeHass(states={"sensor.byd_leistung": "500"}), entity["state"]) == "True"
    assert render(FakeHass(states={"sensor.byd_leistung": "1500"}), entity["state"]) == "True"
    assert render(FakeHass(states={"sensor.byd_leistung": "499"}), entity["state"]) == "False"
    assert render(FakeHass(states={"sensor.byd_leistung": "-800"}), entity["state"]) == "False"
    assert render(FakeHass(states={}), entity["state"]) == "False"
