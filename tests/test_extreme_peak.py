"""Extreme-price reserve buffer in the shipped peak template block."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.opti_akku.engine import StrategyEngine, load_resources


NOW = datetime(2026, 1, 15, 18, tzinfo=UTC)


def peak_engine():
    resources = load_resources()
    resources["template_blocks"] = [
        block
        for block in resources["template_blocks"]
        if any(
            sensor.get("unique_id") == "opti_peak_reserve_soc"
            for sensor in block.get("sensor", [])
        )
    ]
    resources["statistics"] = []
    resources["balancing_automations"] = []
    return StrategyEngine(resources)


def evaluate(
    prices,
    *,
    now=NOW,
    current_price=None,
    soc=5,
    capacity=10,
    minimum_soc=5,
    maximum_soc=95,
    horizon_hours=1,
):
    values = {
        "sensor.opti_battery_capacity_kwh": capacity,
        "sensor.opti_soc": soc,
        "sensor.opti_price_current_ct_kwh": (
            prices[int((now.hour + now.minute / 60) * len(prices) / 24)]
            if current_price is None
            else current_price
        ),
        "input_number.minsoc": minimum_soc,
        "input_number.maxsoc": maximum_soc,
        "input_number.opti_peak_verbrauch_kw": 0.9,
        "input_number.opti_peak_min_aufschlag_ct": 0,
        "sensor.opti_forecast_score": 0,
        "binary_sensor.opti_peak_horizont_lang": "off",
        "binary_sensor.opti_pv_reichtag": "on",
        "input_select.akkusteuerung_modus": "Akku Dynamisch",
        "sun.sun": "below_horizon",
    }
    attributes = {
        "sensor.opti_price_series": {"today": prices},
        "sun.sun": {"next_rising": (now + timedelta(hours=horizon_hours - 1)).isoformat()},
    }
    return peak_engine().evaluate(values, attributes, now)


def hourly_prices(*values):
    prices = [0] * 24
    for offset, value in enumerate(values):
        prices[18 + offset] = value
    return prices


@pytest.mark.parametrize(
    ("price", "expected_kwh"),
    [(40, 0), (60, 0), (80, 0.125), (100, 0.25), (180, 0.25)],
)
def test_extreme_buffer_factor_applies_only_above_60_ct(price, expected_kwh):
    result = evaluate(hourly_prices(price))
    attrs = result.attributes["sensor.opti_peak_reserve_soc"]

    assert attrs["benoetigt_kwh"] == pytest.approx(round(1 + expected_kwh, 2))
    assert attrs["extreme_buffer_kwh"] == pytest.approx(expected_kwh)
    assert attrs["extreme_buffer_soc"] == pytest.approx(expected_kwh * 10)


def test_extreme_buffer_is_capped_by_ten_soc_points():
    prices = hourly_prices(*([100] * 6))
    result = evaluate(
        prices,
        minimum_soc=0,
        maximum_soc=100,
        horizon_hours=6,
    )
    attrs = result.attributes["sensor.opti_peak_reserve_soc"]

    assert attrs["extreme_buffer_kwh"] == pytest.approx(1)
    assert attrs["extreme_buffer_soc"] == pytest.approx(10)


def test_extreme_buffer_is_capped_by_remaining_max_soc_headroom():
    result = evaluate(hourly_prices(100), maximum_soc=16.5)
    attrs = result.attributes["sensor.opti_peak_reserve_soc"]

    assert attrs["extreme_buffer_kwh"] == pytest.approx(0.15)
    assert attrs["extreme_buffer_soc"] == pytest.approx(1.5)
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == pytest.approx(16.5)


def test_extreme_buffer_is_allocated_between_expensive_and_very_expensive():
    prices = hourly_prices(80, 100)
    prices[:8] = [90] * 8
    result = evaluate(prices, horizon_hours=2)
    attrs = result.attributes["sensor.opti_peak_reserve_soc"]

    assert attrs["peak_stunden_exp"] == 1
    assert attrs["peak_stunden_ve"] == 1
    assert attrs["extreme_buffer_kwh"] == pytest.approx(0.375)
    assert attrs["reserve_ve_soc"] == pytest.approx(17.5)
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == pytest.approx(28.8)


@pytest.mark.parametrize(
    ("minute", "expected_kwh"),
    [(10, 0.9 * (5 / 60) * 0.25 / 0.9), (15, 0)],
)
def test_quarter_hour_extra_uses_only_actual_remaining_duration(minute, expected_kwh):
    prices = [0] * 96
    prices[18 * 4] = 100
    now = NOW.replace(minute=minute)
    result = evaluate(prices, now=now)
    attrs = result.attributes["sensor.opti_peak_reserve_soc"]

    assert attrs["extreme_buffer_kwh"] == pytest.approx(expected_kwh)
    if minute == 15:
        assert attrs["benoetigt_kwh"] == pytest.approx(0.25)


@pytest.mark.parametrize(
    ("current_price", "soc", "future_price", "expected"),
    [
        (50, 5, 100, "on"),
        (61, 5, 100, "off"),
        (50, 5, 50, "off"),
        ("unavailable", 5, 100, "off"),
    ],
)
def test_extreme_price_hold_requires_cheap_current_price_and_remaining_peak(
    current_price, soc, future_price, expected
):
    result = evaluate(
        hourly_prices(50 if current_price == "unavailable" else current_price, future_price),
        current_price=current_price,
        soc=soc,
        horizon_hours=2,
    )

    assert result.states["binary_sensor.opti_extreme_price_hold"] == expected


def test_capacity_cap_does_not_disable_protection_of_existing_extreme_reserve():
    result = evaluate(hourly_prices(50, 100), capacity=2, soc=50, horizon_hours=2)
    attrs = result.attributes["sensor.opti_peak_reserve_soc"]
    assert attrs["extreme_buffer_kwh"] == 0
    assert attrs["extreme_reserve_soc"] == pytest.approx(55)
    assert result.states["binary_sensor.opti_extreme_price_hold"] == "on"
