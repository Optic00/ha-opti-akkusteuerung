"""Restart-, Glitch- und Tages-Latch-Regression fuer das Balancing."""
from __future__ import annotations

import datetime as dt

from .condition_eval import evaluate_condition
from .ha_harness import REPO, TZ, FakeHass, load_yaml

AUTOMATIONS = REPO / "automations" / "opti_balancing_counter.yaml"
HELPERS = REPO / "packages" / "sma_helpers.yaml"
RESET_ID = "opti_balancing_counter_reset"
INCREMENT_ID = "opti_balancing_counter_increment"
BESTAETIGUNGEN = "counter.opti_balancing_done_minuten"
TAGE = "counter.tage_seit_akku100"
LETZTER_ABSCHLUSS = "input_datetime.opti_balancing_letzter_abschluss"
ABSCHLUSS_GUELTIG = "input_boolean.opti_balancing_abschluss_gueltig"
NOW = dt.datetime(2026, 7, 18, 12, 0, tzinfo=TZ)


def _automation(automation_id):
    return next(a for a in load_yaml(AUTOMATIONS) if a["id"] == automation_id)


def _helper(domain, entity):
    return load_yaml(HELPERS).get(domain, {}).get(entity)


def _require_restartfestes_design():
    helper = _helper("counter", "opti_balancing_done_minuten")
    assert helper is not None, "persistenter Bestaetigungs-Counter fehlt"
    timestamp = _helper("input_datetime", "opti_balancing_letzter_abschluss")
    assert timestamp is not None, "persistenter Tagesabschluss-Zeitstempel fehlt"
    return helper, timestamp, _automation(RESET_ID)


def _entity_ids(action):
    entity_ids = action.get("target", {}).get("entity_id", [])
    return [entity_ids] if isinstance(entity_ids, str) else entity_ids


class BalancingAutomationHarness:
    """Fuehrt die in den beiden Automationen verwendeten Aktionen minimal aus."""

    def __init__(
        self,
        *,
        soc="97",
        schwelle="98.5",
        tage=14,
        bestaetigungen=0,
        letzter_abschluss="unknown",
        abschluss_gueltig="off",
        now=NOW,
    ):
        helper, _timestamp, self.reset_automation = _require_restartfestes_design()
        self.increment_automation = _automation(INCREMENT_ID)
        self.maximum = int(helper["maximum"])
        self.now = now
        self.states = {
            "sensor.opti_soc": str(soc),
            "input_number.opti_balancing_done_soc": str(schwelle),
            TAGE: str(tage),
            BESTAETIGUNGEN: str(bestaetigungen),
            LETZTER_ABSCHLUSS: letzter_abschluss,
            ABSCHLUSS_GUELTIG: abschluss_gueltig,
        }

    @property
    def tage(self):
        return int(self.states[TAGE])

    @property
    def bestaetigungen(self):
        return int(self.states[BESTAETIGUNGEN])

    @property
    def letzter_abschluss(self):
        return self.states[LETZTER_ABSCHLUSS]

    @property
    def abschluss_gueltig(self):
        return self.states[ABSCHLUSS_GUELTIG]

    def _condition(self, condition, trigger_id):
        if condition["condition"] == "trigger":
            ids = condition["id"]
            if isinstance(ids, str):
                ids = [ids]
            return trigger_id in ids
        return evaluate_condition(
            FakeHass(states=self.states, now=self.now), condition
        )

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
                    maximum = self.maximum if entity_id == BESTAETIGUNGEN else current + 1
                    self.states[entity_id] = str(min(current + 1, maximum))
            elif service == "input_datetime.set_datetime":
                for entity_id in _entity_ids(action):
                    self.states[entity_id] = self.now.isoformat()
            elif service == "input_boolean.turn_on":
                for entity_id in _entity_ids(action):
                    self.states[entity_id] = "on"
            else:
                raise NotImplementedError(f"Aktion nicht unterstuetzt: {action!r}")

    def fire(self, trigger_id):
        self._run_actions(self.reset_automation["actions"], trigger_id)

    def minute(self):
        self.fire("minute")

    def day_end(self):
        if self._conditions(self.increment_automation.get("conditions", []), "day_end"):
            self._run_actions(self.increment_automation["actions"], "day_end")

    def set_soc(self, value):
        value = str(value)
        if value != self.states["sensor.opti_soc"]:
            self.states["sensor.opti_soc"] = value
            if value in ("unknown", "unavailable"):
                self.fire("sensorfehler")
            elif float(value) <= float(
                self.states["input_number.opti_balancing_done_soc"]
            ):
                self.fire("rueckfall")


def test_persistente_helper_werden_ohne_initial_restauriert():
    helper, timestamp, _automation_cfg = _require_restartfestes_design()
    tage_helper = _helper("counter", "tage_seit_akku100")
    assert tage_helper is not None
    assert "initial" not in tage_helper
    assert "initial" not in helper
    assert helper["minimum"] == 0
    assert helper["maximum"] == 30
    assert helper["step"] == 1
    assert "initial" not in timestamp
    assert timestamp["has_date"] is True
    assert timestamp["has_time"] is True


def test_abschluss_gueltigkeitsflag_wird_ohne_initial_restauriert():
    gueltig = _helper("input_boolean", "opti_balancing_abschluss_gueltig")
    assert gueltig is not None
    assert "initial" not in gueltig


def test_reset_automation_hat_restartfeste_trigger_ohne_for_timer():
    _helper_cfg, _timestamp, automation = _require_restartfestes_design()
    triggers = automation["triggers"]

    assert not any("for" in trigger for trigger in triggers)
    assert any(
        trigger.get("trigger") == "time_pattern"
        and trigger.get("minutes") == "/1"
        and trigger.get("id") == "minute"
        for trigger in triggers
    )
    assert any(
        trigger.get("trigger") == "homeassistant"
        and trigger.get("event") == "start"
        and trigger.get("id") == "ha_start"
        for trigger in triggers
    )
    assert any(
        trigger.get("trigger") == "template"
        and trigger.get("id") == "rueckfall"
        for trigger in triggers
    )


def test_rueckfall_und_sensorfehler_werden_ereignissicher_eingereiht():
    _helper_cfg, _timestamp, automation = _require_restartfestes_design()
    assert automation["mode"] == "queued"

    triggers = automation["triggers"]
    assert any(
        trigger.get("trigger") == "state"
        and trigger.get("entity_id") == "sensor.opti_soc"
        and trigger.get("to") == ["unknown", "unavailable"]
        and trigger.get("id") == "sensorfehler"
        for trigger in triggers
    )
    reset_option = automation["actions"][0]["choose"][0]
    assert reset_option["conditions"] == [
        {"condition": "trigger", "id": ["rueckfall", "sensorfehler"]}
    ]


def test_restart_mit_hohem_soc_setzt_nach_restlichen_minuten_zurueck():
    lauf = BalancingAutomationHarness(soc="99", tage=14)
    for _ in range(21):
        lauf.minute()
    assert lauf.bestaetigungen == 21

    nach_restart = BalancingAutomationHarness(
        soc="unavailable",
        tage=lauf.tage,
        bestaetigungen=lauf.bestaetigungen,
    )
    nach_restart.fire("ha_start")
    assert nach_restart.bestaetigungen == 21
    nach_restart.set_soc("99")
    for _ in range(9):
        nach_restart.minute()

    assert nach_restart.bestaetigungen == 0
    assert nach_restart.tage == 0
    assert nach_restart.letzter_abschluss.startswith("2026-07-18")
    assert nach_restart.abschluss_gueltig == "on"


def test_kurzer_soc_spike_wird_sofort_verworfen_und_resettet_nicht():
    lauf = BalancingAutomationHarness(soc="97", tage=14)
    lauf.set_soc("99")
    for _ in range(29):
        lauf.minute()
    assert lauf.bestaetigungen == 29

    lauf.set_soc("97")
    assert lauf.bestaetigungen == 0
    assert lauf.tage == 14

    lauf.set_soc("99")
    lauf.minute()
    assert lauf.bestaetigungen == 1
    assert lauf.tage == 14


def test_sensorfehler_verwirft_laufende_bestaetigungen_sofort():
    lauf = BalancingAutomationHarness(soc="99", tage=14)
    for _ in range(10):
        lauf.minute()
    lauf.set_soc("unavailable")
    assert lauf.bestaetigungen == 0
    assert lauf.tage == 14


def test_tages_latch_verhindert_mehrfachen_abschluss():
    lauf = BalancingAutomationHarness(soc="99", tage=14)
    for _ in range(30):
        lauf.minute()
    assert lauf.tage == 0
    assert lauf.bestaetigungen == 0

    lauf.states[TAGE] = "1"
    for _ in range(35):
        lauf.minute()
    assert lauf.tage == 1
    assert lauf.bestaetigungen == 0


def test_tageszaehler_wird_nur_ohne_heutigen_abschluss_erhoeht():
    heute = BalancingAutomationHarness(
        soc="90",
        tage=5,
        letzter_abschluss="2026-07-18T10:30:00+02:00",
        abschluss_gueltig="on",
    )
    heute.day_end()
    assert heute.tage == 5

    gestern = BalancingAutomationHarness(
        soc="99",
        tage=5,
        letzter_abschluss="2026-07-17T23:30:00+02:00",
        abschluss_gueltig="on",
    )
    gestern.day_end()
    assert gestern.tage == 6

    unbekannt = BalancingAutomationHarness(
        soc="99", tage=5, letzter_abschluss="unknown"
    )
    unbekannt.day_end()
    assert unbekannt.tage == 6


def test_ha_defaultdatum_am_erststart_ist_kein_balancing_abschluss():
    erststart = BalancingAutomationHarness(
        soc="90",
        tage=5,
        letzter_abschluss="2026-07-18 00:00:00",
        abschluss_gueltig="off",
    )
    erststart.day_end()
    assert erststart.tage == 6


def test_ha_defaultdatum_blockiert_die_erste_balancing_bestaetigung_nicht():
    erststart = BalancingAutomationHarness(
        soc="99",
        tage=14,
        letzter_abschluss="2026-07-18 00:00:00",
        abschluss_gueltig="off",
    )
    for _ in range(30):
        erststart.minute()

    assert erststart.tage == 0
    assert erststart.bestaetigungen == 0
    assert erststart.abschluss_gueltig == "on"
