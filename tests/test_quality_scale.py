"""quality_scale.yaml stays complete, valid and consistent with the code.

Hassfest validates this file only for core integrations, so this test does it
for Opti Akku. Rule names follow script/hassfest/quality_scale.py in Home
Assistant core; add new rules there when HA adds them.
"""

from importlib import import_module
from pathlib import Path

import pytest
import yaml

from custom_components.opti_akku.const import PLATFORMS

COMPONENT = Path(__file__).parents[1] / "custom_components" / "opti_akku"

RULES = {
    "bronze": (
        "action-setup", "appropriate-polling", "brands", "common-modules", "config-flow",
        "config-flow-test-coverage", "dependency-transparency", "docs-actions",
        "docs-conditions", "docs-high-level-description", "docs-installation-instructions",
        "docs-removal-instructions", "docs-triggers", "entity-event-setup", "entity-unique-id",
        "has-entity-name", "runtime-data", "test-before-configure", "test-before-setup",
        "unique-config-entry",
    ),
    "silver": (
        "action-exceptions", "config-entry-unloading", "docs-configuration-parameters",
        "docs-installation-parameters", "entity-unavailable", "integration-owner",
        "log-when-unavailable", "parallel-updates", "reauthentication-flow", "test-coverage",
    ),
    "gold": (
        "devices", "diagnostics", "discovery", "discovery-update-info", "docs-data-update",
        "docs-examples", "docs-known-limitations", "docs-supported-devices",
        "docs-supported-functions", "docs-troubleshooting", "docs-use-cases", "dynamic-devices",
        "entity-category", "entity-device-class", "entity-disabled-by-default",
        "entity-translations", "exception-translations", "icon-translations",
        "reconfiguration-flow", "repair-issues", "stale-devices",
    ),
    "platinum": ("async-dependency", "inject-websession", "strict-typing"),
}
ALL_RULES = {rule for tier in RULES.values() for rule in tier}


def _rules() -> dict[str, dict[str, str]]:
    data = yaml.safe_load((COMPONENT / "quality_scale.yaml").read_text(encoding="utf-8"))
    assert set(data) == {"rules"}
    return {
        name: value if isinstance(value, dict) else {"status": value}
        for name, value in data["rules"].items()
    }


def _status(rule: str) -> str:
    return _rules()[rule]["status"]


def test_lists_every_rule_once_with_valid_status():
    rules = _rules()
    assert set(rules) == ALL_RULES
    for name, value in rules.items():
        assert value["status"] in {"done", "todo", "exempt"}, name
        assert set(value) <= {"status", "comment"}, name
        if value["status"] == "exempt":
            assert value.get("comment", "").strip(), f"{name}: exempt needs a reason"


def test_parallel_updates_set_on_every_platform():
    if _status("parallel-updates") != "done":
        pytest.skip("rule not claimed")
    for platform in PLATFORMS:
        module = import_module(f"custom_components.opti_akku.{platform.value}")
        assert isinstance(getattr(module, "PARALLEL_UPDATES", None), int), platform.value


def test_brand_and_diagnostics_claims_hold():
    assert (COMPONENT / "brand" / "icon.png").is_file()
    assert (COMPONENT / "diagnostics.py").is_file()


def test_removal_instructions_are_documented():
    if _status("docs-removal-instructions") != "done":
        pytest.skip("rule not claimed")
    readme = (COMPONENT.parents[1] / "README.md").read_text(encoding="utf-8")
    assert "## Entfernen" in readme
