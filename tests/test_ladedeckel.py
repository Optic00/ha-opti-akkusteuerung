"""Issue #68: Das Halteband braucht einen belegten Eintritt am eigenen Deckel."""
import pytest

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render_native
from .condition_eval import evaluate_condition
from .test_strategie_paritaet import _make_hass, _evaluate_automation, _vorschau


def _step(soc, maximum, previous=None, previous_limit=None):
    cfg = load_yaml(REPO / "packages/opti_derived.yaml")
    entity = find_template_entity(cfg, "binary_sensor", "opti_ladedeckel_aktiv")
    block = next(b for b in cfg["template"] if entity in b.get("binary_sensor", []))
    hass = FakeHass(
        states={"sensor.opti_soc": str(soc), "input_number.maxsoc": str(maximum)},
        this_state=previous, this_attributes={"maxsoc": previous_limit},
    )
    if not all(evaluate_condition(hass, c) for c in block.get("conditions", [])):
        return previous, previous_limit
    return ("on" if render_native(hass, entity["state"]) else "off",
            render_native(hass, entity["attributes"]["maxsoc"]))


@pytest.mark.parametrize("maximum,soc", [(95, 93), (100, 98)])
def test_fremder_entlademodus_verriegelt_nicht(maximum, soc):
    state = {"sensor.opti_soc": str(soc), "input_number.maxsoc": str(maximum),
             "sensor.opti_target_soc": "50"}
    assert _evaluate_automation(_make_hass(state)) == (21, "Akku nur Entladen")
    state.update({"input_select.akkusteuerung_modus": "Akku nur Entladen",
                  "sensor.opti_target_soc": str(maximum)})
    hass = _make_hass(state)
    assert _evaluate_automation(hass)[1] == "Akku Dynamisch"
    assert _vorschau(hass, "state") == "Akku Dynamisch"
    assert "Ladedeckel" not in _vorschau(hass, "grund")


def test_echter_eintritt_haelt_bis_unter_untergrenze():
    memory = (None, None)
    for soc, expected in [(93, "off"), (95, "on"), (94, "on"),
                          (92, "on"), (91.9, "off"), (93, "off")]:
        memory = _step(soc, 95, *memory)
        assert memory == (expected, 95)


def test_neustart_und_sensorluecke_bewahren_belegten_eintritt():
    assert _step(93, 95, "on", 95) == ("on", 95)
    assert _step("unavailable", 95, "on", 95) == ("on", 95)
    assert _step(91, 95, "on", 95) == ("off", 95)


def test_geaenderter_deckel_erbt_keinen_alten_eintritt():
    assert _step(98, 100, "on", 95) == ("off", 100)
    assert _step(94, 93, "off", 95) == ("on", 93)


@pytest.mark.parametrize("soc,limit,latched,old_limit,expected", [
    (95, 95, "unknown", None, True),  # Obergrenze wirkt vor dem Sensor-Update.
    (93, 95, "on", 95, True),
    (92, 95, "on", 95, True),
    (91.9, 95, "on", 95, False),    # Staler Merker darf nicht halten.
    (98, 100, "on", 95, False),    # MaxSOC-Update vor Merker-Update.
    (98, "unavailable", "on", 95, False),
])
def test_strategie_und_vorschau_beachten_gueltigen_merker(
        soc, limit, latched, old_limit, expected):
    hass = _make_hass({
        "sensor.opti_soc": str(soc), "input_number.maxsoc": str(limit),
        "sensor.opti_target_soc": "100",
        "binary_sensor.opti_ladedeckel_aktiv": latched,
        "_attrs": {"binary_sensor.opti_ladedeckel_aktiv": {"maxsoc": old_limit}},
    })
    branch, mode = _evaluate_automation(hass)
    assert (branch == 7) is expected
    assert ("Ladedeckel" in _vorschau(hass, "grund")) is expected
    assert _vorschau(hass, "state") == mode


def test_merker_hat_neustart_und_recovery_trigger():
    cfg = load_yaml(REPO / "packages/opti_derived.yaml")
    entity = find_template_entity(cfg, "binary_sensor", "opti_ladedeckel_aktiv")
    block = next(b for b in cfg["template"] if entity in b.get("binary_sensor", []))
    assert any(t.get("event") == "start" for t in block["triggers"])
    refs = {eid for t in block["triggers"] if t.get("trigger") == "state"
            for eid in t["entity_id"]}
    assert refs == {"sensor.opti_soc", "input_number.maxsoc"}
    cfg = load_yaml(REPO / "automations/opti_strategie.yaml")
    assert any("binary_sensor.opti_ladedeckel_aktiv" in t.get("entity_id", [])
               for t in cfg[0]["triggers"])
