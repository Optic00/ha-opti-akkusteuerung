"""Fake-HA-Jinja-Umgebung: rendert die echten Templates aus den YAML-Dateien
mit synthetischen States. Nachbau nur der hier benutzten HA-Funktionen/Filter."""
from __future__ import annotations

import datetime as dt
import pathlib
import statistics
import types
from ast import literal_eval
from zoneinfo import ZoneInfo

import jinja2
import yaml

TZ = ZoneInfo("Europe/Berlin")
REPO = pathlib.Path(__file__).resolve().parent.parent


class FakeHass:
    def __init__(self, states=None, attrs=None, now=None, this_attributes=None,
                 this_state=None):
        self.states_map = dict(states or {})
        self.attrs_map = {k: dict(v) for k, v in (attrs or {}).items()}
        self.now_value = now or dt.datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
        self.this_attributes = dict(this_attributes or {})
        self.this_state = this_state

    def states(self, entity_id):
        return self.states_map.get(entity_id, "unknown")

    def state_attr(self, entity_id, attr):
        return self.attrs_map.get(entity_id, {}).get(attr)

    def has_value(self, entity_id):
        return self.states_map.get(entity_id) not in (None, "unknown", "unavailable")

    def is_state(self, entity_id, value):
        return self.states_map.get(entity_id) == value

    def now(self):
        return self.now_value

    def today_at(self, timestr="00:00"):
        parts = timestr.split(":")
        return self.now_value.replace(
            hour=int(parts[0]), minute=int(parts[1]) if len(parts) > 1 else 0,
            second=0, microsecond=0)


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_datetime(value, default=None):
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return default


def _as_local(value):
    return value.astimezone(TZ) if isinstance(value, dt.datetime) else value


def _as_timestamp(value, default=None):
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.timestamp()


def _setup(env, hass):
    env.globals.update(
        states=hass.states, state_attr=hass.state_attr, has_value=hass.has_value,
        is_state=hass.is_state, now=hass.now, today_at=hass.today_at,
        as_timestamp=_as_timestamp,
        timedelta=dt.timedelta, min=min, max=max,
        this=types.SimpleNamespace(attributes=hass.this_attributes,
                                   state=hass.this_state),
    )
    env.filters.update({
        "float": _float, "int": _int,
        "as_datetime": _as_datetime, "as_local": _as_local,
        "round": lambda v, n=0: round(float(v), n),
        # HA-median: ungerade Anzahl -> mittleres Element, gerade -> Mittelwert
        # der beiden mittleren (wie statistics.median).
        "median": lambda v: statistics.median(float(x) for x in v),
    })
    return env


def render(hass, template_str):
    env = _setup(jinja2.Environment(), hass)
    return env.from_string(template_str).render().strip()


def render_native(hass, template_str):
    """Nachbau von HAs Template.async_render(parse_result=True): render zu String,
    stripped (echtes HA macht render_result.strip()), dann literal_eval-Versuch."""
    env = _setup(jinja2.Environment(), hass)
    render_result = env.from_string(template_str).render().strip()
    try:
        return literal_eval(render_result)
    except (ValueError, TypeError, SyntaxError, MemoryError):
        return render_result


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_template_entity(cfg, kind, unique_id):
    """Sucht in einer Package-Datei (Schlüssel 'template') den Sensor/Binary-Sensor."""
    for block in cfg["template"]:
        for entity in block.get(kind, []):
            if entity.get("unique_id") == unique_id:
                return entity
    raise KeyError(f"{kind}/{unique_id} nicht gefunden")


def find_trigger_block_variables(cfg, var_name):
    """Liefert das variables-Template eines trigger-basierten template-Blocks."""
    for block in cfg["template"]:
        if "triggers" in block and var_name in block.get("variables", {}):
            return block["variables"][var_name]
    raise KeyError(f"variables/{var_name} nicht gefunden")


def find_automation_condition(cfg, option_alias):
    """Liefert die conditions-Liste einer choose-Option aus opti_strategie.yaml."""
    automation = cfg[0]
    for action in automation["actions"]:
        for option in action.get("choose", []) or []:
            if option.get("alias") == option_alias:
                return option["conditions"]
    raise KeyError(f"Option '{option_alias}' nicht gefunden")
