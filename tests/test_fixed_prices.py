"""#204: fixed daily price windows as a dated price source."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.opti_akku.fixed_prices import (
    FixedPriceError,
    fixed_price_plan,
    format_windows,
    normalized_windows,
    parse_windows,
)
from custom_components.opti_akku.sources import build_inputs

BERLIN = ZoneInfo("Europe/Berlin")
NIGHT = [{"start": "00:00", "end": "05:00", "price": 21.74},
         {"start": "05:00", "end": "00:00", "price": 31.74}]


@pytest.fixture(autouse=True)
def berlin_time(hass):
    dt_util.set_default_time_zone(BERLIN)
    yield
    dt_util.set_default_time_zone(UTC)


def at(*args):
    return datetime(*args, tzinfo=BERLIN).astimezone(UTC)


def test_night_window_builds_hourly_today_and_tomorrow():
    current, series, period = fixed_price_plan(NIGHT, at(2026, 11, 11, 12, 30))
    assert current == 31.74
    assert series["today"] == [21.74] * 5 + [31.74] * 19
    assert series["tomorrow"] == series["today"]
    assert period == {"start": "2026-11-11T11:00:00+00:00", "end": "2026-11-11T12:00:00+00:00"}


@pytest.mark.parametrize(("minute", "expected"), [((4, 59), 21.74), ((5, 0), 31.74), ((0, 0), 21.74), ((23, 59), 31.74)])
def test_current_price_comes_from_the_same_windows(minute, expected):
    current, series, _period = fixed_price_plan(NIGHT, at(2026, 11, 11, *minute))
    assert current == expected
    assert len(series["today"]) == 24 and len(series["tomorrow"]) == 24


def test_window_across_midnight_and_order_independence():
    windows = [{"start": "06:00", "end": "22:00", "price": 30},
               {"start": "22:00", "end": "06:00", "price": 20}]
    current, series, _ = fixed_price_plan(windows, at(2026, 11, 11, 23, 0))
    assert current == 20
    assert series["today"] == [20] * 6 + [30] * 16 + [20] * 2
    assert fixed_price_plan(list(reversed(windows)), at(2026, 11, 11, 23, 0))[1] == series


def test_single_window_covers_the_whole_day():
    current, series, _ = fixed_price_plan([{"start": "00:00", "end": "00:00", "price": 25}], at(2026, 11, 11, 3))
    assert current == 25 and series["today"] == [25] * 24


def test_quarter_hour_boundaries_use_quarter_hour_slots():
    windows = [{"start": "00:30", "end": "05:15", "price": 10},
               {"start": "05:15", "end": "00:30", "price": 30}]
    current, series, period = fixed_price_plan(windows, at(2026, 11, 11, 5, 10))
    assert current == 10
    assert series["today"] == [30] * 2 + [10] * 19 + [30] * 75
    assert period == {"start": "2026-11-11T04:00:00+00:00", "end": "2026-11-11T04:15:00+00:00"}


def test_spring_forward_day_has_23_hours():
    # 2026-03-29: 02:00 local does not exist.
    _current, series, _ = fixed_price_plan(NIGHT, at(2026, 3, 29, 12))
    assert series["today"] == [21.74] * 4 + [31.74] * 19
    assert len(series["tomorrow"]) == 24
    # The day before: tomorrow is the short day.
    assert len(fixed_price_plan(NIGHT, at(2026, 3, 28, 12))[1]["tomorrow"]) == 23


def test_fall_back_day_has_25_hours_and_both_two_am_hours_are_cheap():
    # 2026-10-25: 02:00 local happens twice.
    _current, series, _ = fixed_price_plan(NIGHT, at(2026, 10, 25, 12))
    assert series["today"] == [21.74] * 6 + [31.74] * 19
    second_two_am = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 CET
    assert fixed_price_plan(NIGHT, second_two_am)[0] == 21.74


def test_fall_back_day_quarter_hours_has_100_slots():
    windows = [{"start": "00:00", "end": "05:15", "price": 10},
               {"start": "05:15", "end": "00:00", "price": 30}]
    _current, series, _ = fixed_price_plan(windows, at(2026, 10, 25, 12))
    assert len(series["today"]) == 100 and series["today"].count(10) == 25
    assert len(fixed_price_plan(windows, at(2026, 3, 29, 12))[1]["today"]) == 92


@pytest.mark.parametrize(("windows", "code"), [
    ([{"start": "00:00", "end": "05:00", "price": 20}, {"start": "06:00", "end": "00:00", "price": 30}], "price_windows_gap"),
    ([{"start": "00:00", "end": "06:00", "price": 20}, {"start": "05:00", "end": "00:00", "price": 30}], "price_windows_overlap"),
    # 24 h in total, but one hour twice and another not at all.
    ([{"start": "00:00", "end": "05:00", "price": 20}, {"start": "04:00", "end": "12:00", "price": 25},
      {"start": "13:00", "end": "00:00", "price": 30}], "price_windows_overlap"),
    ([{"start": "00:00", "end": "00:00", "price": 20}, {"start": "05:00", "end": "06:00", "price": 30}], "price_windows_overlap"),
    ([{"start": "22:00", "end": "06:00", "price": 20}, {"start": "06:00", "end": "21:00", "price": 30}], "price_windows_gap"),
    ([{"start": "00:10", "end": "00:10", "price": 20}], "price_windows_grid"),
    ([{"start": "00:00:30", "end": "00:00", "price": 20}], "price_windows_invalid"),
    ([{"start": "24:00", "end": "00:00", "price": 20}], "price_windows_invalid"),
    ([{"start": "0:00", "end": "00:00", "price": 20}], "price_windows_invalid"),
    ([{"start": "00:00", "end": "00:00", "price": "nan"}], "price_windows_invalid"),
    ([{"start": "00:00", "end": "00:00", "price": True}], "price_windows_invalid"),
    ([{"start": "00:00", "end": "00:00", "price": 5000}], "price_windows_invalid"),
    ([{"start": 0, "end": "00:00", "price": 20}], "price_windows_invalid"),
    ([], "price_windows_invalid"),
    (None, "price_windows_invalid"),
    (["00:00-00:00"], "price_windows_invalid"),
])
def test_invalid_windows_are_rejected(windows, code):
    with pytest.raises(FixedPriceError) as err:
        parse_windows(windows)
    assert err.value.code == code


def test_time_selector_seconds_are_normalized_and_negative_prices_allowed():
    raw = [{"start": "05:00:00", "end": "00:00:00", "price": "-1.5"},
           {"start": "00:00:00", "end": "05:00:00", "price": 21.74}]
    assert normalized_windows(raw) == [{"start": "00:00", "end": "05:00", "price": 21.74},
                                       {"start": "05:00", "end": "00:00", "price": -1.5}]
    assert format_windows(raw) == "00:00–05:00 21.74 ct/kWh, 05:00–00:00 -1.5 ct/kWh"
    assert format_windows([]) == "—"


def test_build_inputs_uses_windows_without_any_age_check():
    """No entity, a tiny price_max_age and no provider data still produce no source error."""
    options = {"sources": {}, "price_provider": "fixed_windows", "price_windows": NIGHT, "price_max_age": 1}
    states, attributes, errors = build_inputs({}, options, {}, at(2026, 11, 11, 3, 15))
    assert errors == {}
    assert states["sensor.opti_price_current_ct_kwh"] == 21.74
    assert states["sensor.opti_price_series"] == 21.74
    assert attributes["sensor.opti_price_series"]["today"][:6] == [21.74] * 5 + [31.74]
    assert attributes["sensor.opti_price_current_ct_kwh"]["end"] == "2026-11-11T03:00:00+00:00"


def test_build_inputs_ignores_configured_price_entities_for_fixed_windows():
    options = {"sources": {"price_current": "sensor.old", "price_series": "sensor.old"},
               "price_provider": "fixed_windows", "price_windows": NIGHT}
    states, _attributes, errors = build_inputs({}, options, {}, at(2026, 11, 11, 12))
    assert errors == {} and states["sensor.opti_price_current_ct_kwh"] == 31.74


def test_build_inputs_reports_invalid_stored_windows_as_price_errors():
    options = {"sources": {}, "price_provider": "fixed_windows", "price_windows": NIGHT[:1]}
    states, attributes, errors = build_inputs({}, options, {}, at(2026, 11, 11, 12))
    assert errors == {"price_current": "price_windows_gap", "price_series": "price_windows_gap"}
    assert states["sensor.opti_price_current_ct_kwh"] == "unavailable"
    assert "sensor.opti_price_series" not in attributes


def test_interval_crossing_changes_the_reported_period():
    """The pre-write recheck compares this period, as for Tibber."""
    before = fixed_price_plan(NIGHT, at(2026, 11, 11, 4, 59))[2]
    after = fixed_price_plan(NIGHT, at(2026, 11, 11, 4, 59) + timedelta(minutes=2))[2]
    assert before != after


def test_half_hour_dst_shift_falls_back_to_quarter_hours():
    """Lord Howe Island moves its clock by 30 minutes; hourly slots would misalign."""
    dt_util.set_default_time_zone(ZoneInfo("Australia/Lord_Howe"))
    noon = datetime(2026, 4, 5, 12, tzinfo=ZoneInfo("Australia/Lord_Howe")).astimezone(UTC)
    _current, series, _ = fixed_price_plan(NIGHT, noon)
    assert len(series["today"]) == 98  # 24.5 h
    assert series["today"][:22] == [21.74] * 22 and series["today"][22] == 31.74  # 00:00-05:00 is 5.5 h
    assert len(series["tomorrow"]) == 96
    regular = datetime(2026, 4, 7, 12, tzinfo=ZoneInfo("Australia/Lord_Howe")).astimezone(UTC)
    assert len(fixed_price_plan(NIGHT, regular)[1]["today"]) == 24
