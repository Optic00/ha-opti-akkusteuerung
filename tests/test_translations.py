"""strings.json and translations stay in sync and cover what the code uses."""

import json
import re
import string
from pathlib import Path

import pytest
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers.translation import async_get_translations

from custom_components.opti_akku.errors import translated

COMPONENT = Path(__file__).parents[1] / "custom_components" / "opti_akku"
FILES = {
    "strings": COMPONENT / "strings.json",
    "en": COMPONENT / "translations" / "en.json",
    "de": COMPONENT / "translations" / "de.json",
}
JOURNAL_LABELS = ("shadow_start", "comparison_start", "shadow_stop", "comparison_stop")


def _load(name: str) -> dict:
    return json.loads(FILES[name].read_text(encoding="utf-8"))


def _paths(node, prefix=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _paths(value, (*prefix, key))
    else:
        yield prefix


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_all_languages_have_the_same_keys():
    reference = set(_paths(_load("strings")))
    for name in ("en", "de"):
        assert set(_paths(_load(name))) == reference, name


def test_strings_json_is_the_english_source():
    assert _load("strings") == _load("en")


@pytest.mark.parametrize("name", ["en", "de"])
def test_placeholders_match_the_source(name):
    source, translated = _load("strings"), _load(name)
    for path in _paths(source):
        a, b = source, translated
        for key in path:
            a, b = a[key], b[key]
        if isinstance(a, str):
            assert _placeholders(a) == _placeholders(b), (name, path)


def test_data_descriptions_belong_to_fields_of_their_step():
    data = _load("strings")
    for flow in ("config", "options"):
        for name, step in data[flow]["step"].items():
            extra = set(step.get("data_description", {})) - set(step.get("data", {}))
            assert not extra, (flow, name, extra)



@pytest.mark.parametrize("name", ["strings", "de"])
def test_every_form_field_has_a_description(name):
    """Bronze rule config-flow: each field explains itself in the dialog."""
    data = _load(name)
    missing = [
        f"{flow}/{step_id}/{field}"
        for flow in ("config", "options")
        for step_id, step in data[flow]["step"].items()
        for field in step.get("data", {})
        if not step.get("data_description", {}).get(field, "").strip()
    ]
    assert not missing, missing


def _schema_field_names() -> set[str]:
    """Every field name config_flow.py can put into a form, found without running it.

    Resolves each vol.Required/vol.Optional key: string literals, module
    constants, and loop variables over the field lists the flow builds
    schemas from (including names derived from the loop variable). A key it
    cannot resolve fails the test, so new dynamic fields stay covered.
    Complements the runtime check in tests/form_labels.py, which also checks
    the step, for forms that flow tests render.
    """
    import ast
    import importlib

    from custom_components.opti_akku import config_flow as cf

    tree = ast.parse((COMPONENT / "config_flow.py").read_text(encoding="utf-8"))
    namespace = dict(vars(cf))
    for node in ast.walk(tree):  # function-level imports such as CONTROL_DOMAINS
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            module = importlib.import_module(f"custom_components.opti_akku.{node.module}")
            namespace.update({a.asname or a.name: getattr(module, a.name) for a in node.names})
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def evaluate(expr, bound):
        if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
                and expr.func.attr == "get" and isinstance(expr.func.value, ast.Name)):
            # GROUPS.get(section, []) for any section: all groups.
            return [v for group in namespace[expr.func.value.id].values() for v in group]
        return eval(compile(ast.Expression(expr), "config_flow", "eval"), namespace, bound)

    def resolve(node, var):
        if isinstance(namespace.get(var), str):
            return {namespace[var]}
        scope = parents.get(node)
        while scope is not None:
            if isinstance(scope, (ast.For, ast.comprehension)):
                target = scope.target
                targets = target.elts if isinstance(target, ast.Tuple) else [target]
                loop_vars = [t.id for t in targets]
                values = [v if len(loop_vars) > 1 else (v,) for v in evaluate(scope.iter, {})]
                if var in loop_vars:
                    return {v[loop_vars.index(var)] for v in values}
                derived = [  # name = key.split(".", 1)[1] inside the loop
                    stmt.value for stmt in getattr(scope, "body", [])
                    if isinstance(stmt, ast.Assign) and [getattr(t, "id", None) for t in stmt.targets] == [var]
                ]
                if derived:
                    return {evaluate(derived[0], dict(zip(loop_vars, v, strict=True))) for v in values}
            scope = parents.get(scope)
        return None

    names, unresolved = set(), []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("Required", "Optional")
                and getattr(node.func.value, "id", None) == "vol" and node.args):
            continue
        key = node.args[0]
        found = ({key.value} if isinstance(key, ast.Constant)
                 else resolve(node, key.id) if isinstance(key, ast.Name) else None)
        if found is None:
            unresolved.append(f"config_flow.py:{node.lineno} {ast.unparse(key)}")
        else:
            names |= found
    assert not unresolved, unresolved
    return names


def test_every_schema_field_is_labeled_and_described_somewhere():
    data = _load("strings")
    described = {
        field
        for flow in ("config", "options")
        for step in data[flow]["step"].values()
        for field, label in step.get("data", {}).items()
        if label and step.get("data_description", {}).get(field, "").strip()
    }
    assert not _schema_field_names() - described, sorted(_schema_field_names() - described)

def test_every_translated_exception_key_exists():
    used = set()
    for path in COMPONENT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r'translated\(\s*\w+,\s*(f?)"([^"]+)"', source):
            fstring, key = match.groups()
            if fstring:
                assert key.startswith("{label}_"), (path.name, key)
                used.update(key.replace("{label}", label) for label in JOURNAL_LABELS)
            else:
                used.add(key)
        if path.name == "alerts.py":
            used.update(re.findall(r'self\.delivery_error = "([a-z_]+)"', source))
    assert len(used) > 30
    for name in FILES:
        missing = used - set(_load(name)["exceptions"])
        assert not missing, (name, missing)


def test_no_untranslated_raises_remain():
    pattern = re.compile(
        r'raise (HomeAssistantError|ServiceValidationError|ConfigEntryError|ConfigEntryNotReady)\(\s*f?"'
    )
    offenders = [
        f"{path.name}:{source[:match.start()].count(chr(10)) + 1}"
        for path in COMPONENT.glob("*.py")
        for source in [path.read_text(encoding="utf-8")]
        for match in pattern.finditer(source)
    ]
    assert not offenders


async def test_translated_error_renders_the_english_log_message(hass):
    """HA renders str(error) in English for logs; the frontend shows the user's language."""
    hass.config.components.add("opti_akku")
    await async_get_translations(hass, "en", "exceptions", {"opti_akku"})
    assert str(translated(HomeAssistantError, "inverter_unreachable")) == "The inverter is not reachable"
    error = translated(ConfigEntryError, "huawei_config_invalid", error="KeyError")
    assert error.translation_placeholders == {"error": "KeyError"}
    assert "(KeyError)" in str(error)


def test_translated_state_entities_have_names_in_every_language():
    from custom_components.opti_akku.entity import TRANSLATED_STATE_KEYS, state_translation_key

    for name in FILES:
        entity = _load(name)["entity"]
        for key in TRANSLATED_STATE_KEYS:
            platform = "binary_sensor" if key.startswith("binary_sensor.") else "sensor"
            label = entity[platform].get(state_translation_key(key), {}).get("name", "")
            assert label.strip(), (name, key)


def test_every_described_state_entity_has_a_translated_name():
    """Strategy, source and fixed metadata entities must not fall back to raw names."""
    from custom_components.opti_akku.coordinator import _build_metadata
    from custom_components.opti_akku.engine import StrategyEngine
    from custom_components.opti_akku.entity import TRANSLATED_STATE_KEYS
    from custom_components.opti_akku.sensor import CORE_UNITS, EVCC_COMPARISON_KEY, PLAN_SHADOW_KEY

    own_translation = {"binary_sensor.opti_connection", PLAN_SHADOW_KEY, EVCC_COMPARISON_KEY}
    described = {
        key for key in _build_metadata(StrategyEngine())
        if key not in own_translation and key.removeprefix("sensor.opti_") not in CORE_UNITS
    }
    assert not described - TRANSLATED_STATE_KEYS, sorted(described - TRANSLATED_STATE_KEYS)
