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

    assert set(tracker.snapshot()) == {"version", "pending", "completed"}


def run_trial(tracker, start, *, gap_minutes=()):
    """One-hour trial sampled every minute, optionally without some samples."""
    first = {
        **plan(),
        "pv_cover_from": (start + timedelta(hours=1)).isoformat(),
    }
    tracker.observe(start, 600, first)
    out = None
    for minute in range(1, 61):
        if minute in gap_minutes:
            continue
        out = tracker.observe(start + timedelta(minutes=minute), 600, {"status": "data_missing"})
    return out


def completed_trial(day=0, **changes):
    start = NOW + timedelta(days=day)
    end = start + timedelta(hours=1)
    trial = {
        "start": start.isoformat(), "end": end.isoformat(), "last": end.isoformat(),
        "profile_ready": True, "predicted_kwh": 0.5, "actual_kwh": 0.6,
        "covered_seconds": 3600.0, "missing_seconds": 0.0,
        "coverage_percent": 100.0, "error_kwh": 0.1,
        "note": "House energy until predicted PV onset; not avoided grid import",
    }
    trial.update(changes)
    return trial


def test_completed_trials_survive_restart_roundtrip():
    tracker = DemandAccuracy()
    run_trial(tracker, NOW)
    run_trial(tracker, NOW + timedelta(days=1), gap_minutes=range(10, 20))
    saved = json.loads(json.dumps(tracker.snapshot()))

    restored = DemandAccuracy()
    restored.restore(saved)

    assert restored.completed == tracker.completed
    assert len(restored.completed) == 2
    assert restored.completed[0]["error_kwh"] == 0.1
    gap = restored.completed[1]
    assert gap["missing_seconds"] == 11 * 60
    assert gap["error_kwh"] is None
    assert gap["coverage_percent"] == pytest.approx(81.7)
    assert restored.snapshot() == saved


def test_restored_history_stays_bounded_after_new_completion():
    saved = {"version": 1, "pending": None,
             "completed": [completed_trial(day) for day in range(7)]}
    tracker = DemandAccuracy()
    tracker.restore(saved)
    assert len(tracker.completed) == 7

    out = run_trial(tracker, NOW + timedelta(days=8))

    assert len(out["completed"]) == 7
    assert out["completed"][0]["start"] == completed_trial(1)["start"]
    assert out["completed"][-1]["start"] == (NOW + timedelta(days=8)).isoformat()


def test_restored_completed_claims_are_recomputed_not_trusted():
    claimed = completed_trial(
        covered_seconds=1800.0, missing_seconds=1800.0,
        coverage_percent=100.0, error_kwh=-0.2, note="saved 3 EUR of grid import")
    tracker = DemandAccuracy()
    tracker.restore({"version": 1, "pending": None, "completed": [claimed]})

    result = tracker.completed[0]
    assert result["coverage_percent"] == 50.0
    assert result["error_kwh"] is None
    assert result["note"] == "House energy until predicted PV onset; not avoided grid import"


def test_version_one_snapshot_without_completed_keeps_pending():
    tracker = DemandAccuracy()
    tracker.observe(NOW, 600, plan())
    tracker.observe(NOW + timedelta(minutes=1), 600, plan())
    legacy = {"version": 1, "pending": tracker.snapshot()["pending"]}

    other = DemandAccuracy()
    other.restore(json.loads(json.dumps(legacy)))

    assert other.completed == []
    assert other.pending == tracker.pending
    # The restart interval is still missing, not interpolated.
    out = other.observe(NOW + timedelta(minutes=2), 600, plan())
    assert out["pending"]["missing_seconds"] == 60
    assert out["pending"]["actual_kwh"] == pytest.approx(0.01)


@pytest.mark.parametrize(
    "completed",
    [
        None,
        {"0": completed_trial()},
        [completed_trial(day) for day in range(8)],
        ["not a trial"],
        [completed_trial(start="2026-09-14T06:00:00")],
        [completed_trial(last=(NOW + timedelta(minutes=30)).isoformat())],
        [completed_trial(covered_seconds=3000.0)],
        [completed_trial(covered_seconds=3600.0, missing_seconds=10.0)],
        [completed_trial(actual_kwh=-0.1)],
        [completed_trial(actual_kwh=float("nan"))],
        [completed_trial(predicted_kwh=True)],
        [completed_trial(predicted_kwh=1200.1)],
        [completed_trial(actual_kwh=0.3, covered_seconds=0.0, missing_seconds=3600.0)],
        [completed_trial(end=(NOW + timedelta(hours=25)).isoformat(),
                         last=(NOW + timedelta(hours=25)).isoformat(),
                         covered_seconds=90000.0)],
        [completed_trial(1), completed_trial(0)],
        [completed_trial(0), completed_trial(0)],
        [completed_trial(), completed_trial(1, start="bad")],
    ],
)
def test_corrupt_completed_history_is_discarded_without_losing_pending(completed):
    pending = {
        "start": (NOW + timedelta(days=9)).isoformat(),
        "end": (NOW + timedelta(days=9, hours=1)).isoformat(),
        "last": (NOW + timedelta(days=9, minutes=1)).isoformat(),
        "profile_ready": True, "predicted_kwh": 0.5, "actual_kwh": 0.01,
        "covered_seconds": 60, "missing_seconds": 0,
    }
    tracker = DemandAccuracy()
    tracker.restore({"version": 1, "pending": pending, "completed": completed})

    assert tracker.completed == []
    assert tracker.pending is not None
    assert tracker.pending["covered_seconds"] == 60


def test_history_overlapping_pending_trial_is_dropped():
    pending = {
        "start": (NOW + timedelta(minutes=30)).isoformat(),
        "end": (NOW + timedelta(hours=2)).isoformat(),
        "last": (NOW + timedelta(minutes=30)).isoformat(),
        "profile_ready": False, "predicted_kwh": 0.5, "actual_kwh": 0,
        "covered_seconds": 0, "missing_seconds": 0,
    }
    tracker = DemandAccuracy()
    tracker.restore({"version": 1, "pending": pending, "completed": [completed_trial()]})
    assert tracker.completed == []
    assert tracker.pending is not None


def test_pending_whose_durations_do_not_match_elapsed_time_is_rejected():
    tracker = DemandAccuracy()
    tracker.restore({"version": 1, "pending": {
        "start": NOW.isoformat(),
        "end": (NOW + timedelta(hours=1)).isoformat(),
        "last": (NOW + timedelta(minutes=10)).isoformat(),
        "profile_ready": True, "predicted_kwh": 0.5, "actual_kwh": 0.5,
        "covered_seconds": 3600, "missing_seconds": 0,
    }})
    assert tracker.pending is None


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
