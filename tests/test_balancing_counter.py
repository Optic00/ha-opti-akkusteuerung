"""Restart- und Glitch-Regression fuer den Balancing-Counter-Reset.

Die kleine Ablauf-Harness wertet nur die in dieser Automation verwendeten
HA-Bausteine aus (choose, Conditions und counter.increment/reset). Dadurch
laufen die Szenarien gegen die echte YAML-Struktur, ohne HAs kompletten
Trigger-Scheduler nachzubauen.
"""
from __future__ import annotations

from .condition_eval import evaluate_condition
from .ha_harness import REPO, FakeHass, load_yaml

AUTOMATIONS = REPO / "automations" / "opti_balancing_counter.yaml"
HELPERS = REPO / "packages" / "sma_helpers.yaml"
RESET_ID = "opti_balancing_counter_reset"
BESTAETIGUNGEN = "counter.opti_balancing_done_minuten"
TAGE = "counter.tage_seit_akku100"


def _reset_automation():
    return next(a for a in load_yaml(AUTOMATIONS) if a["id"] == RESET_ID)


def _bestaetigungs_helper():
    return load_yaml(HELPERS).get("counter", {}).get("opti_balancing_done_minuten")


def _require_restartfestes_design():
    helper = _bestaetigungs_helper()
    assert helper is not None, "persistenter Bestaetigungs-Counter fehlt"
    return helper, _reset_automation()


def _entity_ids(action):
    entity_ids = action.get("target", {}).get("entity_id", [])
    return [entity_ids] if isinstance(entity_ids, str) else entity_ids


class BalancingAutomationHarness:
    """Fuehrt die deklarativen Aktionen der Reset-Automation minimal aus."""

    def __init__(self, *, soc="97", schwelle="98.5", tage=14,
                 bestaetigungen=0):
        helper, self.automation = _require_restartfestes_design()
        self.maximum = int(helper["maximum"])
        self.states = {
            "sensor.opti_soc": str(soc),
            "input_number.opti_balancing_done_soc": str(schwelle),
            TAGE: str(tage),
            BESTAETIGUNGEN: str(bestaetigungen),
        }

    @property
    def tage(self):
        return int(self.states[TAGE])

    @property
    def bestaetigungen(self):
        return int(self.states[BESTAETIGUNGEN])

    def _condition(self, condition, trigger_id):
        if condition["condition"] == "trigger":
            ids = condition["id"]
            if isinstance(ids, str):
                ids = [ids]
            return trigger_id in ids
        return evaluate_condition(FakeHass(states=self.states), condition)

    def _conditions(self, conditions, trigger_id):
        return all(self._condition(c, trigger_id) for c in conditions)

    def _run_actions(self, actions, trigger_id):
        for action in actions:
            if "choose" in action:
                for option in action["choose"]:
                    if self._conditions(option.get("conditions", []), trigger_id):
                        self._run_actions(option["sequence"], trigger_id)
                        break
                else:
                    self._run_actions(action.get("default", []), trigger_id)
                continue

            service = action.get("action")
            if service == "counter.reset":
                for entity_id in _entity_ids(action):
                    self.states[entity_id] = "0"
            elif service == "counter.increment":
                for entity_id in _entity_ids(action):
                    current = int(self.states.get(entity_id, "0"))
                    self.states[entity_id] = str(min(current + 1, self.maximum))
            else:
                raise NotImplementedError(f"Aktion nicht unterstuetzt: {action!r}")

    def fire(self, trigger_id):
        self._run_actions(self.automation["actions"], trigger_id)

    def minute(self):
        self.fire("minute")

    def set_soc(self, value):
        value = str(value)
        if value != self.states["sensor.opti_soc"]:
            self.states["sensor.opti_soc"] = value
            if value in ("unknown", "unavailable"):
                self.fire("sensorfehler")
            elif float(value) <= float(
                    self.states["input_number.opti_balancing_done_soc"]):
                self.fire("rueckfall")


def test_bestaetigungs_counter_wird_ohne_initial_restauriert():
    helper, _automation = _require_restartfestes_design()
    assert "initial" not in helper
    assert helper["minimum"] == 0
    assert helper["maximum"] == 30
    assert helper["step"] == 1


def test_reset_automation_hat_restartfeste_trigger_ohne_for_timer():
    _helper, automation = _require_restartfestes_design()
    triggers = automation["triggers"]

    assert not any("for" in trigger for trigger in triggers)
    assert any(trigger.get("trigger") == "time_pattern"
               and trigger.get("minutes") == "/1"
               and trigger.get("id") == "minute" for trigger in triggers)
    assert any(trigger.get("trigger") == "homeassistant"
               and trigger.get("event") == "start"
               and trigger.get("id") == "ha_start" for trigger in triggers)
    assert any(trigger.get("trigger") == "template"
               and trigger.get("id") == "rueckfall" for trigger in triggers)


def test_rueckfall_und_sensorfehler_werden_ereignissicher_eingereiht():
    _helper, automation = _require_restartfestes_design()
    assert automation["mode"] == "queued"

    triggers = automation["triggers"]
    assert any(trigger.get("trigger") == "state"
               and trigger.get("entity_id") == "sensor.opti_soc"
               and trigger.get("to") == ["unknown", "unavailable"]
               and trigger.get("id") == "sensorfehler" for trigger in triggers)

    # Der Reset darf bei spaeterer Ausfuehrung nicht nochmals den dann
    # aktuellen SoC pruefen: das bereits eingereihte Rueckfall-/Fehler-Ereignis
    # ist die Wahrheit, auch wenn der Sensor inzwischen wieder hoch steht.
    reset_option = automation["actions"][0]["choose"][0]
    assert reset_option["conditions"] == [{
        "condition": "trigger",
        "id": ["rueckfall", "sensorfehler"],
    }]


def test_restart_mit_hohem_soc_setzt_nach_restlichen_minuten_zurueck():
    lauf = BalancingAutomationHarness(soc="99", tage=14)
    for _ in range(21):
        lauf.minute()
    assert lauf.bestaetigungen == 21
    assert lauf.tage == 14

    # HA-Restart: der alte for:-Timer waere weg. Der neue Helper restauriert
    # dagegen 21 Bestaetigungen. Wie bei Template-Sensoren ueblich, ist der SoC
    # am start-Event noch unavailable und kommt danach direkt oberhalb zurueck.
    nach_restart = BalancingAutomationHarness(
        soc="unavailable", tage=lauf.tage, bestaetigungen=lauf.bestaetigungen)
    nach_restart.fire("ha_start")
    assert nach_restart.bestaetigungen == 21
    nach_restart.set_soc("99")
    for _ in range(8):
        nach_restart.minute()
    assert nach_restart.bestaetigungen == 29
    assert nach_restart.tage == 14

    nach_restart.minute()
    assert nach_restart.bestaetigungen == 0
    assert nach_restart.tage == 0


def test_kurzer_soc_spike_wird_sofort_verworfen_und_resettet_nicht():
    lauf = BalancingAutomationHarness(soc="97", tage=14)
    lauf.set_soc("99")
    for _ in range(29):
        lauf.minute()
    assert lauf.bestaetigungen == 29

    lauf.set_soc("97")
    assert lauf.bestaetigungen == 0
    assert lauf.tage == 14

    # Ein neuer Anlauf beginnt wieder bei null und darf nicht die 29 Minuten
    # des verworfenen Spikes weiterverwenden.
    lauf.set_soc("99")
    lauf.minute()
    assert lauf.bestaetigungen == 1
    assert lauf.tage == 14


def test_sensorfehler_verwirft_laufende_bestaetigungen_sofort():
    lauf = BalancingAutomationHarness(soc="99", tage=14)
    for _ in range(10):
        lauf.minute()
    assert lauf.bestaetigungen == 10

    lauf.set_soc("unavailable")
    assert lauf.bestaetigungen == 0
    assert lauf.tage == 14
