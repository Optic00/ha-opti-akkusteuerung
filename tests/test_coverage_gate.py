"""The coverage ratchet rejects regressions without rewarding empty tests."""

from tools.check_coverage import check


def report(*, missing=1, percent=90.0, extra=None):
    files = {
        "custom_components/opti_akku/existing.py": {
            "summary": {"missing_lines": missing, "percent_covered": percent}
        }
    }
    if extra is not None:
        files["custom_components/opti_akku/new.py"] = {
            "summary": {"missing_lines": 1, "percent_covered": extra}
        }
    return {"files": files}


BASELINE = {
    "custom_components/opti_akku/existing.py": {
        "max_missing": 1,
        "min_percent": 90.0,
    }
}


def test_current_baseline_and_well_tested_new_module_pass():
    assert check(report(extra=95), BASELINE) == []


def test_more_missing_lines_and_lower_percentage_both_fail():
    failures = check(report(missing=2, percent=89.9), BASELINE)
    assert any("Missing lines increased" in item for item in failures)
    assert any("Coverage percentage decreased" in item for item in failures)


def test_new_module_and_missing_baseline_module_fail_closed():
    assert "New module below 95%" in check(report(extra=94), BASELINE)[0]
    failures = check({"files": {}}, BASELINE)
    assert failures == [
        "Baseline module missing from report: custom_components/opti_akku/existing.py"
    ]


def test_current_module_cannot_disappear_from_report_and_baseline_together():
    current = {
        "custom_components/opti_akku/existing.py",
        "custom_components/opti_akku/hidden.py",
    }
    failures = check(report(), BASELINE, current)
    assert failures == [
        "Current module missing from report: custom_components/opti_akku/hidden.py",
    ]


def test_malformed_report_and_baseline_are_rejected():
    assert check({}, BASELINE) == ["Coverage report has no files mapping"]
    assert check(report(), {"custom_components/opti_akku/existing.py": {}}) == [
        "Invalid baseline: custom_components/opti_akku/existing.py"
    ]
