from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.load_profile import LoadProfile

START = datetime(2026, 9, 13, tzinfo=UTC)


def observe(profile, seconds, value, fingerprint="plant-a", floor=0):
    return profile.observe(value, START + timedelta(seconds=seconds), fingerprint, floor)


def test_irregular_sampling_is_weighted_by_time_not_event_count():
    profile = LoadProfile(max_gap_seconds=600)
    observe(profile, 0, 100)
    observe(profile, 10, 1000)
    result = observe(profile, 100, 1000)
    assert result.raw_mean_w == pytest.approx(910)
    assert result.coverage_seconds == 100


def test_window_boundary_retains_predecessor_and_expires_old_time():
    profile = LoadProfile(max_gap_seconds=3600)
    observe(profile, 0, 100)
    observe(profile, 1800, 300)
    result = observe(profile, 4500, 300)
    assert result.coverage_seconds == 3600
    assert result.raw_mean_w == pytest.approx(250)
    assert result.warming_up is False


def test_gap_is_capped_and_latest_invalid_never_returns_stale_value():
    profile = LoadProfile(max_gap_seconds=60)
    observe(profile, 0, 600)
    result = observe(profile, 300, None)
    assert result.raw_mean_w is None
    assert result.forecast_w is None
    assert result.coverage_seconds == 0

    result = observe(profile, 360, 1200)
    assert result.raw_mean_w == 600
    assert result.coverage_seconds == 60


def test_restore_keeps_history_but_does_not_bridge_offline_gap():
    original = LoadProfile(max_gap_seconds=60)
    observe(original, 0, 100)
    observe(original, 15, 200)
    snapshot = original.snapshot()

    restored = LoadProfile(max_gap_seconds=60)
    assert restored.restore(snapshot, now=START + timedelta(seconds=600), fingerprint="plant-a")
    result = observe(restored, 600, 1000)
    assert result.coverage_seconds == 15
    assert result.raw_mean_w == 100


def test_fingerprint_change_resets_history():
    profile = LoadProfile(max_gap_seconds=60)
    observe(profile, 0, 100)
    observe(profile, 15, 100)
    result = observe(profile, 30, 900, fingerprint="plant-b")
    assert result.raw_mean_w == 900
    assert result.coverage_seconds == 0
    assert result.warming_up is True


def test_backward_clock_clears_history_and_starts_fresh_without_elapsed_time():
    profile = LoadProfile(max_gap_seconds=60)
    observe(profile, 100, 100)
    result = observe(profile, 130, 300)
    assert result.raw_mean_w == 100
    assert result.coverage_seconds == 30

    result = observe(profile, 90, 700)
    assert result.raw_mean_w == 700
    assert result.forecast_w == 700
    assert result.coverage_seconds == 0
    assert result.warming_up is True

    result = observe(profile, 105, 900)
    assert result.raw_mean_w == 700
    assert result.coverage_seconds == 15


def test_floor_is_applied_after_mean_and_zero_remains_zero_without_floor():
    profile = LoadProfile(max_gap_seconds=60)
    observe(profile, 0, 0)
    zero = observe(profile, 30, 0)
    assert zero.raw_mean_w == 0
    assert zero.forecast_w == 0

    profile = LoadProfile(max_gap_seconds=60)
    observe(profile, 0, 0)
    observe(profile, 30, 100)
    floored = observe(profile, 60, 100, floor=80)
    assert floored.raw_mean_w == 50
    assert floored.forecast_w == 80


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -1, "invalid", True])
def test_non_finite_observation_is_invalid(bad_value):
    profile = LoadProfile()
    result = observe(profile, 0, bad_value)
    assert result.raw_mean_w is None
    assert result.forecast_w is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(fingerprint="different"),
        lambda data: data["samples"][0].update(value_w=float("nan")),
        lambda data: data["samples"].append(dict(data["samples"][0])),
        lambda data: data["samples"][0].update(timestamp="2026-09-13T00:00:00"),
    ],
)
def test_restore_rejects_incompatible_or_invalid_samples(mutate):
    profile = LoadProfile()
    observe(profile, 0, 100)
    snapshot = profile.snapshot()
    mutate(snapshot)

    restored = LoadProfile()
    assert not restored.restore(snapshot, now=START + timedelta(seconds=1), fingerprint="plant-a")
    assert observe(restored, 1, 200).coverage_seconds == 0


@pytest.mark.parametrize("floor", [True, -1, 5001, float("nan"), float("inf")])
def test_invalid_floor_is_rejected(floor):
    with pytest.raises(ValueError, match="min_load_w"):
        observe(LoadProfile(), 0, 100, floor=floor)


def test_large_finite_values_do_not_overflow_weighted_mean():
    profile = LoadProfile(max_gap_seconds=3600)
    observe(profile, 0, 1e308)
    result = observe(profile, 3600, 1e308)
    assert result.raw_mean_w == 1e308


def test_local_dst_timestamps_are_normalized_before_subtraction():
    berlin = ZoneInfo("Europe/Berlin")
    profile = LoadProfile(max_gap_seconds=3600)
    first = datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=1)
    profile.observe(100, first, "plant-a")
    result = profile.observe(300, second, "plant-a")
    assert result.coverage_seconds == 3600
    assert result.raw_mean_w == 100


def test_sample_cap_drops_old_history_without_extending_a_predecessor():
    profile = LoadProfile(window_seconds=10, max_gap_seconds=5, max_samples=4)
    for second in range(6):
        result = profile.observe(
            0 if second % 2 == 0 else 100,
            START + timedelta(seconds=second),
            "plant-a",
        )
    assert len(profile.snapshot()["samples"]) == 4
    assert result.coverage_seconds == 3
    assert result.raw_mean_w == pytest.approx(100 / 3)


def test_restore_rejects_snapshot_over_sample_cap():
    profile = LoadProfile(window_seconds=10, max_gap_seconds=5, max_samples=4)
    snapshot = {
        "version": 1,
        "fingerprint": "plant-a",
        "samples": [
            {"timestamp": (START + timedelta(seconds=index)).isoformat(), "value_w": 1}
            for index in range(5)
        ],
    }
    assert not profile.restore(snapshot, now=START + timedelta(seconds=5), fingerprint="plant-a")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_seconds": 0},
        {"max_gap_seconds": float("nan")},
        {"window_seconds": 10, "max_gap_seconds": 5, "max_samples": 3},
    ],
)
def test_invalid_profile_bounds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        LoadProfile(**kwargs)


def test_fingerprint_must_be_text_for_observe_and_restore():
    profile = LoadProfile()
    with pytest.raises(TypeError, match="fingerprint"):
        profile.observe(100, START, None)
    with pytest.raises(TypeError, match="fingerprint"):
        profile.restore({}, now=START, fingerprint=None)


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {"version": 2, "fingerprint": "plant-a", "samples": []},
        {"version": 1, "fingerprint": "plant-a", "samples": ["invalid"]},
    ],
)
def test_restore_rejects_untrusted_snapshot_shapes(snapshot):
    profile = LoadProfile()
    assert not profile.restore(snapshot, now=START, fingerprint="plant-a")
    assert profile.snapshot() == {"version": 1, "fingerprint": "plant-a", "samples": []}


def test_sample_cap_compacts_redundant_points_without_changing_curve():
    profile = LoadProfile(window_seconds=10, max_gap_seconds=5, max_samples=4)
    for second in range(6):
        result = profile.observe(500, START + timedelta(seconds=second), "plant-a")
    assert len(profile.snapshot()["samples"]) == 4
    assert result.raw_mean_w == 500
    assert result.coverage_seconds == 5


def test_same_timestamp_replaces_sample_instead_of_double_counting():
    profile = LoadProfile(max_gap_seconds=60)
    observe(profile, 0, 100)
    observe(profile, 0, 500)
    result = observe(profile, 30, 500)
    assert len(profile.snapshot()["samples"]) == 2
    assert result.raw_mean_w == 500
    assert result.coverage_seconds == 30


def test_window_trim_keeps_only_the_needed_predecessor():
    profile = LoadProfile(window_seconds=60, max_gap_seconds=60)
    observe(profile, 0, 100)
    observe(profile, 30, 200)
    result = observe(profile, 120, 300)
    samples = profile.snapshot()["samples"]
    assert len(samples) == 2
    assert samples[0]["timestamp"] == (START + timedelta(seconds=30)).isoformat()
    assert result.raw_mean_w == 200
    assert result.coverage_seconds == 30
