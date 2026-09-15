"""Build the runtime resource from strategy sources owned by this repository."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "strategy"
OUTPUT = ROOT / "custom_components/opti_akku/resources/strategy.json"
PARTS = {
    "provenance": {"schema_version", "source_revision", "source_repository", "source_files"},
    "settings": {"helper_definitions", "helper_defaults"},
    "templates": {"template_blocks"},
    "statistics": {"statistics"},
    "decisions": {"strategy", "fail_safe"},
    "balancing": {"balancing_automations"},
    "porting_notes": {"porting_notes"},
}


def build(source: Path = SOURCE) -> dict:
    """Read only the explicit local source files, never a legacy checkout."""
    result = {}
    for name, expected in PARTS.items():
        document = yaml.safe_load((source / f"{name}.yaml").read_text())
        if not isinstance(document, dict) or document.keys() != expected:
            raise ValueError(f"Unexpected strategy sections in {name}.yaml")
        result.update(document)
    if result["schema_version"] != 1:
        raise ValueError("Unsupported strategy schema")
    return result


def render(source: Path = SOURCE) -> str:
    return json.dumps(build(source), ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the runtime bundle is stale")
    args = parser.parse_args()
    content = render()
    if args.check:
        if OUTPUT.read_text() != content:
            parser.exit(1, "Strategy bundle is stale; run python tools/build_strategy_resources.py\n")
        print("Strategy bundle matches local sources")
    else:
        OUTPUT.write_text(content)
        print("Built strategy bundle from local sources")


if __name__ == "__main__":
    main()
