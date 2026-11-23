"""Tests for the bounded decision history in diagnostics (#222)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from custom_components.opti_akku.decision_log import MAX_SEGMENTS, DecisionLog

START = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)


def data(mode="Akku Dynamisch", reason="Standard", soc=40.0, **extra):
    return {
        "mode": mode,
        "engine_requested_mode": mode,
        "decision_id": "default",
        "reason": reason,
        "online": True,
        "source_errors": {},
        "strategy_enabled": True,
        "write_enabled": True,
        "command_result_this_update": "not_attempted",
        "states": {"sensor.opti_soc": soc, "sensor.opti_price_current_ct_kwh": 31.234},
        **extra,
    }


def test_unchanged_decision_extends_one_segment():
    log = DecisionLog()
    log.record(START, data(reason="Halten bei 40.0 %", soc=40.0))
    log.record(START + timedelta(seconds=30), data(reason="Halten bei 39.5 %", soc=39.5))
    log.record(START + timedelta(seconds=60), data(reason="Halten bei 39.0 %", soc=39.0))

    [segment] = log.report(START + timedelta(seconds=60))
    assert segment["from"] == START.isoformat()
    assert segment["until"] == (START + timedelta(seconds=60)).isoformat()
    assert segment["cycles"] == 3
    assert segment["reason"] == "Halten bei 40.0 %"
    assert segment["reason_last"] == "Halten bei 39.0 %"
    assert (segment["soc"], segment["soc_last"]) == (40.0, 39.0)
    assert segment["price_ct"] == 31.23
    assert "key" not in segment


def test_changed_mode_health_or_decision_starts_new_segment():
    log = DecisionLog()
    log.record(START, data())
    log.record(START + timedelta(minutes=1), data(mode="Akku Netzladen", decision_id="peak_precharge"))
    log.record(START + timedelta(minutes=2), data(mode="Akku Netzladen", decision_id="peak_precharge",
                                                  online=False))
    log.record(START + timedelta(minutes=3), data(mode="Akku Netzladen", decision_id="peak_precharge",
                                                  online=False, command_result_this_update="failed"))
    log.record(START + timedelta(minutes=3, seconds=30),
               data(mode="Akku Netzladen", decision_id="peak_precharge", online=False,
                    command_result_this_update="superseded"))

    segments = log.report(START + timedelta(minutes=3))
    assert [s["mode"] for s in segments] == ["Akku Dynamisch", "Akku Netzladen", "Akku Netzladen"]
    assert [s["online"] for s in segments] == [True, True, False]
    assert segments[1]["decision_id"] == "peak_precharge"
    assert segments[2]["write_failures"] == 1


def test_history_keeps_24_hours_and_a_fixed_count():
    log = DecisionLog()
    log.record(START, data(mode="Akku Pause"))
    log.record(START + timedelta(hours=25), data())
    assert [s["mode"] for s in log.report(START + timedelta(hours=25))] == ["Akku Dynamisch"]

    for index in range(MAX_SEGMENTS + 20):
        log.record(START + timedelta(hours=26, seconds=index),
                   data(mode="Akku Pause" if index % 2 else "Akku Dynamisch"))
    assert len(log.report(START + timedelta(hours=26, seconds=MAX_SEGMENTS + 20))) == MAX_SEGMENTS


def test_foreign_entity_ids_are_masked():
    log = DecisionLog()
    log.record(START, data(reason="Quelle sensor.private_meter fehlt, sensor.opti_test ok, "
                                  "Host 192.168.1.20, 3.5 kW", decision_id="input_select.private"))
    [segment] = log.report(START)
    assert segment["reason"] == "Quelle <entity> fehlt, <entity> ok, Host <host>, 3.5 kW"
    assert segment["decision_id"] == "<entity>"
    assert "private" not in json.dumps(segment)


def test_restore_round_trip_and_restart_split():
    log = DecisionLog()
    log.record(START, data())
    restored = DecisionLog()
    restored.restore(json.loads(json.dumps(log.snapshot())))
    restored.record(START + timedelta(minutes=1), data())

    segments = restored.report(START + timedelta(minutes=1))
    assert len(segments) == 2
    assert segments[1]["after_restart"] is True
    assert "after_restart" not in segments[0]


def test_restore_rejects_malformed_data():
    for saved in ({"version": 1, "segments": []}, {"version": 2, "segments": "x"},
                  {"version": 2, "segments": [{"from": "x"}]},
                  {"version": 2, "segments": [{}] * (MAX_SEGMENTS + 1)}):
        log = DecisionLog()
        log.restore(saved)
        assert log.report(START) == []


def test_restore_rebuilds_segments_from_allowed_fields():
    saved = {"version": 2, "segments": [{
        "from": START.isoformat(), "until": (START + timedelta(minutes=5)).isoformat(),
        "cycles": 10, "write_failures": 0, "mode": "Akku Pause",
        "reason": "x" * 500 + " sensor.private_meter", "secret": "token-123", "soc": 140,
        "price_ct": "nan",
    }]}
    log = DecisionLog()
    log.restore(saved)
    [segment] = log.report(START + timedelta(minutes=5))
    assert "secret" not in segment
    assert len(segment["reason"]) == 200
    assert segment["soc"] is None and segment["price_ct"] is None
    assert "token-123" not in json.dumps(log.snapshot())


def test_first_start_is_not_marked_as_restart():
    log = DecisionLog()
    log.restore(None)
    log.record(START, data())
    [segment] = log.report(START)
    assert "after_restart" not in segment


def test_long_segment_is_split_hourly_and_pruned_by_start():
    log = DecisionLog()
    for hour in range(26):
        log.record(START + timedelta(hours=hour), data(soc=float(hour)))
        log.record(START + timedelta(hours=hour, minutes=30), data(soc=float(hour)))
    now = START + timedelta(hours=25, minutes=30)
    segments = log.report(now)
    assert all(datetime.fromisoformat(s["from"]) >= now - timedelta(hours=24) for s in segments)
    assert all(s.get("continued") for s in segments[1:])
    assert segments[0]["soc"] == 2.0


def test_manual_mode_is_recorded():
    log = DecisionLog()
    log.record(START, data(manual_mode="Akku Netzladen"))
    [segment] = log.report(START)
    assert segment["manual_mode"] == "Akku Netzladen"
