"""Pure, persistent Opti strategy engine, independent of Home Assistant imports.

The shipped JSON contains only reviewed templates from the predecessor project.
Templates read a virtual state graph; they cannot call HA services or write Modbus.
Each entity is evaluated once per observation in dependency order, with its old
``this`` snapshot shared by state and attribute templates. The separate hardware
coordinator is responsible for scheduling observations and authorizing writes.
"""
from __future__ import annotations

import ast
import copy
import datetime as dt
import graphlib
import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jinja2

_UNKNOWN = {"unknown", "unavailable"}
_TRUE = {"true", "yes", "on", "1", "enable", "enabled"}
_ENTITY = re.compile(r"\b(?:sensor|binary_sensor|input_number|input_boolean|input_select|input_datetime|input_text|counter|sun)\.[a-z0-9_]+\b")
_MODE = "input_select.akkusteuerung_modus"
_MASTER = "input_boolean.akku_opti_automatik"
_MINUTES = "counter.opti_balancing_done_minuten"
_DAYS = "counter.tage_seit_akku100"
_DONE_VALID = "input_boolean.opti_balancing_abschluss_gueltig"
_DONE_AT = "input_datetime.opti_balancing_letzter_abschluss"
_PREVIEW = "sensor.opti_strategie_vorschau"
_MISSING = object()


@dataclass(frozen=True)
class Evaluation:
    """A complete virtual state view, the selected mode and internal mutations."""

    states: dict[str, Any]
    attributes: dict[str, dict[str, Any]]
    mode: str
    reason: str
    helper_updates: dict[str, Any]
    decision_id: str = "unknown"


def load_resources() -> dict[str, Any]:
    """Read the bundled strategy and helper schemas (no user configuration)."""
    path = Path(__file__).with_name("resources") / "strategy.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _float(value: Any, default: Any = _MISSING) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        if default is _MISSING:
            raise ValueError("Non-numeric template input") from None
        return default


def _int(value: Any, default: Any = 0) -> Any:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _state(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)) and not _finite(value):
        return "unavailable"
    if isinstance(value, str) and value.strip().lower() in {
        "nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"
    }:
        return "unavailable"
    return str(value)


def _truth(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _list(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class StrategyEngine:
    """Evaluate a trusted, bundled strategy against timestamped observations.

    ``now`` must be timezone aware and use the configured HA local timezone.
    Call at least every 30 seconds, and on changed sources/options, to service
    the 30/60-second debounce and minute-based balancing confirmation. Multiple
    calls at the same timestamp neither add samples nor confirm another minute.
    External source states are never retained when omitted from a later call.

    Safety boundary: invalid core SoC/capacity or disabled automation selects
    Pause immediately. Missing battery temperature makes the derived charge
    limit unavailable, and <= -5 / >= 50 degC produces 0 W. The hardware
    coordinator MUST enforce these limits for automatic AND manual commands;
    a selected mode by itself is never permission to ignore missing limits.
    EV fast charging uses the source latch (immediate on, 300s off, unknown
    retains an active lock, cold start unknown is off). The coordinator may
    apply a stricter stop on unknown configured EV inputs.
    """

    def __init__(self, resource_data: dict | None = None) -> None:
        self.resources = copy.deepcopy(resource_data if resource_data is not None else load_resources())
        if self.resources.get("schema_version", 1) != 1:
            raise ValueError("Unsupported strategy resource version")
        self._helper_defaults = {
            key: _state(value) for key, value in self.resources.get("helper_defaults", {}).items()
        }
        self._helpers = dict(self._helper_defaults)
        self._entities: dict[str, dict] = {}
        for block_index, block in enumerate(self.resources.get("template_blocks", [])):
            for domain in ("sensor", "binary_sensor"):
                for definition in block.get(domain, []):
                    entity = f"{domain}.{definition['unique_id']}"
                    if entity in self._entities:
                        raise ValueError(f"Duplicate internal entity: {entity}")
                    self._entities[entity] = {
                        "definition": definition,
                        "variables": block.get("variables", {}),
                        "conditions": block.get("conditions", []),
                        "block": block_index,
                        "binary": domain == "binary_sensor",
                    }
        graph = {}
        for entity, spec in self._entities.items():
            dependencies = set()
            for value in _strings(spec):
                dependencies.update(_ENTITY.findall(value))
            graph[entity] = (dependencies & self._entities.keys()) - {entity}
        try:
            self._order = tuple(graphlib.TopologicalSorter(graph).static_order())
        except graphlib.CycleError as err:
            raise ValueError("Cyclic bundled strategy dependency") from err
        self._derived_states: dict[str, str] = {}
        self._derived_attrs: dict[str, dict] = {}
        self._delays: dict[str, dict] = {}
        self._samples: dict[str, list[list[float]]] = {}
        self._last_evaluation: str | None = None
        self._last_minute: int | None = None
        self._last_daily_increment: str | None = None
        self._starting = True
        self._states: dict[str, str] = {}
        self._attributes: dict[str, dict] = {}
        self._updates: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._now = dt.datetime.now(dt.timezone.utc)
        self._templates: dict[str, jinja2.Template] = {}
        self._env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self._env.globals.update(
            states=self._get_state,
            state_attr=self._get_attribute,
            has_value=self._has_value,
            is_state=lambda entity, state: self._get_state(entity) == _state(state),
            is_number=_finite,
            now=lambda: self._now,
            today_at=self._today_at,
            as_timestamp=self._as_timestamp,
            timedelta=dt.timedelta,
            min=min,
            max=max,
        )
        self._env.filters.update(
            float=_float,
            int=_int,
            as_datetime=self._as_datetime,
            as_local=lambda value: value.astimezone(self._now.tzinfo) if isinstance(value, dt.datetime) else value,
            as_utc=lambda value: value.astimezone(dt.timezone.utc) if isinstance(value, dt.datetime) else value,
            round=lambda value, precision=0: round(float(value), precision),
            median=statistics.median,
        )

        # Constructed in HA's executor: syntax errors surface during setup and
        # no first-evaluation template compilation blocks the coordinator.
        for value in _strings({
            "templates": self.resources.get("template_blocks", []),
            "strategy": self.resources.get("strategy", {}),
            "balancing": self.resources.get("balancing_automations", []),
        }):
            if ("{{" in value or "{%" in value) and value not in self._templates:
                self._templates[value] = self._env.from_string(value)

    def restore(self, snapshot: dict) -> None:
        """Restore internal state. Offline time never confirms pending timers."""
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            return
        self._helpers = dict(self._helper_defaults)
        for entity, value in snapshot.get("helpers", {}).items():
            if entity in self._helper_defaults:
                self._helpers[entity] = _state(value)
        self._derived_states = {
            key: _state(value) for key, value in snapshot.get("states", {}).items()
            if key in self._entities
        }
        self._derived_attrs = {
            key: copy.deepcopy(value)
            for key, value in snapshot.get("attributes", {}).items()
            if key in self._entities and isinstance(value, dict)
        }
        self._samples = {}
        for key, samples in snapshot.get("samples", {}).items():
            if not isinstance(samples, list):
                continue
            self._samples[key] = [
                [float(row[0]), float(row[1])] for row in samples[-1500:]
                if isinstance(row, (list, tuple)) and len(row) == 2
                and _finite(row[0]) and _finite(row[1])
            ]
        # HA template delays are not evidence of continuity over an outage.
        self._delays = {}
        self._last_evaluation = None
        self._last_minute = None
        self._last_daily_increment = snapshot.get("last_daily_increment")
        self._starting = True

    def reset_load_statistics(self) -> None:
        """Discard only house-load histories when input semantics change."""
        for key in ("sensor.opti_house_consumption_60min_w", "sensor.haus_stromverbrauch_60_min"):
            self._samples.pop(key, None)
            self._states.pop(key, None)
            self._attributes.pop(key, None)

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot suitable for HA's local Store."""
        return copy.deepcopy({
            "version": 1,
            "helpers": self._helpers,
            "states": self._derived_states,
            "attributes": self._derived_attrs,
            "samples": self._samples,
            "pending_delays": self._delays,
            "last_evaluation": self._last_evaluation,
            "last_daily_increment": self._last_daily_increment,
        })

    def evaluate(
        self, states: dict[str, Any], attributes: dict[str, dict], now: dt.datetime
    ) -> Evaluation:
        """Evaluate one observation and atomically update the engine snapshot."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("StrategyEngine requires timezone-aware local time")
        self._now = now
        incoming = {key: _state(value) for key, value in states.items()}
        # Config/helper inputs override saved values. Omitted external sensors
        # do not accidentally reuse a formerly healthy measurement.
        self._helpers.update({key: value for key, value in incoming.items() if key in self._helper_defaults})
        self._states = {**self._helpers, **self._derived_states, **incoming}
        self._states.setdefault(_MODE, "Akku Pause")
        self._attributes = {
            **copy.deepcopy(self._derived_attrs),
            **{key: copy.deepcopy(value) for key, value in attributes.items() if isinstance(value, dict)},
        }
        self._updates = {}
        self._errors = {}
        self._update_statistics(incoming)
        self._update_balancing()
        variables: dict[int, dict] = {}
        for entity in self._order:
            # This deliberately configurable gate is the sole derived override.
            if entity == "binary_sensor.opti_winter_charging_allowed" and entity in incoming:
                continue
            self._evaluate_entity(entity, variables)

        self._decision_id = "unknown"
        core_ok = self._core_valid()
        if self._get_state(_MASTER) != "on":
            mode, reason = "Akku Pause", "Opti-Automatik ausgeschaltet"
            self._set_helper(_MODE, mode)
        elif not core_ok:
            mode, reason = "Akku Pause", "Ungültige Kerndaten: SoC oder Batteriekapazität"
            self._set_helper(_MODE, mode)
        else:
            try:
                strategy = self.resources.get("strategy", {})
                if self._all_conditions(strategy.get("conditions", [])):
                    # The preview was evaluated with the same pre-command mode,
                    # so its Schmitt/hysteresis decision is stable for this pass.
                    reason = str(self._get_attribute(_PREVIEW, "grund") or "Strategie")
                    self._actions(strategy.get("actions", []))
                    if self._decision_id == "default" and not self._has_value("sensor.opti_price_level"):
                        self._decision_id = "price_unavailable"
                    mode = self._get_state(_MODE)
                else:
                    mode, reason = "Akku Pause", "Strategie-Eingangsbedingungen nicht erfüllt"
                    self._set_helper(_MODE, mode)
            except (jinja2.TemplateError, TypeError, ValueError, KeyError, ArithmeticError) as err:
                self._errors["strategy"] = type(err).__name__
                mode, reason = "Akku Pause", "Fehler bei der Strategieauswertung"
                self._set_helper(_MODE, mode)

        if self._errors:
            self._attributes.setdefault("sensor.opti_engine_diagnostics", {})["template_errors"] = dict(self._errors)
        self._states["sensor.opti_engine_diagnostics"] = "error" if self._errors else "ok"
        self._attributes.setdefault("sensor.opti_engine_diagnostics", {}).update({
            "reason": reason,
            "decision_id": self._decision_id if not self._errors else "unknown",
            "core_valid": core_ok,
            "source_revision": self.resources.get("source_revision"),
        })
        self._derived_states = {key: self._states.get(key, "unknown") for key in self._entities}
        self._derived_attrs = {key: copy.deepcopy(self._attributes.get(key, {})) for key in self._entities}
        self._helpers = {key: self._states[key] for key in self._helper_defaults if key in self._states}
        self._last_evaluation = now.isoformat()
        self._starting = False
        return Evaluation(
            states=dict(self._states), attributes=copy.deepcopy(self._attributes),
            mode=mode, reason=reason, helper_updates=dict(self._updates),
            decision_id=self._decision_id if not self._errors else "unknown",
        )

    def _get_state(self, entity: str) -> str:
        return self._states.get(entity, "unknown")

    def _get_attribute(self, entity: str, attribute: str) -> Any:
        return self._attributes.get(entity, {}).get(attribute)

    def _has_value(self, entity: str) -> bool:
        return self._get_state(entity) not in _UNKNOWN

    def _today_at(self, value: str = "00:00") -> dt.datetime:
        value = dt.time.fromisoformat(value)
        return self._now.replace(hour=value.hour, minute=value.minute, second=value.second, microsecond=value.microsecond)

    def _as_datetime(self, value: Any, default: Any = None) -> Any:
        if isinstance(value, dt.datetime):
            result = value
        else:
            try:
                result = dt.datetime.fromisoformat(str(value))
            except (ValueError, TypeError):
                return default
        if result.tzinfo is None:
            result = result.replace(tzinfo=self._now.tzinfo)
        return result

    def _as_timestamp(self, value: Any, default: Any = None) -> Any:
        parsed = self._as_datetime(value)
        return parsed.timestamp() if parsed is not None else default

    def _render(self, value: Any, context: dict | None = None) -> Any:
        if isinstance(value, dict):
            return {key: self._render(item, context) for key, item in value.items()}
        if isinstance(value, list):
            return [self._render(item, context) for item in value]
        if not isinstance(value, str) or not ("{{" in value or "{%" in value):
            return value
        template = self._templates.get(value)
        if template is None:
            template = self._templates[value] = self._env.from_string(value)
        rendered = template.render(context or {}).strip()
        try:
            return ast.literal_eval(rendered)
        except (ValueError, TypeError, SyntaxError):
            return rendered

    def _evaluate_entity(self, entity: str, variable_cache: dict[int, dict]) -> None:
        spec = self._entities[entity]
        definition = spec["definition"]
        old_state = self._derived_states.get(entity, "unknown")
        old_attrs = copy.deepcopy(self._derived_attrs.get(entity, {}))
        context = {"this": SimpleNamespace(state=old_state, attributes=old_attrs)}
        try:
            if spec["block"] not in variable_cache:
                block_variables = {}
                for key, template in spec["variables"].items():
                    block_variables[key] = self._render(template, block_variables)
                variable_cache[spec["block"]] = block_variables
            context.update(variable_cache[spec["block"]])
            if not self._all_conditions(spec["conditions"], context):
                # Triggered entities preserve their latch when the trigger guard fails.
                self._states[entity] = old_state
                self._attributes[entity] = old_attrs
                return
            if not _truth(self._render(definition.get("availability", True), context)):
                self._states[entity] = "unavailable"
                self._attributes[entity] = old_attrs
                self._delays.pop(entity, None)
                return
            value = self._render(definition["state"], context)
            new_attrs = self._render(definition.get("attributes", {}), context)
            if spec["binary"]:
                value = self._debounce(entity, _truth(value), old_state, definition)
            self._states[entity] = _state(value)
            self._attributes[entity] = new_attrs
        except (jinja2.TemplateError, TypeError, ValueError, KeyError, ArithmeticError) as err:
            # A failed derived input cannot silently masquerade as a valid zero.
            self._states[entity] = "unavailable"
            self._attributes[entity] = {**old_attrs, "template_error": type(err).__name__}
            self._delays.pop(entity, None)
            self._errors[entity] = type(err).__name__

    def _debounce(self, entity: str, desired: bool, old_state: str, definition: dict) -> str:
        target = "on" if desired else "off"
        old = "on" if old_state == "on" else "off"
        if target == old:
            self._delays.pop(entity, None)
            return target
        delay = definition.get("delay_on" if desired else "delay_off", {})
        seconds = self._duration(delay)
        if seconds <= 0:
            self._delays.pop(entity, None)
            return target
        timestamp = self._now.timestamp()
        pending = self._delays.get(entity)
        if pending is None or pending["target"] != target or pending["since"] > timestamp:
            self._delays[entity] = {"target": target, "since": timestamp}
            return old
        if timestamp - pending["since"] >= seconds:
            self._delays.pop(entity, None)
            return target
        return old

    @staticmethod
    def _duration(value: Any) -> float:
        if isinstance(value, dict):
            return sum(float(value.get(key, 0)) * factor for key, factor in (
                ("days", 86400), ("hours", 3600), ("minutes", 60), ("seconds", 1), ("milliseconds", 0.001)
            ))
        if isinstance(value, str) and ":" in value:
            hours, minutes, seconds = map(float, value.split(":"))
            return hours * 3600 + minutes * 60 + seconds
        return float(value or 0)

    def _all_conditions(self, conditions: Any, context: dict | None = None) -> bool:
        if isinstance(conditions, str):
            return _truth(self._render(conditions, context))
        return all(self._condition(item, context) for item in _list(conditions)) if conditions else True

    def _condition(self, condition: Any, context: dict | None = None) -> bool:
        if isinstance(condition, str):
            return _truth(self._render(condition, context))
        kind = condition["condition"]
        if kind == "template":
            return _truth(self._render(condition["value_template"], context))
        if kind == "state":
            desired = {_state(value) for value in _list(condition["state"])}
            return all(self._get_state(entity) in desired for entity in _list(condition["entity_id"]))
        if kind == "numeric_state":
            for entity in _list(condition["entity_id"]):
                value = self._get_state(entity)
                if not _finite(value):
                    return False
                for comparison in ("above", "below"):
                    if comparison not in condition:
                        continue
                    threshold = condition[comparison]
                    if not _finite(threshold):
                        threshold = self._get_state(threshold)
                    if not _finite(threshold):
                        return False
                    if comparison == "above" and not float(value) > float(threshold):
                        return False
                    if comparison == "below" and not float(value) < float(threshold):
                        return False
            return True
        if kind in {"or", "and", "not"}:
            values = [self._condition(item, context) for item in condition["conditions"]]
            return any(values) if kind == "or" else (all(values) if kind == "and" else not any(values))
        if kind == "sun":
            if condition.get("after") == "sunrise" and condition.get("before") == "sunset":
                return self._get_state("sun.sun") == "above_horizon"
            raise ValueError("Unsupported bundled sun condition")
        if kind == "trigger":
            return bool(context) and context.get("trigger_id") in _list(condition["id"])
        raise ValueError(f"Unsupported bundled condition: {kind}")

    def _set_helper(self, entity: str, value: Any) -> None:
        value = _state(value)
        if self._get_state(entity) != value:
            self._updates[entity] = value
        self._states[entity] = value

    def _actions(self, actions: list, context: dict | None = None) -> None:
        for action in actions:
            if "choose" in action:
                sequence = action.get("default", [])
                for option in action["choose"]:
                    if self._all_conditions(option.get("conditions", []), context):
                        sequence = option.get("sequence", [])
                        break
                self._actions(sequence, context)
                continue
            if "condition" in action:
                if not self._condition(action, context):
                    return
                continue
            service = action.get("action")
            if service is None:
                raise ValueError("Unsupported bundled action")
            data = self._render(action.get("data", {}), context)
            for entity in _list(action.get("target", {}).get("entity_id", [])):
                if entity not in self._helper_defaults:
                    raise ValueError("Bundled action targets non-helper entity")
                if service == "input_select.select_option":
                    value = data["option"]
                    if entity == _MODE:
                        self._decision_id = action.get("decision_id", "unknown")
                elif service == "input_boolean.turn_on":
                    value = "on"
                elif service == "input_boolean.turn_off":
                    value = "off"
                elif service == "input_number.set_value":
                    value = data["value"]
                elif service == "counter.reset":
                    value = 0
                elif service == "counter.increment":
                    value = _int(self._get_state(entity)) + 1
                elif service == "input_datetime.set_datetime":
                    value = data.get("datetime", self._now.isoformat())
                else:
                    raise ValueError(f"Unsupported bundled action: {service}")
                self._set_helper(entity, value)

    def _core_valid(self) -> bool:
        soc = self._get_state("sensor.opti_soc")
        capacity = self._get_state("sensor.opti_battery_capacity_kwh")
        return _finite(soc) and -0.1 < float(soc) < 100.1 and _finite(capacity) and float(capacity) > 0

    def _update_statistics(self, incoming: dict) -> None:
        timestamp = self._now.timestamp()
        for definition in self.resources.get("statistics", []):
            entity = f"sensor.{definition['unique_id']}"
            if entity in incoming:
                continue
            source = definition["entity_id"]
            max_age = self._duration(definition["max_age"])
            rows = self._samples.get(entity, [])
            rows = [row for row in rows if timestamp - max_age <= row[0] <= timestamp]
            value = self._get_state(source)
            if _finite(value) and (not rows or rows[-1][0] != timestamp):
                rows.append([timestamp, float(value)])
            rows = rows[-definition.get("sampling_size", 1500):]
            self._samples[entity] = rows
            self._states[entity] = (
                _state(round(statistics.mean(row[1] for row in rows), definition.get("precision", 0)))
                if rows else "unavailable"
            )
            self._attributes[entity] = {"sample_count": len(rows), "max_age_seconds": max_age}

    def _update_balancing(self) -> None:
        # The original action tree remains the oracle; this scheduler supplies
        # only its three meaningful triggers and avoids counting an offline gap.
        automations = self.resources.get("balancing_automations", [])
        if len(automations) != 2:
            return
        increment, reset = automations
        minute = int(self._now.timestamp() // 60)
        soc = self._get_state("sensor.opti_soc")
        done = self._get_state("input_number.opti_balancing_done_soc")
        valid = _finite(soc) and _finite(done)
        if self._starting:
            trigger = "ha_start"
        elif not valid:
            trigger = "sensorfehler"
        elif float(soc) <= float(done):
            trigger = "rueckfall"
        elif minute != self._last_minute:
            trigger = "minute"
        else:
            trigger = None
        if trigger is not None:
            self._actions(reset["actions"], {"trigger_id": trigger})
        if not self._starting and minute != self._last_minute and self._now.hour == 23 and self._now.minute == 59:
            day = self._now.date().isoformat()
            if day != self._last_daily_increment:
                if self._all_conditions(increment.get("conditions", [])):
                    self._actions(increment["actions"])
                self._last_daily_increment = day
        self._last_minute = minute
