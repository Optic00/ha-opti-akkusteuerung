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
