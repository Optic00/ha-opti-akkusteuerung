"""Shadow check for holding PV charging until today's cheap window (#136)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.negative_price_hold import (
    NegativePriceHoldShadow,
    negative_price_hold_check,
)

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 15, 9, 0, tzinfo=TZ).astimezone(UTC)


def local(hour, minute=0):
    return datetime(2026, 7, 15, hour, minute, tzinfo=TZ).astimezone(UTC)


def prices(cheap=range(12, 15), value=-1.0):
    return {"today": [value if hour in cheap else 20.0 for hour in range(24)], "tomorrow": []}


def pv(kw=6.0, first=6, last=20):
    return [(local(hour, minute), kw * 1000 if first <= hour < last else 0.0)
            for hour in range(9, 24) for minute in (0, 30)]


def check(**overrides):
    args = dict(now=NOW, timezone=TZ, price_series=prices(), feed_in_ct=7.0, pv_slots=pv(),
                load_w=500, score=4, soc=40, capacity_kwh=10, minsoc=10, maxsoc=90,
                peak_reserve_active=False, peak_reserve_soc=None, charge_efficiency=0.9)
    args.update(overrides)
    return negative_price_hold_check(**args)


def test_good_forecast_and_enough_pv_after_window_would_hold():
    result = check()
    assert result["verdict"] == "hold"
    assert result["window_start"] == local(12).isoformat()
    assert result["window_end"] == local(15).isoformat()
    assert result["window_min_price_ct"] == -1.0
    assert result["needed_kwh"] == 5.0
    # 12:00-20:00 at 5.5 kW surplus, 90 % efficiency
    assert result["pv_surplus_from_window_kwh"] == 39.6
    assert result["observation_only"] is True and result["controls_battery"] is False


@pytest.mark.parametrize("overrides,verdict", [
    ({"price_series": prices(cheap=())}, "no_window"),
    ({"price_series": prices(cheap=range(5, 8))}, "no_window"),
    ({"now": local(13)}, "in_window"),
    ({"score": 2}, "forecast_weak"),
    ({"soc": 10}, "min_soc"),
    ({"peak_reserve_active": True, "peak_reserve_soc": 45}, "peak_reserve"),
    ({"soc": 90}, "full"),
    ({"pv_slots": pv(kw=1.2)}, "insufficient_pv"),  # 5.04 kWh < 1.2 * 5 kWh
    ({"pv_slots": pv(last=13)}, "insufficient_pv"),
])
def test_verdicts(overrides, verdict):
    assert check(**overrides)["verdict"] == verdict


def test_price_at_feed_in_is_not_a_window():
    assert check(price_series=prices(value=7.0))["verdict"] == "no_window"


def test_first_unfinished_run_wins_and_tomorrow_is_ignored():
    series = {"today": [20.0] * 10 + [5.0] + [20.0] * 3 + [5.0] * 2 + [20.0] * 8,
              "tomorrow": [-5.0] * 24}
    result = check(price_series=series, now=local(11))
    assert result["window_start"] == local(14).isoformat()
    assert result["window_end"] == local(16).isoformat()
    late = check(price_series={"today": [20.0] * 24, "tomorrow": [-5.0] * 24})
    assert late["verdict"] == "no_window"


@pytest.mark.parametrize("overrides,detail", [
    ({"feed_in_ct": None}, "feed_in"),
    ({"price_series": None}, "price_series"),
    ({"price_series": {"today": [1.0] * 5}}, "price_series"),
    ({"soc": None}, "soc"),
    ({"load_w": "unavailable"}, "load"),
    ({"capacity_kwh": 0}, "battery"),
    ({"soc": 120}, "battery"),
    ({"load_w": -1}, "battery"),
    ({"pv_slots": []}, "pv_timing"),
    ({"pv_slots": pv()[:10]}, "pv_timing"),
    ({"peak_reserve_active": True, "peak_reserve_soc": "unavailable"}, "peak_reserve"),
])
def test_missing_data_is_reported(overrides, detail):
    result = check(**overrides)
    assert result["status"] == "data_missing"
    assert result["detail"] == detail
    assert "verdict" not in result


def observe(shadow, at, verdict, power=0.0, export=0.0, window=True, status="ready"):
    held = {"status": status, "verdict": verdict}
    if window:
        held["window_start"] = local(12).isoformat()
    return shadow.observe(at, held, [0.9, 7.0, 10, 90], power, export)


def test_totals_split_charging_before_and_inside_the_window():
    shadow = NegativePriceHoldShadow()
    observe(shadow, local(10), "hold", power=3600)
    observe(shadow, local(10, 1), "insufficient_pv", power=1800)
    observe(shadow, local(10, 2), "in_window", power=0, export=6000)
    totals = observe(shadow, local(10, 3), "no_window", window=False)
    assert totals["ready_seconds"] == 180
    assert totals["hold_seconds"] == 60
    assert totals["window_seconds"] == 60
    assert totals["charged_while_hold_kwh"] == 0.06
    assert totals["charged_before_window_kwh"] == 0.09
    assert totals["exported_in_window_kwh"] == 0.1
    assert totals["charged_in_window_kwh"] == 0.0
    assert totals["started_at"] == local(10).isoformat()


def test_gaps_errors_and_changed_assumptions_do_not_count():
    shadow = NegativePriceHoldShadow()
    observe(shadow, local(10), "hold", power=1000)
    assert observe(shadow, local(10, 5), "hold", power=1000)["hold_seconds"] == 0  # gap
    observe(shadow, local(10, 6), "hold", status="data_missing")  # counts 10:05-10:06
    assert observe(shadow, local(10, 7), "hold")["ready_seconds"] == 60  # missing data held
    assert observe(shadow, local(10, 6), "hold")["ready_seconds"] == 60  # clock stepped back
    shadow.interrupt()
    assert observe(shadow, local(10, 7), "hold")["ready_seconds"] == 60
    totals = shadow.observe(local(10, 8), {"status": "ready", "verdict": "hold"}, [0.9, 8.0, 10, 90], 0, 0)
    assert totals["ready_seconds"] == 0
    assert totals["started_at"] == local(10, 8).isoformat()


def test_unknown_power_counts_time_but_no_energy():
    shadow = NegativePriceHoldShadow()
    observe(shadow, local(10), "in_window", power=None, export=None)
    totals = observe(shadow, local(10, 1), "in_window")
    assert totals["window_seconds"] == 60
    assert totals["charged_in_window_kwh"] == 0 and totals["exported_in_window_kwh"] == 0


def test_snapshot_round_trip_and_invalid_restore():
    shadow = NegativePriceHoldShadow()
    observe(shadow, local(10), "hold", power=3600)
    observe(shadow, local(10, 1), "hold")
    restored = NegativePriceHoldShadow()
    restored.restore(shadow.snapshot())
    assert restored.totals() == shadow.totals()
    # The first check after a restart never counts the downtime.
    assert observe(restored, local(10, 2), "hold")["hold_seconds"] == 60
    for bad in (None, {"version": 2}, {**shadow.snapshot(), "hold_seconds": -1},
                {**shadow.snapshot(), "started_at": "2026-07-15T10:00:00"},
                {**shadow.snapshot(), "assumptions": [1, 2]},
                {**shadow.snapshot(), "assumptions": ["x", 2, 3]},
                {**shadow.snapshot(), "started_at": 5}):
        target = NegativePriceHoldShadow()
        target.restore(bad)
        assert target.totals()["ready_seconds"] == 0
        assert target.snapshot()["assumptions"] is None


def test_pv_surplus_follows_the_model_charge_efficiency():
    base = check()
    lossless = check(charge_efficiency=1.0)
    assert base["pv_surplus_from_window_kwh"] == 39.6
    assert lossless["pv_surplus_from_window_kwh"] == pytest.approx(39.6 / 0.9, abs=0.01)


def test_restore_migrates_snapshots_without_the_model_charge_efficiency():
    """Before the battery model the totals had three assumptions and assumed 0.9."""
    shadow = NegativePriceHoldShadow()
    observe(shadow, local(10), "hold", power=3600)
    observe(shadow, local(10, 1), "hold")
    saved = shadow.snapshot()
    assert saved["hold_seconds"] == 60
    restored = NegativePriceHoldShadow()
    restored.restore({**saved, "assumptions": saved["assumptions"][1:]})
    assert restored.snapshot()["assumptions"] == saved["assumptions"]
    assert restored.totals()["hold_seconds"] == 60
    assert observe(restored, local(10, 2), "hold")["hold_seconds"] == 60
    changed = restored.observe(local(10, 3), {"status": "ready", "verdict": "hold"},
                               [1.0, 7.0, 10, 90], 0, 0)
    assert changed["hold_seconds"] == 0
