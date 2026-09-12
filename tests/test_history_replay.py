"""Synthetic checks for causal history replay; never load a real HA export."""
import datetime as dt

import pytest

from tools.replay_forecast_history import ForecastVersion, replay_window, timestamp
from .ha_harness import REPO, TZ, load_yaml


CONFIG = load_yaml(REPO / "packages/opti_derived.yaml")


def test_history_replay_holds_both_horizon_states_at_score_two():
    version = ForecastVersion(CONFIG)
    now = dt.datetime(2026, 1, 15, 2, tzinfo=TZ)
    states = {"sensor.opti_house_consumption_w": "1000",
              "sensor.opti_house_consumption_60min_w": "1000"}
    attrs = {"sun.sun": {"next_rising": "2026-01-15T08:00:00+01:00"}}
    # 24 kWh/day consumption: 7.2, 4.8, 2.4 kWh give scores 3, 2, 1.
    results = []
    for energy in ["7.2", "4.8", "2.4", "4.8"]:
        states["sensor.opti_forecast_today_kwh"] = energy
        results.append(version.evaluate(now, states, attrs)["horizon_long"])
    assert results == ["off", "off", "on", "on"]


def _window(*, truncated=False, minimal=False):
    return {"label": "synthetic", "data": {
        "success": True,
        "query_params": {"minimal_response": minimal, "significant_changes_only": False},
        "period": {"start": "2026-01-15T01:59:30+01:00",
                   "end": "2026-01-15T02:00:30+01:00"},
        "entities": [{"entity_id": "sensor.opti_forecast_score_tomorrow",
                      "has_more": truncated, "states": [
                          {"state": "2", "attributes": {}, "last_changed": "2026-01-15T01:59:45+01:00",
                           "last_updated": "2026-01-15T02:00:10+01:00"},
                          {"state": "8", "attributes": {}, "last_changed": "2026-01-15T01:59:30+01:00",
                           "last_updated": "2026-01-15T01:59:30+01:00"}]}]}}


def test_replay_orders_attribute_updates_by_last_updated_without_lookahead():
    # Deliberately unsorted input: the update at 02:00:10 must not leak into
    # the initial state or the inserted 02:00 minute tick.
    config = {"template": [{"sensor": [{"unique_id": "opti_forecast_score",
               "state": "{{ states('sensor.opti_forecast_score_tomorrow') }}"}]}]}
    rows = replay_window(_window(), config, config)["snapshots"]
    assert [row["before"]["score"] for row in rows] == ["8", "8", "2"]
    assert rows[1]["time"] == "2026-01-15T02:00:00+01:00"


@pytest.mark.parametrize("kwargs, message", [
    ({"truncated": True}, "Truncated history"),
    ({"minimal": True}, "full attributes"),
])
def test_incomplete_history_is_not_silently_replayed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        replay_window(_window(**kwargs), CONFIG, CONFIG)


@pytest.mark.parametrize("significant", [True, None])
def test_replay_rejects_filtered_or_unspecified_attribute_history(significant):
    window = _window()
    window["data"]["query_params"]["significant_changes_only"] = significant
    with pytest.raises(ValueError, match="all attribute changes"):
        replay_window(window, CONFIG, CONFIG)


def test_replay_rejects_records_without_attributes():
    window = _window()
    del window["data"]["entities"][0]["states"][1]["attributes"]
    with pytest.raises(ValueError, match="Missing attributes"):
        replay_window(window, CONFIG, CONFIG)


def test_replay_keeps_latest_initial_record_even_for_unsorted_input():
    window = _window()
    window["data"]["period"]["start"] = "2026-01-15T02:00:20+01:00"
    config = {"template": [{"sensor": [{"unique_id": "opti_forecast_score",
               "state": "{{ states('sensor.opti_forecast_score_tomorrow') }}"}]}]}
    rows = replay_window(window, config, config)["snapshots"]
    assert rows[0]["before"]["score"] == "2"


def test_naive_timestamps_are_rejected_and_dst_offsets_preserved():
    with pytest.raises(ValueError, match="timezone"):
        timestamp("2026-10-25T02:30:00")
    first = timestamp("2026-10-25T02:30:00+02:00")
    second = timestamp("2026-10-25T02:30:00+01:00")
    assert second.timestamp() - first.timestamp() == 3600


def test_recorded_sun_event_sensors_replace_omitted_attributes():
    version = ForecastVersion(CONFIG)
    result = version.evaluate(dt.datetime(2026, 1, 15, 2, tzinfo=TZ), {
        "sensor.sun_next_rising": "2026-01-15T07:00:00Z",
        "sensor.sun_next_setting": "2026-01-15T16:00:00Z",
        "sensor.opti_forecast_today_kwh": "12",
        "sensor.opti_house_consumption_w": "1000",
        "sensor.opti_house_consumption_60min_w": "1000",
    }, {})
    assert result["sonnentag"] == "5"
    assert result["score"] == "5"


def test_autumn_repeated_hour_keeps_both_recorded_updates():
    window = _window()
    window["data"]["period"] = {
        "start": "2026-10-25T02:30:00+02:00",
        "end": "2026-10-25T02:30:00+01:00",
    }
    window["data"]["entities"][0]["states"] = [
        {"state": "8", "attributes": {}, "last_changed": "2026-10-25T02:30:00+02:00"},
        {"state": "2", "attributes": {}, "last_changed": "2026-10-25T02:30:00+01:00"},
    ]
    config = {"template": [{"sensor": [{"unique_id": "opti_forecast_score",
               "state": "{{ states('sensor.opti_forecast_score_tomorrow') }}"}]}]}
    rows = replay_window(window, config, config)["snapshots"]
    assert rows[0]["before"]["score"] == "8"
    assert rows[-1]["before"]["score"] == "2"
    assert len(rows) == 61
