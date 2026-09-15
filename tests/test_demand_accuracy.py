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


def test_unknown_latest_sample_prevents_energy_in_next_interval():
    tracker = DemandAccuracy()
    tracker.observe(NOW, None, plan())
    out = tracker.observe(NOW + timedelta(minutes=1), 600, plan())
    assert out["pending"]["missing_seconds"] == 60


def test_bad_snapshot_does_not_break_observer():
    tracker = DemandAccuracy()
    tracker.restore({"version": 1, "pending": {"start": "not a date"}})
    assert tracker.pending is None


def test_no_pv_or_disabled_does_not_create_trial():
    tracker = DemandAccuracy()
    out = tracker.observe(NOW, 600, {"status": "no_pv_timing"})
    assert out["pending"] is None
