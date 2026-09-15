"""Journal lifecycle, actual output, bounded duration and failure reporting."""

from datetime import datetime, UTC, timedelta
import json

import pytest

from custom_components.opti_akku.shadow import ShadowRecorder

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
