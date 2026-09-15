"""Synthetic data only; imported priors never assert measured coverage."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest

from custom_components.opti_akku.demand_history import (
    HistoricalPrior,
    build_rows,
    flag_for_hour,
    numeric,
    read_recorder,
    stamp,
)
from custom_components.opti_akku.demand import DemandForecast
from tests.test_demand import fixture, update, NOW, state


def historical_rows(now=NOW, watts=500):
    return {
        (now - timedelta(days=d)).replace(minute=0).isoformat(): {
            "house_w": watts,
            "summer_mode": "on",
            "outdoor_temperature": 15.0,
        }
        for d in range(1, 30)
    }


def test_duplicate_replace_and_restore():
    prior = HistoricalPrior()
    rows = historical_rows()
    prior.replace(rows, "source", NOW)
    before = deepcopy(prior.snapshot())
    prior.replace(rows, "source", NOW)
    assert prior.snapshot() == before
    restored = HistoricalPrior()
    restored.restore(before)
    assert restored.snapshot() == before
    assert restored.summary()["measured_coverage"] == "unknown"
    with pytest.raises(ValueError):
        prior.replace({"broken": {}}, "source", NOW)
    assert prior.snapshot() == before


def test_unknown_initial_flag_and_unavailable_transition():
    changes = [(NOW + timedelta(minutes=20), "on")]
    assert flag_for_hour(changes, NOW) is None
    changes = [(NOW - timedelta(hours=1), "on"), (NOW + timedelta(minutes=20), "unavailable")]
    assert flag_for_hour(changes, NOW) is None
    assert flag_for_hour([(NOW, "off")], NOW) == "off"


def test_history_parsers_reject_boolean_non_numeric_and_naive_time():
    assert numeric(True) is None
    assert numeric(object()) is None
    with pytest.raises(ValueError, match="Timezone required"):
        stamp("2026-09-14T06:00:00")


def test_flag_after_hour_does_not_change_known_hour_state():
    changes = [(NOW - timedelta(minutes=1), "on"), (NOW + timedelta(hours=1), "off")]
    assert flag_for_hour(changes, NOW) == "on"


def test_missing_invalid_statistics_not_zero_or_filled():
    values = [None, float("nan"), -1, 0, 500]
    rows = build_rows(
        {
            "house": [
                {"start": (NOW + timedelta(hours=i)).timestamp(), "mean": v}
                for i, v in enumerate(values)
            ]
        },
        {},
        NOW,
        NOW + timedelta(days=1),
    )
    assert len(rows) == 2
    assert [r["house_w"] for r in rows.values()] == [0, 500]
    assert all(r["summer_mode"] is None for r in rows.values())
    assert all("seconds" not in r for r in rows.values())


def test_duplicate_hour_rejected():
    row = {"start": NOW.timestamp(), "mean": 500}
    with pytest.raises(ValueError):
        build_rows({"house": [row, row]}, {}, NOW, NOW + timedelta(days=1))


def test_misaligned_statistics_are_ignored_and_history_window_is_bounded():
    assert (
        build_rows(
            {"house": [{"start": (NOW + timedelta(minutes=30)).timestamp(), "mean": 500}]},
            {},
            NOW,
            NOW + timedelta(hours=1),
        )
        == {}
    )
    start = NOW - timedelta(hours=1011)
    rows = [
        {"start": (start + timedelta(hours=index)).timestamp(), "mean": 500}
        for index in range(1011)
    ]
    with pytest.raises(ValueError, match="History window exceeded"):
        build_rows({"house": rows}, {}, start, NOW)


@pytest.mark.parametrize(
    "saved",
    [
        None,
        {"version": 1, "rows": [], "binding": "source"},
        {
            "version": 1,
            "rows": {(NOW + timedelta(minutes=1)).isoformat(): {"house_w": 500}},
            "binding": "source",
        },
        {
            "version": 1,
            "rows": {NOW.isoformat(): {"house_w": True}},
            "binding": "source",
        },
        {
            "version": 1,
            "rows": {
                NOW.isoformat(): {"house_w": 500, "outdoor_temperature": 121}
            },
            "binding": "source",
        },
        {
            "version": 1,
            "rows": {NOW.isoformat(): {"house_w": 500, "summer_mode": "maybe"}},
            "binding": "source",
        },
        {
            "version": 1,
            "rows": {NOW.isoformat(): {"house_w": 500}},
            "binding": None,
        },
    ],
)
def test_restore_rejects_untrusted_history(saved):
    prior = HistoricalPrior()
    prior.restore(saved)
    assert prior.rows == {}
    assert prior.binding is None


def test_no_future_training_or_online_overlap():
    prior = HistoricalPrior()
    rows = historical_rows()
    rows[NOW.isoformat()] = {"house_w": 50000}
    rows[(NOW + timedelta(days=1)).isoformat()] = {"house_w": 50000}
    prior.replace(rows, "s", NOW)
    result = prior.expected(NOW, NOW, UTC, "summer", {})
    assert result["house_w"] == 500
    assert (
        prior.expected(NOW, NOW, UTC, "summer", {k[:10] + "|6|summer": [1, 0, 0, 1] for k in rows})
        is None
    )
    assert prior.expected(NOW, NOW, UTC, "winter", {}) is None


def test_unknown_season_usable_without_assigning_current_season():
    prior = HistoricalPrior()
    rows = historical_rows()
    for r in rows.values():
        r["summer_mode"] = None
    prior.replace(rows, "s", NOW)
    assert prior.expected(NOW, NOW, UTC, "winter", {})["house_w"] == 500
    assert prior.summary()["known_season_hours"] == 0


def test_temperature_matching_requires_three_days_and_no_future_temperature():
    prior = HistoricalPrior()
    rows = historical_rows()
    for i, r in enumerate(rows.values()):
        r["outdoor_temperature"] = 15 if i % 2 else 25
        r["house_w"] = 1000 if i % 2 else 500
    prior.replace(rows, "s", NOW)
    assert prior.expected(NOW, NOW, UTC, "summer", {}, 15)["house_w"] == 1000
    assert (
        prior.expected(NOW, NOW - timedelta(hours=5), UTC, "summer", {}, 15)["match"] == "weekday"
    )


@pytest.mark.parametrize("day,count", [("2026-03-29", 23), ("2026-10-25", 25)])
def test_dst_hours_not_fabricated_or_double_counted(day, count):
    tz = ZoneInfo("Europe/Berlin")
    local = datetime.fromisoformat(day).replace(tzinfo=tz)
    start = local.astimezone(UTC)
    end = (local + timedelta(days=1)).astimezone(UTC)
    stats = [{"start": (start + timedelta(hours=i)).timestamp(), "mean": 500} for i in range(count)]
    rows = build_rows({"house": stats}, {}, start, end)
    assert len(rows) == count
    prior = HistoricalPrior()
    prior.replace(rows, "s", end)
    # One day remains one example even when it has two local 02:00 hours.
    target = (local + timedelta(days=7)).replace(hour=2).astimezone(UTC)
    assert prior.expected(target, target, tz, "unknown", {}) is None


def test_context_enrichment_preserves_online_history_and_accuracy():
    data, options, states = fixture()
    model = DemandForecast()
    update(model, data, options, states)
    update(model, data, options, states, NOW + timedelta(seconds=30))
    cells = deepcopy(model.cells)
    accuracy = model.accuracy
    options["demand_forecast"]["sources"]["outdoor_temperature"] = "sensor.outdoor"
    states["sensor.outdoor"] = state(15, "°C")
    update(model, data, options, states, NOW + timedelta(seconds=30))
    assert model.cells == cells
    assert model.accuracy is accuracy


def test_water_context_does_not_require_meter_or_trigger_due():
    data, options, states = fixture()
    options["demand_forecast"]["sources"].pop("heat_power")
    options["demand_forecast"]["sources"].update(
        context_water_temperature="sensor.water", context_water_target="sensor.target"
    )
    states.update({"sensor.water": state(20, "°C"), "sensor.target": state(55, "°C")})
    result = update(DemandForecast(), data, options, states)
    assert result["status"] == "learning"
    assert not result["dhw_due"]
    assert result["temperature_context"]["context_water_target"] == 55


def test_prior_populates_prediction_but_never_measured_profile():
    data, options, states = fixture(house=2000)
    options["demand_forecast"]["sources"].pop("heat_power")
    model = DemandForecast()
    update(model, data, options, states)
    binding = model.history_binding("test", options, UTC)
    # update test helper passes fingerprint 'source'.
    import json

    load_fingerprint = json.loads(model.fingerprint)[0]
    binding = model.history_binding(load_fingerprint, options, UTC)
    rows = {}
    for hour in range(24):
        rows.update(historical_rows(NOW.replace(hour=hour), 500))
    accuracy = model.accuracy
    model.history.replace(rows, binding, NOW)
    result = update(model, data, options, states)
    assert result["historical_forecast_slots"] > 0
    assert result["expected_load_kwh"] == 1.0
    assert not result["profile_ready"]
    assert result["status"] == "learning"
    assert result["controls_battery"] is False
    assert model.accuracy is accuracy
    assert not model.cells


def test_recorder_reader_normalizes_and_rejects_wrong_metadata(hass):
    from types import SimpleNamespace

    sources = {
        "house": "sensor.house",
        "outdoor_temperature": "sensor.out",
        "summer_mode": "binary_sensor.summer",
    }
    metadata = {
        "sensor.house": (1, {"unit_of_measurement": "kW"}),
        "sensor.out": (2, {"unit_of_measurement": "°C"}),
    }
    with (
        patch("homeassistant.components.recorder.statistics.get_metadata", return_value=metadata),
        patch(
            "homeassistant.components.recorder.statistics.statistics_during_period",
            return_value={"sensor.house": [{"start": NOW.timestamp(), "mean": 500}]},
        ) as query,
        patch(
            "homeassistant.components.recorder.history.get_significant_states",
            return_value={"binary_sensor.summer": [SimpleNamespace(last_updated=NOW, state="on")]},
        ),
    ):
        rows = read_recorder(hass, sources, NOW, NOW + timedelta(hours=1))
        assert query.call_args.args[5] == {"power": "W", "temperature": "°C"}
        assert next(iter(rows.values()))["summer_mode"] == "on"
        metadata["sensor.house"] = (1, {"unit_of_measurement": "kWh"})
        with pytest.raises(ValueError):
            read_recorder(hass, sources, NOW, NOW + timedelta(hours=1))


def test_imported_prior_keeps_sustained_extra_load():
    data, options, states = fixture(house=2000)
    options["demand_forecast"]["sources"].pop("heat_power")
    model = DemandForecast()
    update(model, data, options, states)
    import json

    rows = {}
    for hour in range(24):
        rows.update(historical_rows(NOW.replace(hour=hour), 500))
    model.history.replace(
        rows, model.history_binding(json.loads(model.fingerprint)[0], options, UTC), NOW
    )
    for seconds in range(30, 1231, 30):
        result = update(model, data, options, states, NOW + timedelta(seconds=seconds))
    assert result["extra_base_load_w"] == 1500
    assert result["forecast_slots"][0]["load_w"] == 2000
    provenance = result["forecast_slots"][0]["load_provenance"]
    assert provenance["source"] == "recorder_statistics"
    assert provenance["days"] >= 2
    assert provenance["unknown_season_days"] == 0
    assert provenance["measured_coverage"] == "unknown"


def test_missing_future_hour_does_not_double_count_recent_uplift():
    import json

    data, options, states = fixture(house=2000, pv_at=3)
    options["demand_forecast"]["sources"].pop("heat_power")
    model = DemandForecast()
    update(model, data, options, states)
    model.history.replace(
        historical_rows(NOW, 500),
        model.history_binding(json.loads(model.fingerprint)[0], options, UTC),
        NOW,
    )
    for seconds in range(30, 1231, 30):
        result = update(model, data, options, states, NOW + timedelta(seconds=seconds))
    assert result["extra_base_load_w"] == 1500
    future_fallback = [
        s
        for s in result["forecast_slots"]
        if s["load_provenance"]["source"] == "fixed_or_recent_fallback"
    ]
    assert future_fallback
    assert all(s["load_w"] == 2000 for s in future_fallback)
