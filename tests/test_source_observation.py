"""Synthetic source traces; no HA device or private household data."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.opti_akku.observation import SourceObservation, finite
from custom_components.opti_akku.recovery import RecoveryState

NOW = datetime(2026, 9, 15, tzinfo=UTC)


def sample(value, now=NOW):
    return SimpleNamespace(
        state=str(value),
        last_reported=now,
        last_updated=now,
        last_changed=now,
        attributes={"unit_of_measurement": "W"},
    )


def inputs():
    # Native hybrid AC 400 + second inverter 200 + import 300 - EV100 = house800.
    measurements = {
        "sensor.opti_pv_power_w": 400,
        "sensor.opti_grid_import_w": 300,
        "sensor.opti_grid_export_w": 0,
    }
    states = {
        "sensor.extra": sample(200),
        "sensor.ev": sample(100),
        "sensor.legacy": sample(800),
        "sensor.instant": sample(800),
    }
    options = {
        "sources": {"house_consumption": "sensor.legacy"},
        "source_observation": {
            "enabled": True,
            "meter_confirmed": True,
            "additional_ac_sources": ["sensor.extra"],
            "excluded_load_sources": ["sensor.ev"],
            "instant_house": "sensor.instant",
        },
    }
    return measurements, states, options


def observe(model, at, measurements, states, options, **kwargs):
    return model.update(
        at,
        measurements,
        states,
        options,
        kwargs.get("online", True),
        kwargs.get("errors", {}),
        {"status": "ready"},
        {},
    )


def test_parallel_balance_does_not_change_inputs_and_requires_coverage():
    m, s, o = inputs()
    before = deepcopy((m, o))
    model = SourceObservation()
    result = observe(model, NOW, m, s, o)
    assert result["native_instant_w"] == 800
    assert result["difference_w"] is None
    for seconds in range(30, 3631, 30):
        at = NOW + timedelta(seconds=seconds)
        s = {key: sample(value.state, at) for key, value in s.items()}
        result = observe(model, at, m, s, o)
    assert result["status"] == "ready"
    assert result["difference_w"] == 0
    assert result["comparison_covered_seconds"] == 30
    assert result["controls_battery"] is False
    assert (m, o) == before


def test_stale_upstream_blocks_comparison_not_refreshed_by_observation():
    m, s, o = inputs()
    model = SourceObservation()
    result = observe(model, NOW + timedelta(seconds=901), m, s, o)
    assert result["status"] == "data_missing"
    assert result["native_instant_w"] is None
    assert result["sources"]["sensor.legacy"]["last_reported_age_s"] == 901
    assert s["sensor.legacy"].last_reported == NOW
    assert not model.bins


def test_missing_extra_inverter_is_not_zero_and_offline_not_trusted():
    m, s, o = inputs()
    s.pop("sensor.extra")
    model = SourceObservation()
    assert observe(model, NOW, m, s, o)["native_instant_w"] is None
    m, s, o = inputs()
    assert observe(model, NOW, m, s, o, online=False)["native_instant_w"] is None


def test_not_confirmed_topology_is_diagnostic_only():
    m, s, o = inputs()
    o["source_observation"]["meter_confirmed"] = False
    result = observe(SourceObservation(), NOW, m, s, o)
    assert result["native_instant_w"] is None
    assert "plant_meter_confirmed" in result["balance_errors"]


def test_journal_bounded_no_bridge_over_outage_and_round_trip():
    m, s, o = inputs()
    model = SourceObservation()
    for i in range(400):
        at = NOW + timedelta(minutes=15 * i)
        s = {key: sample(value.state, at) for key, value in s.items()}
        observe(model, at, m, s, o)
    assert not model.bins  # Every gap exceeds90s; no artificial coverage.
    assert len(model.events) <= 256
    assert len(model.events) >= 190
    restored = SourceObservation()
    restored.restore(model.snapshot())
    assert restored.snapshot() == model.snapshot()
    assert restored.previous is None
    observe(
        restored,
        at + timedelta(seconds=20),
        m,
        s,
        o,
        errors={"house_consumption": "missing_or_stale"},
    )
    assert restored.events[-1]["source_errors"]


def test_recovery_requires_two_good_reads_after_outage_and_bad_status():
    r = RecoveryState()
    assert r.update(True, 235, 0)["write_ready"]
    assert not r.update(False, None, 10)["write_ready"]
    assert r.update(True, 16777213, 20)["status"] == "not_ready"
    assert r.update(True, 235, 30)["status"] == "recovering"
    assert r.update(True, 235, 40)["write_ready"]
    assert not r.update(True, 0, 50)["write_ready"]
    assert not r.update(True, 235, 60)["write_ready"]
    assert r.update(True, 2119, 70)["write_ready"]


def test_recovery_uses_configured_statuses_and_rollback_resets_gate():
    r = RecoveryState()
    assert not r.update(True, 1463, 1)["write_ready"]
    assert not r.update(True, 1463, 2, (1463,))["write_ready"]
    assert r.update(True, 1463, 3, (1463,))["write_ready"]
    assert not r.update(True, 1463, 1, (1463,))["write_ready"]


def test_stale_ev_blocks_net_but_not_independent_gross_comparison():
    m, s, o = inputs()
    o["source_observation"]["gross_house"] = "sensor.gross"
    at = NOW + timedelta(seconds=901)
    s["sensor.gross"] = sample(900, at)
    s["sensor.extra"] = sample(200, at)
    result = observe(SourceObservation(), at, m, s, o)
    assert result["native_instant_w"] is None
    assert result["gross_native_w"] == 900
    assert result["gross_difference_w"] == 0
    assert result["status"] == "data_missing"


def test_selected_event_based_stale_zero_is_valid_for_net_and_gross_comparison():
    m, s, o = inputs()
    at = NOW + timedelta(seconds=901)
    o["event_based_excluded_sources"] = ["sensor.ev"]
    o["source_observation"]["gross_house"] = "sensor.gross"
    s = {
        "sensor.extra": sample(200, at),
        "sensor.ev": sample(0),
        "sensor.legacy": sample(900, at),
        "sensor.instant": sample(900, at),
        "sensor.gross": sample(900, at),
    }
    result = observe(SourceObservation(), at, m, s, o)
    assert result["native_instant_w"] == 900
    assert result["gross_native_w"] == 900
    assert result["gross_difference_w"] == 0
    assert not result["balance_errors"]


def test_event_based_exception_does_not_relax_other_stale_exclusions():
    m, s, o = inputs()
    at = NOW + timedelta(seconds=901)
    o["event_based_excluded_sources"] = ["sensor.other"]
    s["sensor.extra"] = sample(200, at)
    for value in (0, 1, "unavailable"):
        s["sensor.ev"] = sample(value)
        result = observe(SourceObservation(), at, m, s, o)
        assert result["native_instant_w"] is None
        assert result["balance_errors"]["sensor.ev"] == "missing_or_stale"


def test_observation_binding_tracks_only_applied_event_based_selection():
    m, s, o = inputs()
    model = SourceObservation()
    observe(model, NOW, m, s, o)
    original = model.binding
    o["event_based_excluded_sources"] = ["sensor.other"]
    observe(model, NOW + timedelta(seconds=30), m, s, o)
    assert model.binding == original
    o["event_based_excluded_sources"] = ["sensor.ev"]
    observe(model, NOW + timedelta(seconds=60), m, s, o)
    assert model.binding != original


def test_gross_reference_kw_is_normalized():
    m, s, o = inputs()
    o["source_observation"]["gross_house"] = "sensor.gross"
    s["sensor.gross"] = sample(0.9)
    s["sensor.gross"].attributes["unit_of_measurement"] = "kW"
    result = observe(SourceObservation(), NOW, m, s, o)
    assert result["gross_difference_w"] == 0


def test_disabling_observation_clears_private_buffer():
    m, s, o = inputs()
    model = SourceObservation()
    observe(model, NOW, m, s, o)
    assert model.events
    o["source_observation"]["enabled"] = False
    result = observe(model, NOW, m, s, o)
    assert result["status"] == "disabled"
    assert not model.events and not model.bins


def test_restore_rejects_corrupt_optional_gross_fields():
    for bad in ("invalid", -1, float("inf"), 301):
        model = SourceObservation()
        model.restore(
            {
                "version": 1,
                "binding": "test",
                "events": [],
                "bins": {
                    "0": {
                        "seconds": 0,
                        "native_ws": 0,
                        "legacy_ws": 0,
                        "absolute_difference_ws": 0,
                        "gross_seconds": bad,
                    }
                },
            }
        )
        assert not model.bins
        assert model.binding is None


def test_clock_rollback_resets_observation_coverage():
    m, s, o = inputs()
    o["source_observation"]["gross_house"] = "sensor.gross"
    s["sensor.gross"] = sample(900)
    model = SourceObservation()
    observe(model, NOW, m, s, o)
    observe(model, NOW + timedelta(seconds=20), m, s, o)
    assert model.bins
    result = observe(model, NOW + timedelta(seconds=10), m, s, o)
    assert result["gross_comparison_covered_seconds"] == 0
    assert not model.bins


def test_restore_rejects_untrusted_observation_shapes_and_values():
    valid_row = {
        "seconds": 0,
        "native_ws": 0,
        "legacy_ws": 0,
        "absolute_difference_ws": 0,
    }
    invalid = [
        None,
        {"version": 1, "binding": "test", "bins": [], "events": []},
        {"version": 1, "binding": "test", "bins": {"bad": valid_row}, "events": []},
        {
            "version": 1,
            "binding": "test",
            "bins": {"0": {**valid_row, "seconds": -1}},
            "events": [],
        },
        {"version": 1, "binding": None, "bins": {}, "events": []},
        {"version": 1, "binding": object(), "bins": {}, "events": []},
        {
            "version": 1,
            "binding": "test",
            "bins": {},
            "events": [{"at": "now", "padding": "x" * 2_000_000}],
        },
    ]
    for saved in invalid:
        model = SourceObservation()
        model.restore(saved)
        assert model.snapshot() == {"version": 1, "binding": None, "bins": {}, "events": []}


def test_non_numeric_observation_value_is_missing():
    assert finite(object()) is None
