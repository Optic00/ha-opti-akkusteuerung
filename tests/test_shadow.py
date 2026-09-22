"""Journal lifecycle, actual output, bounded duration and failure reporting."""

from datetime import datetime, UTC, timedelta
import json

import pytest

from custom_components.opti_akku.shadow import ShadowRecorder, _demand_sample

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
MODES = ("Akku Pause", "Akku nur Laden")
DATA = {"mode": "Akku Pause", "reason": "Test", "online": True, "source_errors": {},
        "states": {"sensor.opti_soc": "60"}}


def test_shadow_duration_restore_gap_and_no_auto_rearm(tmp_path):
    recorder = ShadowRecorder(tmp_path)
    first = recorder.start(NOW, {"setting": 1}, "input_select.reference", "0.1.0")
    recorder.record(NOW, DATA, {"setting": 1}, "Akku Pause", MODES)
    with pytest.raises(ValueError):
        recorder.start(NOW + timedelta(hours=1), {}, "", "0.1.0")
    restored = ShadowRecorder(tmp_path)
    restored.restore(recorder.snapshot())
    changed = restored.record(NOW + timedelta(hours=2), DATA, {"setting": 2}, "Akku nur Laden", MODES)
    assert changed["samples"] == 2
    assert changed["settings_changes"] == 1
    assert changed["mismatches"] == 1
    assert changed["gaps"] == 1
    final = restored.record(NOW + timedelta(hours=25), DATA, {}, None, MODES)
    assert final["status"] == "completed"
    assert final["deadline"] == first["deadline"]
    assert final["gaps"] == 2
    assert final["max_gap_seconds"] == 22 * 3600
    assert restored.record(NOW + timedelta(days=2), DATA, {}, None, MODES) == final
    path = tmp_path / f"{first['session_id']}.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["type"] for row in rows] == ["start", "sample", "sample", "end"]
    assert all(row.get("read_only") is True for row in rows if row["type"] == "sample")
    assert path.stat().st_mode & 0o777 == 0o600
    second = restored.start(NOW + timedelta(days=2), {}, "", "0.1.0")
    assert second["session_id"] != first["session_id"]
    assert len(list(tmp_path.glob("*.jsonl"))) == 2


def test_shadow_sampling_is_bounded_and_missing_reference_is_not_mismatch(tmp_path):
    recorder = ShadowRecorder(tmp_path)
    recorder.start(NOW, {}, "", "0.1.0")
    for second in range(31):
        data = recorder.record(NOW + timedelta(seconds=second), DATA, {}, "unavailable", MODES)
    assert data["samples"] == 3
    assert data["reference_missing"] == 3
    assert data["comparisons"] == data["mismatches"] == 0


def test_missing_journal_does_not_silently_restart(tmp_path):
    recorder = ShadowRecorder(tmp_path)
    started = recorder.start(NOW, {}, "", "0.1.0")
    (tmp_path / f"{started['session_id']}.jsonl").unlink()
    with pytest.raises(OSError):
        recorder.record(NOW, DATA, {}, None, MODES)
    assert recorder.snapshot()["samples"] == 0


def test_restore_rejects_corrupt_session_before_it_can_resume(tmp_path):
    recorder = ShadowRecorder(tmp_path)
    started = recorder.start(NOW, {}, "input_select.reference", "0.1.0")
    idle = ShadowRecorder(tmp_path)
    idle.restore({})
    assert idle.snapshot() == {"status": "idle"}
    invalid = []
    for key, value in (
        ("status", "unknown"),
        ("session_id", "../outside"),
        ("deadline", (NOW + timedelta(hours=23)).isoformat()),
        ("samples", True),
        ("max_gap_seconds", float("nan")),
        ("max_gap_seconds", 10**400),
        ("last_sample", "not-a-time"),
        ("last_sample", (NOW - timedelta(seconds=1)).isoformat()),
        ("settings", []),
        ("reference_entity", None),
    ):
        state = dict(started)
        state[key] = value
        invalid.append(state)
    inconsistent = dict(started)
    inconsistent["comparisons"] = 1
    invalid.append(inconsistent)

    invalid.append(None)
    for saved in invalid:
        restored = ShadowRecorder(tmp_path)
        restored.restore(saved)
        error = restored.snapshot()
        assert error == {"status": "error", "error": "invalid_saved_session"}
        assert restored.record(NOW, DATA, {}, "Akku Pause", MODES) == error

    journal = tmp_path / f"{started['session_id']}.jsonl"
    assert [json.loads(line)["type"] for line in journal.read_text().splitlines()] == ["start"]


def test_failed_start_restores_previous_state(tmp_path, monkeypatch):
    recorder = ShadowRecorder(tmp_path)
    previous = recorder.snapshot()

    def fail(*args, **kwargs):
        raise OSError("disk error")

    monkeypatch.setattr(recorder, "_append", fail)
    with pytest.raises(OSError, match="disk error"):
        recorder.start(NOW, {}, "", "0.1.0")
    assert recorder.snapshot() == previous


def test_stop_preserves_journal_and_allows_fresh_session(tmp_path):
    recorder = ShadowRecorder(tmp_path)
    first = recorder.start(NOW, {}, "", "0.2.0")
    stopped = recorder.stop(NOW + timedelta(minutes=1))
    assert stopped["status"] == "stopped"
    old = tmp_path / f"{first['session_id']}.jsonl"
    rows = old.read_text()
    assert json.loads(rows.splitlines()[-1])["reason"] == "user_stopped"
    assert recorder.stop(NOW) == stopped
    restored = ShadowRecorder(tmp_path)
    restored.restore(stopped)
    second = restored.start(NOW + timedelta(minutes=2), {}, "", "0.2.0")
    assert second["session_id"] != first["session_id"]
    assert second["samples"] == 0
    assert old.read_text() == rows
    assert datetime.fromisoformat(second["deadline"]) == NOW + timedelta(minutes=2, hours=24)


def test_stop_failure_keeps_running_state(tmp_path, monkeypatch):
    recorder = ShadowRecorder(tmp_path)
    first = recorder.start(NOW, {}, "", "0.2.0")
    def fail(*args, **kwargs):
        raise OSError("disk error")
    monkeypatch.setattr(recorder, "_append", fail)
    with pytest.raises(OSError):
        recorder.stop(NOW)
    assert recorder.snapshot() == first


def test_restored_journal_identifies_new_build_without_resetting_deadline(tmp_path, monkeypatch):
    first = ShadowRecorder(tmp_path)
    session = first.start(NOW, {}, "", "old-version")
    monkeypatch.setattr("custom_components.opti_akku.shadow._build_identity",
                        lambda: {"version": "old-version", "sha256": "old"})
    first.record(NOW, DATA, {}, None, MODES)
    restored = ShadowRecorder(tmp_path)
    restored.restore(first.snapshot())
    monkeypatch.setattr("custom_components.opti_akku.shadow._build_identity",
                        lambda: {"version": "new-version", "sha256": "new"})
    restored.record(NOW + timedelta(seconds=30), DATA, {}, None, MODES)
    rows = [json.loads(row) for row in (tmp_path / f"{session['session_id']}.jsonl").read_text().splitlines()]
    assert rows[1]["build"]["sha256"] == "old"
    assert rows[2]["build"]["sha256"] == "new"
    assert restored.snapshot()["deadline"] == session["deadline"]
    assert restored.snapshot()["samples"] == 2


def test_refill_comparison_uses_strict_allowlist_and_preserves_scalars(tmp_path, monkeypatch):
    recorder = ShadowRecorder(tmp_path)
    session = recorder.start(NOW, {}, "", "0.3.0")
    monkeypatch.setattr("custom_components.opti_akku.shadow._build_identity",
                        lambda: {"version": "0.3.0", "sha256": "test"})
    data = {
        **DATA, "entry_shadow_mode": False, "write_enabled": True,
        "strategy_enabled": True, "command_confirmation": "idle_or_confirmed",
        "device_errors": {"read": "timeout"}, "price_status": "ready",
        "foreign_coordinator_key": "secret",
        "demand_forecast": {
            "status": "ready", "controls_battery": True, "observation_only": False,
            "refill_profile_ready": True, "refill_missing_kwh": 1.23456789,
            "refill_coverage_percent": 78.90123, "refill_target_kwh": {"bad": 1},
            "forecast_slots": ["private"],
            "learned_profile": {"raw": "private"},
            "strategy_comparison": {
                "status": "ready", "observation_only": True, "foreign": "omit",
                "blocks": {
                    "remaining_day": {"status": "ready", "reason": "complete",
                                      "active_score": 4, "candidate_score": 6,
                                      "score_delta": 2, "profile_sources": {"omit": 1}},
                    "unexpected": {"active_score": 99},
                },
            },
        },
    }
    recorder.record(NOW, data, {}, None, MODES)
    rows = [json.loads(row) for row in
            (tmp_path / f"{session['session_id']}.jsonl").read_text().splitlines()]
    sample = rows[-1]
    assert sample["observation_only"] is True
    assert sample["entry_shadow_mode"] is False
    assert sample["write_enabled"] is sample["strategy_enabled"] is True
    assert sample["command_confirmation"] == "idle_or_confirmed"
    assert sample["device_source_error_count"] == 1
    assert sample["price_status"] == "ready"
    demand = sample["demand_forecast"]
    assert demand["controls_battery"] is True
    assert demand["observation_only"] is False
    assert demand["refill_missing_kwh"] == 1.23456789
    assert demand["refill_coverage_percent"] == 78.90123
    assert demand["strategy_comparison"]["observation_only"] is True
    assert demand["strategy_comparison"]["blocks"]["remaining_day"]["score_delta"] == 2
    assert "foreign_coordinator_key" not in sample
    assert not {"forecast_slots", "learned_profile", "refill_target_kwh"} & demand.keys()
    assert "unexpected" not in demand["strategy_comparison"]["blocks"]
    assert "profile_sources" not in demand["strategy_comparison"]["blocks"]["remaining_day"]


def test_demand_allowlist_rejects_invalid_report_shapes_and_scalars():
    assert _demand_sample({"demand_forecast": []}) == {}
    assert _demand_sample({"demand_forecast": {
        "status": "ready", "strategy_comparison": []}}) == {"status": "ready"}
    assert _demand_sample({"demand_forecast": {
        "status": "x" * 129, "refill_missing_kwh": float("inf"),
        "refill_covered": [], "strategy_comparison": {
            "status": "ready", "blocks": []}}}) == {
                "strategy_comparison": {"status": "ready"}}
