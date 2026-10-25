from datetime import UTC, datetime, timedelta

import pytest

from custom_components.opti_akku.evcc_comparison import (
    MAX_RESPONSE_BYTES,
    MAX_SLOTS,
    EvccComparison,
    classify,
    derive_plan,
    evcc_check,
    fetch_evopt,
    suggestion_status,
)

T0 = datetime(2026, 10, 25, 12, 5, tzinfo=UTC)
SLOT = 900
# Wh per quarter-hour slot; 100 Wh is 400 W, 10 Wh (40 W) stays below evcc's noise threshold.
SLOTS = [
    # charge, discharge, import, export, soc -> action
    (500, 0, 600, 0, 4000),     # grid charging
    (0, 0, 300, 0, 5000),       # idle while importing: hold
    (0, 0, 0, 300, 5000),       # idle while exporting: holdcharge
    (0, 400, 0, 200, 5000),     # discharging into the grid
    (0, 300, 0, 0, 5000),       # plain self consumption
    (10, 10, 300, 0, 1000),     # importing at the minimum: normal, not hold
    (0, 0, 0, 300, 9000),       # exporting while full: normal, not holdcharge
]
ACTIONS = ["charge", "hold", "holdcharge", "discharge", "normal", "normal", "normal"]


def evopt(*, slots=SLOTS, updated=T0 - timedelta(minutes=4), status="Optimal", kinds=None, start=None):
    start = start or datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    kinds = kinds or [{"type": "loadpoint", "name": "lp1"}, {"type": "battery", "name": "byd"}]
    home = {"charging_power": [s[0] for s in slots], "discharging_power": [s[1] for s in slots],
            "state_of_charge": [s[4] for s in slots]}
    other = {"charging_power": [0] * len(slots), "discharging_power": [0] * len(slots)}
    batteries = [home if kind["type"] == "battery" else other for kind in kinds]
    configs = [{"s_min": 1000, "s_max": 9000} for _ in kinds]
    return {
        # Go marshals nanoseconds; Python only parses microseconds.
        "updated": updated.strftime("%Y-%m-%dT%H:%M:%S.123456789+00:00"),
        "req": {"time_series": {"dt": [SLOT] * len(slots)}, "batteries": configs},
        "res": {"status": status, "batteries": batteries,
                "grid_import": [s[2] for s in slots], "grid_export": [s[3] for s in slots]},
        "details": {"timestamp": [(start + timedelta(seconds=SLOT * i)).isoformat() for i in range(len(slots))],
                    "batteryDetails": kinds},
    }


def test_derivation_follows_evccs_rule_for_every_slot():
    plan = derive_plan(evopt(), T0)
    assert plan["status"] == "ready"
    assert plan["current_action"] == "charge"
    assert plan["battery"] == "byd" and plan["home_batteries"] == 1
    assert [segment[2] for segment in plan["segments"]] == ["charge", "hold", "holdcharge", "discharge", "normal"]
    assert plan["segments"][-1][1] == "2026-10-25T13:45:00+00:00"
    assert plan["segments_truncated"] is False
    # The current slot only counts from now on.
    assert plan["hours"]["charge"] == pytest.approx(10 / 60, abs=0.01)
    assert plan["hours"]["normal"] == 0.75
    assert plan["age_seconds"] == 240


def test_each_slot_maps_to_the_documented_action():
    for slots, action in zip(SLOTS, ACTIONS, strict=True):
        plan = derive_plan(evopt(slots=[slots]), T0)
        assert plan["current_action"] == action, slots


def test_past_slots_are_skipped_and_segments_merge():
    now = T0 + timedelta(minutes=30)
    plan = derive_plan(evopt(slots=[SLOTS[4]] * 4, updated=now - timedelta(minutes=1)), now)
    assert plan["segments"] == [["2026-10-25T12:30:00+00:00", "2026-10-25T13:00:00+00:00", "normal"]]


def test_missing_grid_series_count_as_zero_and_soc_is_optional():
    data = evopt(slots=[SLOTS[1]])
    del data["res"]["grid_import"], data["res"]["grid_export"]
    del data["res"]["batteries"][1]["state_of_charge"]
    assert derive_plan(data, T0)["current_action"] == "normal"


def test_segments_are_capped():
    slots = [SLOTS[1], SLOTS[2]] * 60
    plan = derive_plan(evopt(slots=slots), T0)
    assert len(plan["segments"]) == 96 and plan["segments_truncated"] is True


@pytest.mark.parametrize("change,status", [
    (lambda d: None, "no_evopt"),
    (lambda d: [d], "invalid"),
    (lambda d: {**d, "updated": "yesterday"}, "invalid"),
    (lambda d: {**d, "updated": "2026-10-25T12:00:00"}, "invalid"),
    (lambda d: {**d, "updated": 5}, "invalid"),
    (lambda d: {**d, "res": {**d["res"], "status": "Infeasible"}}, "not_solved"),
    (lambda d: {**d, "res": None}, "not_solved"),
    (lambda d: {**d, "details": {**d["details"], "timestamp": d["details"]["timestamp"][:2]}}, "invalid"),
    (lambda d: {**d, "details": {**d["details"], "timestamp": ["x"] * 7}}, "invalid"),
    (lambda d: {**d, "req": {**d["req"], "time_series": {"dt": [0] * 7}}}, "invalid"),
    (lambda d: {**d, "req": {**d["req"], "batteries": []}}, "invalid"),
    (lambda d: {**d, "details": {**d["details"], "batteryDetails": [{"type": "loadpoint"}, {"type": "vehicle"}]}},
     "no_home_battery"),
    (lambda d: {**d, "res": {**d["res"], "batteries": [{}, None]}}, "invalid"),
    (lambda d: {**d, "res": {**d["res"], "batteries": [{}, {"charging_power": [1], "discharging_power": [0] * 7}]}},
     "invalid"),
    (lambda d: {**d, "res": {**d["res"], "grid_import": ["x"] * 7}}, "invalid"),
    (lambda d: {**d, "res": {**d["res"], "batteries": [{}, {**d["res"]["batteries"][1], "state_of_charge": "x"}]}},
     "invalid"),
    (lambda d: {**d, "req": {**d["req"], "batteries": [{}, {"s_min": None, "s_max": 1}]}}, "invalid"),
])
def test_unusable_results_never_produce_a_plan(change, status):
    plan = derive_plan(change(evopt()), T0)
    assert plan["status"] == status
    assert "current_action" not in plan


def test_old_and_future_plans_are_rejected():
    assert derive_plan(evopt(updated=T0 - timedelta(minutes=31)), T0)["status"] == "stale"
    later = derive_plan(evopt(start=datetime(2026, 10, 25, 12, 15, tzinfo=UTC)), T0)
    assert later["status"] == "no_current_slot"
    ended = T0 + timedelta(hours=3)
    assert derive_plan(evopt(updated=ended), ended)["status"] == "no_current_slot"


def test_classify_uses_the_ladder_vocabulary():
    assert classify("normal", "Akku Dynamisch") == "agree"
    assert classify("charge", "Akku Dynamisch") == "evcc_grid_charge"
    assert classify("normal", "Akku Netzladen") == "ladder_grid_charge"
    assert classify("hold", "Akku Dynamisch") == "evcc_hold"
    assert classify("holdcharge", "Akku nur Laden") == "evcc_discharge"
    assert classify("holdcharge", "Akku Dynamisch") == "evcc_no_charge"
    assert classify("discharge", "Akku Dynamisch") == "other"
    assert classify("normal", "manuell") == "other"


@pytest.mark.parametrize("attributes,state,reported,status", [
    (None, None, T0, "entity_missing"),
    ({}, "unavailable", T0, "unavailable"),
    ({"suggestion": {"action": "hold"}}, "84", T0 - timedelta(minutes=16), "stale"),
    ({"suggestion": {"action": "hold"}}, "84", None, "stale"),
    ({}, "84", T0, "no_suggestion"),
    ({"suggestion": {"action": "boost"}}, "84", T0, "invalid_suggestion"),
    ({"suggestion": "hold"}, "84", T0, "invalid_suggestion"),
])
def test_suggestion_states(attributes, state, reported, status):
    assert suggestion_status(attributes, state, reported, T0) == {"status": status}


def test_suggestion_ready():
    result = suggestion_status({"suggestion": {"action": "hold", "charge": 0}}, "84", T0, T0)
    assert result == {"status": "ready", "action": "hold"}


def test_check_prefers_the_suggestion_and_reports_the_derivation():
    plan = derive_plan(evopt(), T0)
    check = evcc_check({"status": "ready", "action": "hold"}, plan, "Akku Dynamisch")
    assert check["status"] == "ready" and check["source"] == "suggestion"
    assert check["verdict"] == "evcc_hold" and check["evcc_mode"] == "Akku nur Laden"
    assert check["derivation_matches"] is False
    assert check["informational_only"] is True and check["controls_battery"] is False and check["note"]
    fallback = evcc_check({"status": "stale"}, plan, "Akku Netzladen")
    assert fallback["source"] == "evopt" and fallback["verdict"] == "agree"
    assert fallback["derivation_matches"] is None
    only = evcc_check({"status": "ready", "action": "discharge"}, {"status": "not_configured"}, "Akku Dynamisch")
    assert only["evcc_mode"] is None and only["verdict"] == "other"
    assert evcc_check({"status": "stale"}, {"status": "no_evopt"}, "Akku Dynamisch")["status"] == "no_evcc_data"


def ready(verdict="agree", source="suggestion", matches=None):
    return {"status": "ready", "verdict": verdict, "source": source, "derivation_matches": matches}


def test_totals_hold_the_previous_verdict_and_skip_gaps():
    comparison = EvccComparison()
    comparison.observe(T0, ready("evcc_hold", matches=True))
    comparison.observe(T0 + timedelta(seconds=30), ready("agree", "evopt", matches=False))
    totals = comparison.observe(T0 + timedelta(seconds=60), {"status": "no_evcc_data"})
    assert totals["ready_seconds"] == 60
    assert totals["evcc_hold_seconds"] == 30 and totals["agree_seconds"] == 30
    assert totals["suggestion_seconds"] == 30 and totals["evopt_seconds"] == 30
    assert totals["derivation_checked_seconds"] == 60 and totals["derivation_mismatch_seconds"] == 30
    assert totals["disagreement_percent"] == 50
    totals = comparison.observe(T0 + timedelta(seconds=90), ready())
    assert totals["ready_seconds"] == 60
    # Over 90 s and after an interrupt nothing counts.
    totals = comparison.observe(T0 + timedelta(seconds=300), ready())
    assert totals["ready_seconds"] == 60
    comparison.interrupt()
    totals = comparison.observe(T0 + timedelta(seconds=330), ready())
    assert totals["ready_seconds"] == 60
    assert totals["started_at"] == T0.isoformat()
    assert EvccComparison().observe(T0, ready())["disagreement_percent"] is None


def test_restore_round_trip_and_rejects_inconsistent_counters():
    comparison = EvccComparison()
    comparison.observe(T0, ready())
    comparison.observe(T0 + timedelta(seconds=30), ready())
    saved = comparison.snapshot()
    restored = EvccComparison()
    restored.restore(saved)
    assert restored.snapshot() == saved
    for broken in (
        None,
        {**saved, "version": 2},
        {**saved, "started_at": "2026-10-25T12:00:00"},
        {**saved, "agree_seconds": -1},
        {**saved, "agree_seconds": "x"},
        {**saved, "ready_seconds": 99},
        {**saved, "suggestion_seconds": 0, "evopt_seconds": 0},
        {**saved, "derivation_mismatch_seconds": 5},
        {**saved, "started_at": 5},
    ):
        restored.restore(broken)
        assert restored.snapshot()["ready_seconds"] == 0


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, chunks, error=None):
        self.content, self.error = FakeContent(chunks), error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.error:
            raise self.error


class FakeSession:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


async def test_fetch_reads_only_the_evopt_part_of_the_state():
    session = FakeSession(FakeResponse([b'{"updated":', b' "x"}']))
    assert await fetch_evopt(session, "http://evcc.local:7070/") == b'{"updated": "x"}'
    url, kwargs = session.calls[0]
    assert url == "http://evcc.local:7070/api/state"
    assert kwargs["params"] == {"jq": ".evopt"} and kwargs["timeout"] == 10
    with pytest.raises(RuntimeError):
        await fetch_evopt(FakeSession(FakeResponse([], RuntimeError("503"))), "http://evcc.local")
    chunk = b"x" * 65536
    with pytest.raises(ValueError, match="too large"):
        await fetch_evopt(FakeSession(FakeResponse([chunk] * (MAX_RESPONSE_BYTES // len(chunk) + 1))), "http://evcc.local")


def test_oversized_plans_are_rejected_before_parsing():
    plan = derive_plan(evopt(slots=[SLOTS[4]] * (MAX_SLOTS + 1)), T0)
    assert plan == {"status": "too_many_slots"}
