"""Reject module coverage regressions and hold new modules to 95 percent."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


NEW_MODULE_MIN_PERCENT = 95.0


def check(
    report: dict[str, Any],
    baseline: dict[str, Any],
    production_paths: set[str] | None = None,
) -> list[str]:
    """Return deterministic coverage-policy violations."""
    files = report.get("files")
    if not isinstance(files, dict):
        return ["Coverage report has no files mapping"]

    failures = []
    if production_paths is not None:
        for path in sorted(production_paths - set(files)):
            failures.append(f"Current module missing from report: {path}")
    for path in sorted(set(baseline) - set(files)):
        failures.append(f"Baseline module missing from report: {path}")
    for path, details in sorted(files.items()):
        if not path.startswith("custom_components/opti_akku/") or not path.endswith(".py"):
            continue
        summary = details.get("summary", {}) if isinstance(details, dict) else {}
        missing = summary.get("missing_lines")
        percent = summary.get("percent_covered")
        if not isinstance(missing, int) or not isinstance(percent, int | float):
            failures.append(f"Invalid coverage summary: {path}")
            continue
        expected = baseline.get(path)
        if expected is None:
            if percent < NEW_MODULE_MIN_PERCENT:
                failures.append(
                    f"New module below {NEW_MODULE_MIN_PERCENT:.0f}%: {path} ({percent:.2f}%)"
                )
            continue
        max_missing = expected.get("max_missing")
        min_percent = expected.get("min_percent")
        if not isinstance(max_missing, int) or not isinstance(min_percent, int | float):
            failures.append(f"Invalid baseline: {path}")
            continue
        if missing > max_missing:
            failures.append(
                f"Missing lines increased: {path} ({missing} > {max_missing})"
            )
        if percent + 1e-9 < min_percent:
            failures.append(
                f"Coverage percentage decreased: {path} ({percent:.2f}% < {min_percent:.2f}%)"
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    baseline = json.loads(args.baseline.read_text())
    root = Path(__file__).resolve().parents[1]
    component = root / "custom_components" / "opti_akku"
    production_paths = {
        path.relative_to(root).as_posix()
        for path in component.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    failures = check(report, baseline, production_paths)
    if failures:
        print("\n".join(failures))
        return 1
    print("Per-module coverage did not regress; new modules meet the 95% floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
