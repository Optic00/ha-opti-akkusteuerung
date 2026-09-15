"""Synthetic source-to-engine regressions for dated, continuous price intervals."""
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.opti_akku.sources import build_inputs

BERLIN = ZoneInfo("Europe/Berlin")
PRICE = "sensor.opti_price_series"
OPTIONS = {"sources": {"price_series": "sensor.prices"}}


def dated_prices(day, *, timezone=UTC, minutes=60, price=.3, field="price"):
    start = datetime.combine(day, time(), tzinfo=timezone).astimezone(UTC)
    stop = datetime.combine(day + timedelta(days=1), time(), tzinfo=timezone).astimezone(UTC)
    result = []
    while start < stop:
        end = start + timedelta(minutes=minutes)
        result.append({"start": start.astimezone(timezone).isoformat(),
                       "end": end.astimezone(timezone).isoformat(), field: price})
        start = end
    return result


def normalize(today, now, tomorrow=None, native=None):
    # Reporting is fresh even when the price intervals belong to yesterday.
    source = SimpleNamespace(state="0.3", last_reported=now,
                             attributes={"unit_of_measurement": "EUR/kWh",
                                         "today": today, "tomorrow": tomorrow})
    return build_inputs(native or {}, OPTIONS, {"sensor.prices": source}, now)


@pytest.fixture(autouse=True)
def utc_time(hass):
    dt_util.set_default_time_zone(UTC)
    yield
    dt_util.set_default_time_zone(UTC)


@pytest.fixture
def berlin_time(utc_time):
    dt_util.set_default_time_zone(BERLIN)


@pytest.mark.parametrize("prices", [[.3] * 24, [{"price": .3}] * 24])
def test_fresh_reporting_cannot_date_numeric_or_undated_dictionary_arrays(prices):
    now = datetime(2026, 9, 13, 0, 1, tzinfo=UTC)
    states, attrs, errors = normalize(prices, now)
    assert states[PRICE] == "unavailable"
    assert PRICE not in attrs
    assert errors == {"price_series": "invalid_price_series"}


def test_previous_day_array_is_not_relabelled_after_local_midnight(berlin_time):
    day = date(2026, 9, 12)
    prices = dated_prices(day, timezone=BERLIN)
    before = datetime(2026, 9, 12, 23, 59, tzinfo=BERLIN)
    assert normalize(prices, before.astimezone(UTC))[2] == {}
    after = before + timedelta(minutes=2)
    states, attrs, errors = normalize(prices, after.astimezone(UTC))
    assert states[PRICE] == "unavailable" and PRICE not in attrs
    assert errors == {"price_series": "invalid_price_series"}


def test_previous_tomorrow_is_usable_at_one_am_without_rotated_bucket_names(berlin_time):
    day = date(2026, 9, 12)
    yesterday = dated_prices(day, timezone=BERLIN, price=.1)
    current_day = dated_prices(day + timedelta(days=1), timezone=BERLIN, price=.4)
    now = datetime(2026, 9, 13, 1, tzinfo=BERLIN).astimezone(UTC)
    states, attrs, errors = normalize(yesterday, now, current_day)
    assert not errors and states[PRICE] == 30
    assert attrs[PRICE] == {"today": [40] * 24, "tomorrow": []}


def test_same_dated_day_in_two_buckets_is_not_silently_merged(berlin_time):
    day = date(2026, 9, 13)
    rows = dated_prices(day, timezone=BERLIN)
    states, attrs, errors = normalize(rows, datetime(2026, 9, 13, 1, tzinfo=BERLIN), rows)
    assert states[PRICE] == "unavailable" and PRICE not in attrs
    assert errors == {"price_series": "invalid_price_series"}


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23),
                                     (date(2026, 9, 12), 24),
                                     (date(2026, 10, 25), 25)])
@pytest.mark.parametrize("minutes", [15, 60])
@pytest.mark.parametrize("field", ["price", "total", "value"])
def test_complete_local_days_preserve_dst_positions_and_price_aliases(
    berlin_time, day, hours, minutes, field
):
    now = datetime.combine(day, time(0, 5), tzinfo=BERLIN).astimezone(UTC)
    prices = dated_prices(day, timezone=BERLIN, minutes=minutes, field=field)
    prices[0][field] = -.1
    original = deepcopy(prices)
    states, attrs, errors = normalize(prices, now)
    assert not errors and states[PRICE] == 30
    assert attrs[PRICE]["today"] == [-10] + [30] * (hours * 60 // minutes - 1)
    assert prices == original


@pytest.mark.parametrize("problem", ["naive", "gap", "overlap", "reversed",
                                    "wrong_order", "missing_end", "missing_slot",
                                    "mixed_resolution", "invalid_price", "malformed", "out_of_range", "overflow"])
def test_invalid_intervals_reject_the_whole_snapshot(problem):
    day = date(2026, 9, 12)
    prices = dated_prices(day)
    if problem == "naive":
        prices[3]["start"] = "2026-09-12T03:00:00"
    elif problem == "gap":
        prices[3]["start"] = prices[4]["start"]
    elif problem == "overlap":
        prices[3]["start"] = prices[2]["start"]
    elif problem == "reversed":
        prices[3]["start"], prices[3]["end"] = prices[3]["end"], prices[3]["start"]
    elif problem == "wrong_order":
        prices[3], prices[4] = prices[4], prices[3]
    elif problem == "missing_end":
        del prices[3]["end"]
    elif problem == "missing_slot":
        prices.pop(3)
    elif problem == "mixed_resolution":
        prices[3]["end"] = "2026-09-12T03:15:00+00:00"
    elif problem == "malformed":
        prices[3]["start"] = "not a timestamp"
    elif problem == "out_of_range":
        prices[3]["start"] = "0001-01-01T00:00:00+01:00"
    elif problem == "overflow":
        prices[3]["price"] = 1e308
    else:
        prices[3]["price"] = "unavailable"
    states, attrs, errors = normalize(prices, datetime(2026, 9, 12, 6, tzinfo=UTC))
    assert states[PRICE] == "unavailable" and PRICE not in attrs
    assert errors == {"price_series": "invalid_price_series"}


def test_tomorrow_must_be_the_next_local_day_including_dst(berlin_time):
    day = date(2026, 3, 28)
    now = datetime(2026, 3, 28, 23, 30, tzinfo=BERLIN).astimezone(UTC)
    today = dated_prices(day, timezone=BERLIN)
    tomorrow = dated_prices(day + timedelta(days=1), timezone=BERLIN)
    assert len(normalize(today, now, tomorrow)[1][PRICE]["tomorrow"]) == 23
    states, attrs, errors = normalize(today, now, today)
    assert states[PRICE] == "unavailable" and PRICE not in attrs
    assert errors == {"price_series": "invalid_price_series"}


@pytest.mark.parametrize("hour,soc,current,expected", [(19, 50, 50, "Akku nur Entladen"),
                                                      (6, 20, 10, "Akku Netzladen")])
def test_missing_plan_clears_previous_price_decision_and_recovers(berlin_time, hour, soc, current, expected):
    from tests.test_engine import StrategyEngine, measurements, solar_attrs

    engine = StrategyEngine()
    now = datetime(2026, 9, 12, hour, tzinfo=BERLIN)
    prices = dated_prices(now.date(), timezone=BERLIN, price=.1)
    for slot in prices[18:22]:
        slot["price"] = .5
    native = measurements(**{
        "sensor.opti_soc": soc,
        "sun.sun": "below_horizon", "sensor.opti_forecast_today_kwh": 0,
        "sensor.opti_forecast_tomorrow_kwh": 0,
        "sensor.opti_forecast_remaining_today_kwh": 0,
        "sensor.opti_price_current_ct_kwh": current,
    })

    def evaluate(series, previous=None):
        states, attrs, errors = normalize(series, now.astimezone(UTC), native=native)
        if previous:
            states["input_select.akkusteuerung_modus"] = previous
        attrs.update(solar_attrs(now))
        return engine.evaluate(states, attrs, now), errors

    valid, errors = evaluate(prices)
    assert not errors
    assert valid.mode == expected
    invalid, errors = evaluate([.1] * 18 + [.5] * 4 + [.1] * 2, valid.mode)
    assert errors == {"price_series": "invalid_price_series"}
    assert invalid.states["sensor.opti_price_level"] == "unavailable"
    assert invalid.states["sensor.opti_peak_reserve_soc"] == "unavailable"
    assert invalid.states["binary_sensor.opti_peak_reserve_aktiv"] == "off"
    assert invalid.mode not in ("Akku nur Entladen", "Akku schnell Entladen", "Akku Netzladen")
    assert invalid.reason != valid.reason
    recovered, errors = evaluate(prices, invalid.mode)
    assert not errors and recovered.mode == valid.mode
    assert recovered.reason == valid.reason


@pytest.mark.parametrize("overrides,mode", [
    ({"input_boolean.akku_opti_automatik": "off"}, "Akku Pause"),
    ({"sensor.opti_soc": "unavailable"}, "Akku Pause"),
    ({"sensor.opti_soc": 5}, "Akku nur Laden"),
    ({"sensor.opti_soc": 96}, "Akku nur Entladen"),
])
def test_invalid_price_plan_preserves_independent_core_and_soc_guards(berlin_time, overrides, mode):
    from tests.test_engine import StrategyEngine, measurements, solar_attrs

    now = datetime(2026, 9, 12, 19, tzinfo=BERLIN)
    native = measurements(**{"input_select.akkusteuerung_modus": "Akku nur Entladen", **overrides})
    states, attrs, errors = normalize([.3] * 24, now.astimezone(UTC), native=native)
    attrs.update(solar_attrs(now))
    result = StrategyEngine().evaluate(states, attrs, now)
    assert errors == {"price_series": "invalid_price_series"}
    assert result.mode == mode
