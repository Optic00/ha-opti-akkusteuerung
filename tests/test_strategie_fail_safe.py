"""Struktur-Regressionen fuer Cleanup und Core-Daten-Fail-safe.

Die Tests pruefen die echte HA-YAML. Sie modellieren nicht den Adapter, sondern
pinnen die sicherheitskritischen Vertraege:

* die First-Match-choose-Kette darf den nachfolgenden Cleanup nicht abbrechen;
* der Cleanup darf den bereits gewaehlten Modus nicht ueberschreiben;
* Core-Datenverlust und ausgeschaltete Automatik fuehren nur zu Akku Pause;
* Kapazitaets-Recovery triggert die Hauptstrategie erneut.
"""
from __future__ import annotations

from .ha_harness import REPO, FakeHass, load_yaml, render

STRATEGIE = REPO / "automations" / "opti_strategie.yaml"
MAIN_ID = "opti_canonical_strategie"
FAIL_SAFE_ID = "opti_canonical_strategie_fail_safe"
MAIN_ACTION_ALIAS = "Zwischen Speicherszenarien wählen"
CLEANUP_ALIAS = "Netzladen: Booster deaktivieren wenn Akku voll"
MODUS = "input_select.akkusteuerung_modus"


def _automations():
    return load_yaml(STRATEGIE)


def _automation(automation_id):
    return next(a for a in _automations() if a["id"] == automation_id)


def _action(alias):
    return next(a for a in _automation(MAIN_ID)["actions"]
                if a.get("alias") == alias)


def _walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_dicts(value)


def _target_entities(action):
    entity_ids = action.get("target", {}).get("entity_id", [])
    return [entity_ids] if isinstance(entity_ids, str) else entity_ids


def _selected_options(node, entity_id=MODUS):
    return {
        item.get("data", {}).get("option")
        for item in _walk_dicts(node)
        if item.get("action") == "input_select.select_option"
        and entity_id in _target_entities(item)
    }


def _main_choose_options():
    return _action(MAIN_ACTION_ALIAS)["choose"]


def test_kein_strategiezweig_stoppt_vor_cleanup():
    stops = [
        item["stop"]
        for option in _main_choose_options()
        for item in _walk_dicts(option.get("sequence", []))
        if "stop" in item
    ]
    assert stops == [], f"Zweig-stop verhindert Cleanup: {stops}"


def test_cleanup_ueberschreibt_den_gewaehlten_modus_nicht():
    assert _selected_options(_action(CLEANUP_ALIAS)) == set()


def test_strategie_triggert_auf_kapazitaets_rueckkehr():
    state_entities = {
        entity_id
        for trigger in _automation(MAIN_ID)["triggers"]
        if trigger.get("trigger") == "state"
        for entity_id in (
            trigger.get("entity_id")
            if isinstance(trigger.get("entity_id"), list)
            else [trigger.get("entity_id")]
        )
    }
    assert "sensor.opti_battery_capacity_kwh" in state_entities


def test_hauptstrategie_hat_native_core_daten_gates():
    conditions = _automation(MAIN_ID)["conditions"]
    numeric = {
        c.get("entity_id"): c
        for c in conditions
        if c.get("condition") == "numeric_state"
    }
    assert numeric["sensor.opti_soc"]["above"] < 0
    assert numeric["sensor.opti_soc"]["below"] > 100
    assert numeric["sensor.opti_battery_capacity_kwh"]["above"] == 0


def test_fail_safe_nutzt_pause_und_nie_automatisch():
    fail_safe = _automation(FAIL_SAFE_ID)
    assert _selected_options(fail_safe) == {"Akku Pause"}

    all_strings = [
        value
        for item in _walk_dicts(fail_safe)
        for value in item.values()
        if isinstance(value, str)
    ]
    assert "Akku Automatisch" not in all_strings


def test_fail_safe_hat_sofortpfade_fuer_master_aus_und_ha_start():
    triggers = _automation(FAIL_SAFE_ID)["triggers"]
    assert any(
        t.get("trigger") == "state"
        and t.get("entity_id") == "input_boolean.akku_opti_automatik"
        and t.get("to") == "off"
        and t.get("id") == "master_aus"
        for t in triggers
    )
    assert any(
        t.get("trigger") == "homeassistant"
        and t.get("event") == "start"
        and t.get("id") == "ha_start"
        for t in triggers
    )


def test_fail_safe_prueft_core_daten_auch_nach_automation_reload_sofort():
    fail_safe = _automation(FAIL_SAFE_ID)
    assert any(
        trigger.get("trigger") == "event"
        and trigger.get("event_type") == "automation_reloaded"
        and trigger.get("id") == "automationen_neu_geladen"
        for trigger in fail_safe["triggers"]
    )
    start_branch = next(
        option for option in fail_safe["actions"][0]["choose"]
        if option["conditions"][0].get("condition") == "trigger"
        and set(
            option["conditions"][0]["id"]
            if isinstance(option["conditions"][0]["id"], list)
            else [option["conditions"][0]["id"]]
        ) == {"ha_start", "automationen_neu_geladen"}
    )
    core_condition = next(
        condition for condition in _walk_dicts(start_branch["conditions"])
        if condition.get("condition") == "template"
    )
    assert core_condition["value_template"] == _core_invalid_trigger()["value_template"]


def _core_invalid_trigger():
    return next(
        trigger
        for trigger in _automation(FAIL_SAFE_ID)["triggers"]
        if trigger.get("id") == "core_ungueltig"
    )


def test_fail_safe_entprellt_den_kombinierten_core_fehler_zehn_sekunden():
    fail_safe = _automation(FAIL_SAFE_ID)
    core_trigger = _core_invalid_trigger()
    assert core_trigger["trigger"] == "template"
    assert core_trigger["for"] == "00:00:10"
    assert not any(
        trigger.get("trigger") == "state"
        and trigger.get("entity_id") in {
            "sensor.opti_soc",
            "sensor.opti_battery_capacity_kwh",
        }
        for trigger in fail_safe["triggers"]
    )

    core_branch = next(
        option for option in fail_safe["actions"][0]["choose"]
        if option["conditions"] == [
            {"condition": "trigger", "id": "core_ungueltig"}
        ]
    )
    assert not any("delay" in action for action in core_branch["sequence"])
    assert _selected_options(core_branch) == {"Akku Pause"}


def test_fail_safe_core_template_akzeptiert_nur_plausible_zahlen():
    template = _core_invalid_trigger()["value_template"]

    def invalid(soc, capacity):
        return render(
            FakeHass(states={
                "sensor.opti_soc": soc,
                "sensor.opti_battery_capacity_kwh": capacity,
            }),
            template,
        ) == "True"

    assert invalid("50", "10") is False
    assert invalid("unknown", "10") is True
    assert invalid("unavailable", "10") is True
    assert invalid("kein_messwert", "10") is True
    assert invalid("-0.1", "10") is True
    assert invalid("100.1", "10") is True
    assert invalid("50", "unavailable") is True
    assert invalid("50", "kein_messwert") is True
    assert invalid("50", "0") is True


def test_fail_safe_ha_start_nutzt_denselben_robusten_core_vertrag():
    fail_safe = _automation(FAIL_SAFE_ID)
    ha_start = next(
        option for option in fail_safe["actions"][0]["choose"]
        if option["conditions"][0].get("condition") == "trigger"
        and "ha_start" in (
            option["conditions"][0]["id"]
            if isinstance(option["conditions"][0]["id"], list)
            else [option["conditions"][0]["id"]]
        )
    )
    core_condition = next(
        condition for condition in _walk_dicts(ha_start["conditions"])
        if condition.get("condition") == "template"
    )
    assert core_condition["value_template"] == _core_invalid_trigger()["value_template"]
