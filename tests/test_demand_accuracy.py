"""Frozen prediction versus observed household energy, including missing coverage."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from custom_components.opti_akku.demand_accuracy import DemandAccuracy

NOW = datetime(2026, 9, 14, 6, tzinfo=UTC)


def plan():
    return {
        "status": "ready",
        "pv_cover_from": (NOW + timedelta(hours=1)).isoformat(),
        "expected_load_kwh": 0.5,
        "profile_ready": True,
    }


def test_frozen_prediction_is_compared_not_replaced_by_later_forecasts():
    tracker = DemandAccuracy()
    tracker.observe(NOW, 600, plan())
    for i in range(1, 61):
        out = tracker.observe(NOW + timedelta(minutes=i), 600, {**plan(), "expected_load_kwh": 99})
    completed = out["completed"][0]
    assert completed["predicted_kwh"] == 0.5
    assert completed["actual_kwh"] == pytest.approx(0.6)
    assert completed["error_kwh"] == 0.1
    assert completed["coverage_percent"] == 100


def test_gap_is_not_interpolated_and_has_no_error_claim():
    tracker = DemandAccuracy()
    tracker.observe(NOW, 600, plan())
    out = tracker.observe(NOW + timedelta(hours=1), 600, {"status": "data_missing"})
    result = out["completed"][0]
    assert result["missing_seconds"] == 3600
    assert result["actual_kwh"] == 0
    assert result["error_kwh"] is None


def test_restart_preserves_trial_but_cuts_offline_energy():
    tracker = DemandAccuracy()
    tracker.observe(NOW, 600, plan())
    tracker.observe(NOW + timedelta(minutes=1), 600, plan())
    other = DemandAccuracy()
    other.restore(json.loads(json.dumps(tracker.snapshot())))
    out = other.observe(NOW + timedelta(minutes=2), 600, plan())
    assert out["pending"]["actual_kwh"] == pytest.approx(0.01)
    assert out["pending"]["missing_seconds"] == 60


def test_snapshot_only_persists_state_that_restore_uses():
    tracker = DemandAccuracy()
    tracker.observe(NOW, 600, plan())

    assert set(tracker.snapshot()) == {"version", "pending"}


def test_unknown_latest_sample_prevents_energy_in_next_interval():
    tracker = DemandAccuracy()
    tracker.observe(NOW, None, plan())
    out = tracker.observe(NOW + timedelta(minutes=1), 600, plan())
    assert out["pending"]["missing_seconds"] == 60


def test_clock_rollback_discards_in_progress_trial():
    tracker = DemandAccuracy()
    tracker.observe(NOW, 600, plan())
    out = tracker.observe(NOW - timedelta(minutes=1), 600, {"status": "data_missing"})
    assert out == {"pending": None, "completed": []}


def test_bad_snapshot_does_not_break_observer():
    tracker = DemandAccuracy()
    tracker.restore({"version": 1, "pending": {"start": "not a date"}})
    assert tracker.pending is None


@pytest.mark.parametrize(
    "saved",
    [
        None,
        {"version": 2, "pending": {}},
        {"version": 1, "pending": []},
        {
            "version": 1,
            "pending": {
                "start": "2026-09-14T06:00:00",
                "end": "2026-09-14T07:00:00",
                "last": "2026-09-14T06:30:00",
                "predicted_kwh": 0.5,
                "actual_kwh": 0.2,
                "covered_seconds": 1800,
                "missing_seconds": 0,
            },
        },
        {
            "version": 1,
            "pending": {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "last": NOW.isoformat(),
                "predicted_kwh": 10**400,
                "actual_kwh": 0.2,
                "covered_seconds": 1800,
                "missing_seconds": 0,
            },
        },
        {
            "version": 1,
            "pending": {
                "start": 1,
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "last": NOW.isoformat(),
                "predicted_kwh": 0.5,
                "actual_kwh": 0.2,
                "covered_seconds": 1800,
                "missing_seconds": 0,
            },
        },
        {
            "version": 1,
            "pending": {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "last": (NOW - timedelta(minutes=1)).isoformat(),
                "predicted_kwh": 0.5,
                "actual_kwh": 0.2,
                "covered_seconds": 1800,
                "missing_seconds": 0,
            },
        },
        {
            "version": 1,
            "pending": {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "last": NOW.isoformat(),
                "predicted_kwh": float("nan"),
                "actual_kwh": 0.2,
                "covered_seconds": 1800,
                "missing_seconds": 0,
            },
        },
        {
            "version": 1,
            "pending": {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "last": NOW.isoformat(),
                "predicted_kwh": 1200.1,
                "actual_kwh": 0.2,
                "covered_seconds": 1800,
                "missing_seconds": 0,
            },
        },
        {
            "version": 1,
            "pending": {
                "start": NOW.isoformat(),
                "end": (NOW + timedelta(hours=1)).isoformat(),
                "last": NOW.isoformat(),
                "predicted_kwh": True,
                "actual_kwh": 0.2,
                "covered_seconds": 1800,
                "missing_seconds": 0,
            },
        },
    ],
)
def test_restore_rejects_untrusted_or_implausible_pending_trials(saved):
    tracker = DemandAccuracy()
    tracker.restore(saved)
    assert tracker.pending is None


def test_no_pv_or_disabled_does_not_create_trial():
    tracker = DemandAccuracy()
    out = tracker.observe(NOW, 600, {"status": "no_pv_timing"})
    assert out["pending"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"pv_cover_from": "not-a-date"},
        {"pv_cover_from": (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()},
        {"expected_load_kwh": float("nan")},
        {"expected_load_kwh": True},
        {"expected_load_kwh": 1200.1},
        {"expected_load_kwh": 10**400},
    ],
)
def test_invalid_prediction_does_not_start_accuracy_trial(changes):
    tracker = DemandAccuracy()

    out = tracker.observe(NOW, 600, {**plan(), **changes})

    assert out == {"pending": None, "completed": []}
