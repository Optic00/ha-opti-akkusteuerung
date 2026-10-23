"""Observation-only estimate of PV energy above the AC surplus limit."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.clipping import clipping_forecast

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 15, 11, 15, tzinfo=TZ).astimezone(UTC)


def slot(hour, minute, p50, p10=None, p90=None, day=15):
    start = datetime(2026, 7, day, hour, minute, tzinfo=TZ)
    return {"period_start": start.isoformat(), "pv_estimate": p50,
            "pv_estimate10": p50 if p10 is None else p10,
            "pv_estimate90": p50 if p90 is None else p90}


def state(detail, *, value="40", reported=NOW):
    return SimpleNamespace(state=value, attributes={"detailedForecast": detail},
                           last_reported=reported, last_updated=reported)


def run(source, limit=9500, **kwargs):
    return clipping_forecast(source, limit, NOW, TZ, **kwargs)


def test_remaining_energy_counts_only_the_rest_of_today():
    detail = [
        slot(10, 30, 10.5),               # past: counts for the day only
        slot(11, 0, 10.5, 9.0, 11.5),     # current: 15 of 30 minutes remain
        slot(11, 30, 11.5, 9.6, 12.5),
        slot(12, 0, 9.0),                 # below the limit
        slot(11, 0, 20.0, day=16),        # tomorrow is ignored
    ]
    value, attrs = run(state(detail), soc=50, capacity_kwh=10, maxsoc=90)
    # P50: 1000 W * 0.25 h + 2000 W * 0.5 h
    assert value == 1.25
    assert attrs["status"] == "ready"
    assert attrs["remaining_p10_kwh"] == 0.05
    assert attrs["remaining_p90_kwh"] == 2.0
    assert attrs["day_p50_kwh"] == 2.0
    assert attrs["first_clipping_start"] == datetime(2026, 7, 15, 10, 30, tzinfo=TZ).astimezone(UTC).isoformat()
    assert attrs["last_clipping_end"] == datetime(2026, 7, 15, 12, 0, tzinfo=TZ).astimezone(UTC).isoformat()
    assert attrs["free_capacity_kwh"] == 4.0
    assert attrs["reserve_shortfall_kwh"] == 0.0
    assert attrs["forecast_capped_suspected"] is False
    assert attrs["observation_only"] is True and attrs["controls_battery"] is False


def test_shortfall_when_free_capacity_is_too_small():
    value, attrs = run(state([slot(12, 0, 12.5)]), soc=85, capacity_kwh=10, maxsoc=90)
    assert value == 1.5
    assert attrs["free_capacity_kwh"] == 0.5
    assert attrs["reserve_shortfall_kwh"] == 1.0


@pytest.mark.parametrize("soc,capacity,maxsoc", [(None, 10, 90), (50, 0, 90), (120, 10, 90), (50, 10, 0)])
def test_invalid_battery_values_leave_capacity_unknown(soc, capacity, maxsoc):
    value, attrs = run(state([slot(12, 0, 12.5)]), soc=soc, capacity_kwh=capacity, maxsoc=maxsoc)
    assert value == 1.5
    assert attrs["free_capacity_kwh"] is None and attrs["reserve_shortfall_kwh"] is None


def test_plateau_marks_a_possibly_capped_forecast():
    detail = [slot(11, 0, 9.98), slot(11, 30, 10.0), slot(12, 0, 10.0), slot(12, 30, 7.0)]
    _, attrs = run(state(detail))
    assert attrs["forecast_peak_p90_w"] == 10000
    assert attrs["forecast_capped_suspected"] is True


@pytest.mark.parametrize("limit", [0, -1, None, "x", True])
def test_without_limit_the_estimate_is_off(limit):
    assert run(state([slot(12, 0, 12.5)]), limit) == ("unavailable", {
        "observation_only": True, "controls_battery": False, "status": "not_configured"})


@pytest.mark.parametrize("source,status", [
    (None, "no_forecast"),
    (state([slot(12, 0, 12.5)], value="unavailable"), "no_forecast"),
    (state(None), "no_detail"),
    (state([]), "no_detail"),
    (state([slot(12, 0, 12.5)], reported=NOW - timedelta(hours=13)), "stale"),
    (state([slot(12, 0, 12.5)], reported=None), "invalid"),
    (state([{"period_start": "2026-07-15T12:00:00", "pv_estimate": 1, "pv_estimate10": 1, "pv_estimate90": 1}]), "invalid"),
    (state([{**slot(12, 0, 12.5), "pv_estimate90": None}]), "invalid"),
    (state([slot(12, 0, 12.5), slot(12, 0, 11.0)]), "invalid"),
    (state([slot(12, 0, 12.5), slot(12, 15, 11.0)]), "invalid"),
    (state([slot(12, 0, 12.5, day=16)]), "no_today_slots"),
])
def test_unusable_forecasts_are_reported_not_guessed(source, status):
    value, attrs = run(source)
    assert value == "unavailable"
    assert attrs["status"] == status


def test_duplicate_identical_rows_are_accepted():
    value, _ = run(state([slot(12, 0, 12.5), slot(12, 0, 12.5)]))
    assert value == 1.5


def test_default_age_limit_is_twelve_hours_and_the_option_overrides_it():
    aged = state([slot(12, 0, 12.5)], reported=NOW - timedelta(seconds=30000))
    assert run(aged)[0] == 1.5
    assert run(aged, max_age=21600) == ("unavailable", {"observation_only": True, "controls_battery": False,
                                                        "limit_w": 9500, "status": "stale"})


def test_zero_max_age_accepts_old_forecasts():
    value, _ = run(state([slot(12, 0, 12.5)], reported=NOW - timedelta(days=2)), max_age=0)
    assert value == 1.5
