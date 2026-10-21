"""Horizon planner core: optimality on small cases, physics, P10 effect, fail-closed shadow."""

from datetime import UTC, datetime, timedelta
from itertools import product
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.planner import (
    DYNAMIC,
    GRID_CHARGE,
    ONLY_CHARGE,
    Battery,
    Horizon,
    Settings,
    Slot,
    baseline_cost,
    mode_for,
    plan,
)
from custom_components.opti_akku.planner_shadow import (
    ForecastHistory,
    PlannerInputs,
    PlannerShadow,
    build_slots,
    deviation,
    pv_forecast_hourly,
)

TZ = ZoneInfo("Europe/Berlin")
T0 = datetime(2026, 1, 20, 0, 0, tzinfo=TZ).astimezone(UTC)


def hourly(prices, pv=None, load=None, start=T0):
    pv = pv or [0.0] * len(prices)
    load = load or [0.5] * len(prices)
    return [Slot(start + timedelta(hours=i), 1.0, p, pv[i], load[i]) for i, p in enumerate(prices)]


def brute_force(horizon: Horizon, start_soc: float) -> float:
    """Minimum objective over every sequence of the horizon's own discrete actions."""
    start = horizon.index(start_soc)
    terminal = horizon.terminal_values(start)
    best = float("inf")
    for sequence in product(*horizon.actions):
        index, cost, ok = start, 0.0, True
        for action in sequence:
            index += action.offset
            if not 0 <= index < horizon.states:
                ok = False
                break
            cost += action.cost_eur
        if ok:
            best = min(best, cost + terminal[index])
    return best


@pytest.mark.parametrize("terminal", ["value", "hold", "none"])
@pytest.mark.parametrize("grid", [True, False])
def test_dp_matches_brute_force_on_small_cases(terminal, grid):
    battery = Battery(1.0, 0, 100, 500, 500, 0.9, 0.9)
    settings = Settings(feed_in_ct=8, wear_ct_kwh=2, grid_charging=grid, terminal=terminal, soc_step=25)
    slots = hourly([10, 40, 5, 60], pv=[0, 0.3, 0.6, 0], load=[0.2, 0.3, 0.1, 0.4])
    for soc in (0, 50, 100):
        horizon = Horizon(slots, battery, settings)
        horizon.solve(soc)
        result = horizon.walk(soc)
        assert result.objective_eur == pytest.approx(brute_force(horizon, soc), abs=1e-9)
        # The forward walk reproduces the table value of the start state.
        assert horizon.values[0][horizon.index(soc)] == pytest.approx(result.objective_eur, abs=1e-9)


def test_known_solution_charges_cheap_and_discharges_expensive():
    battery = Battery(1.0, 0, 100, 1000, 1000, 1.0, 1.0)
    settings = Settings(feed_in_ct=0, wear_ct_kwh=0, terminal="none", soc_step=25)
    result = plan(hourly([10, 50], load=[0.0, 0.5]), battery, settings, 0)
    first, second = result.steps
    assert first.mode == GRID_CHARGE and first.grid_charge_kwh == pytest.approx(0.5)
    assert second.mode == DYNAMIC and second.discharge_kwh == pytest.approx(0.5)
    assert result.cost_eur == pytest.approx(0.05)


def test_physics_balance_limits_and_efficiency():
    battery = Battery(10.0, 10, 90, 2000, 3000, 0.9, 0.8)
    settings = Settings(feed_in_ct=8, wear_ct_kwh=3)
    slots = hourly([20, 15, 50, 45, 12, 30], pv=[0, 0, 4.0, 3.0, 0, 0], load=[0.5, 0.5, 0.5, 0.5, 2.0, 2.5])
    result = plan(slots, battery, settings, 10)
    for step in result.steps:
        s = step.slot
        assert s.pv_kwh + step.import_kwh + step.discharge_kwh == pytest.approx(
            s.load_kwh + step.export_kwh + step.charge_kwh)
        assert step.charge_kwh <= 2.0 + 1e-9 and step.discharge_kwh <= 3.0 + 1e-9
        assert step.discharge_kwh <= max(0.0, s.load_kwh - s.pv_kwh) + 1e-9  # never exports
        assert 10 - 1e-9 <= step.soc_after <= 90 + 1e-9
        # Stored energy follows charge x eta and discharge / eta up to half a grid step.
        stored_change = (step.soc_after - step.soc_before) / 100 * battery.capacity_kwh
        half_step = 0.5 / 100 / 2 * battery.capacity_kwh
        assert abs(stored_change - (step.charge_kwh * 0.9 - step.discharge_kwh / 0.8)) <= half_step + 1e-9
    assert result.steps[2].charge_kwh > 0  # PV surplus is stored
    assert result.objective_eur <= baseline_cost(slots, battery, settings, 10) + 1e-9


def test_terminal_credit_never_buys_grid_energy_for_itself():
    battery = Battery(10.0, 5, 95, 3000, 5000)
    settings = Settings(feed_in_ct=8, wear_ct_kwh=0, terminal="value", terminal_factor=1.0)
    # Cheap now, no consumption ahead: holding would only be worth the credit.
    result = plan(hourly([10, 30, 30, 30], load=[0, 0, 0, 0]), battery, settings, 5)
    assert sum(step.grid_charge_kwh for step in result.steps) == pytest.approx(0.0)


def test_mode_mapping_in_the_ladder_vocabulary():
    battery = Battery(10.0, 5, 95, 3000, 5000)
    surplus = Slot(T0, 1.0, 20, 2.0, 0.5)
    deficit = Slot(T0, 1.0, 20, 0.0, 0.5)
    assert mode_for(surplus, battery, 50, 1.5, 0) == DYNAMIC
    assert mode_for(surplus, battery, 50, 0.0, 0) == "Akku nur Entladen"
    assert mode_for(surplus, battery, 50, 2.5, 0) == GRID_CHARGE
    assert mode_for(deficit, battery, 50, 0, 0.5) == DYNAMIC
    assert mode_for(deficit, battery, 50, 0, 0.0) == ONLY_CHARGE
    assert mode_for(deficit, battery, 50, 1.0, 0.0) == GRID_CHARGE


# -- shadow inputs -------------------------------------------------------------

def solcast(start, rows, updated=None):
    detail = [{"period_start": (start + timedelta(minutes=30 * i)).isoformat(),
               "pv_estimate": p50, "pv_estimate10": p10} for i, (p10, p50) in enumerate(rows)]
    return SimpleNamespace(state="1", attributes={"detailedForecast": detail},
                           last_reported=updated or start, last_updated=updated or start)


def test_pv_forecast_hourly_sums_half_hours_and_fails_closed():
    now = T0 + timedelta(hours=6)
    state = solcast(now, [(1.0, 2.0), (1.0, 2.0), (0.5, 1.0), (0.5, 1.0)], updated=now)
    status, hours = pv_forecast_hourly([state, None], now)
    assert status == "ready"
    assert hours == {now: (1.0, 2.0), now + timedelta(hours=1): (0.5, 1.0)}
    stale = solcast(now, [(1.0, 2.0)], updated=now - timedelta(hours=7))
    assert pv_forecast_hourly([stale], now)[0] == "stale"
    broken = solcast(now, [(1.0, 2.0)], updated=now)
    broken.attributes["detailedForecast"][0]["pv_estimate10"] = "x"
    assert pv_forecast_hourly([broken], now) == ("invalid", {})
    assert pv_forecast_hourly([None], now) == ("no_forecast", {})
    # Several entities for the same hours must agree.
    other = solcast(now, [(0.0, 2.0), (1.0, 2.0)], updated=now)
    assert pv_forecast_hourly([state, other], now)[0] == "invalid"


def prices(day_prices, now):
    return {"today": list(day_prices), "tomorrow": []}


def day_context(hour, pv_hours):
    """Price periods of 2026-01-20 and a PV forecast keyed by UTC hour."""
    local_midnight = datetime(2026, 1, 20, tzinfo=TZ)
    now = (local_midnight + timedelta(hours=hour)).astimezone(UTC)
    pv = {(local_midnight + timedelta(hours=h)).astimezone(UTC): values for h, values in pv_hours.items()}
    for h in range(24):
        pv.setdefault((local_midnight + timedelta(hours=h)).astimezone(UTC), (0.0, 0.0))
    return now, pv


def precharge_prices():
    return [15 if 6 <= h <= 8 else 45 if 17 <= h <= 20 else 28 for h in range(24)]


def test_issue_155_median_pv_fills_later_so_no_grid_charge_but_p10_does():
    # #155: PV comes later today. The median fills the battery before the peak,
    # the conservative P10 does not, so only the P10 plan buys grid energy early.
    battery = Battery(10.0, 5, 95, 3000, 5000)
    settings = Settings(feed_in_ct=8, wear_ct_kwh=3)
    now, pv = day_context(6, {11: (0.6, 2.5), 12: (0.8, 3.0), 13: (0.8, 3.0), 14: (0.6, 2.5)})
    from custom_components.opti_akku.arbitrage import _price_periods
    periods = _price_periods(prices(precharge_prices(), now), now, TZ)
    median = plan(build_slots(now, periods, pv, "p50", None, 500), battery, settings, 20)
    conservative = plan(build_slots(now, periods, pv, "p10", None, 500), battery, settings, 20)
    assert sum(step.grid_charge_kwh for step in median.steps) == pytest.approx(0.0)
    charged = [step for step in conservative.steps if step.grid_charge_kwh > 0]
    assert charged and all(step.slot.price_ct == 15 and step.mode == GRID_CHARGE for step in charged)


def test_winter_empty_battery_grid_charges_in_the_cheapest_hour_before_the_peak():
    battery = Battery(10.0, 5, 95, 3000, 5000)
    settings = Settings(feed_in_ct=8, wear_ct_kwh=3)
    day = [20 if h < 3 else 12 if h == 3 else 22 if h < 17 else 48 if h <= 20 else 25 for h in range(24)]
    now, pv = day_context(0, {})
    from custom_components.opti_akku.arbitrage import _price_periods
    periods = _price_periods(prices(day, now), now, TZ)
    result = plan(build_slots(now, periods, pv, "p10", None, 600), battery, settings, 5)
    charged = [step for step in result.steps if step.grid_charge_kwh > 0]
    assert charged and all(step.slot.price_ct == 12 for step in charged)
    assert {step.mode for step in charged} == {GRID_CHARGE}
    assert all(step.mode == DYNAMIC and step.discharge_kwh > 0
               for step in result.steps if step.slot.price_ct == 48)
    assert all(step.mode == ONLY_CHARGE for step in result.steps if step.slot.price_ct == 22)
    assert all(step.indifferent for step in result.steps if step.slot.price_ct == 20)  # empty battery


# -- shadow evaluation ---------------------------------------------------------

def inputs(now, pv, **overrides):
    cheapest_now = [12 if h == 6 else p for h, p in enumerate(precharge_prices())]
    values = dict(price_series=prices(cheapest_now, now), pv_status="ready", pv_hourly=pv,
                  load_hours=None, load_source="fixed_setting", fallback_load_w=500, soc=20,
                  capacity_kwh=10, minsoc=5, maxsoc=95, max_charge_w=3000, max_discharge_w=5000,
                  feed_in_ct=8, grid_charging=True, ladder_mode="Akku Dynamisch")
    values.update(overrides)
    return PlannerInputs(**values)


def test_shadow_evaluates_and_counts_deviations():
    now, pv = day_context(6, {11: (0.6, 2.5), 12: (0.8, 3.0), 13: (0.8, 3.0)})
    shadow = PlannerShadow()
    check = shadow.evaluate(now, TZ, inputs(now, pv))
    assert check["status"] == "ready" and check["controls_battery"] is False
    assert check["verdict"] == GRID_CHARGE and check["deviation"] == "planner_grid_charge"
    assert check["grid_charge_w"] > 0 and check["expected_saving_eur"] >= 0
    assert len(check["soc_target_next_hours"]) == 12
    assert check["solve_ms"] is not None and check["slot_minutes"] == 60
    totals = shadow.observe(now, TZ, check)
    assert totals["today"]["ready_seconds"] == 0  # first observation never counts
    later = now + timedelta(seconds=30)
    check2 = shadow.evaluate(later, TZ, inputs(later, pv, soc=21))
    assert check2["planned_at"] == check["planned_at"]  # cached within the interval
    totals = shadow.observe(later, TZ, check2)
    assert totals["today"]["planner_grid_charge_seconds"] == 30
    assert totals["total"]["ready_seconds"] == 30 and totals["today_disagreement_percent"] == 100.0
    # A gap over 90 s never counts; a new day resets today's bucket only.
    gap = later + timedelta(minutes=5)
    shadow.observe(gap, TZ, shadow.evaluate(gap, TZ, inputs(gap, pv)))
    assert shadow.data["total"]["ready_seconds"] == 30
    next_day = gap + timedelta(days=1)
    shadow.observe(next_day, TZ, {"status": "data_missing"})
    assert shadow.data["today"]["ready_seconds"] == 0 and shadow.data["total"]["ready_seconds"] == 30
    restored = PlannerShadow()
    restored.restore(shadow.snapshot())
    assert restored.snapshot() == shadow.snapshot()
    restored.restore({"version": 1, "today": {"ready_seconds": -1}, "total": {}})
    assert restored.data["total"]["ready_seconds"] == 0


@pytest.mark.parametrize("field, value, detail", [
    ("price_series", None, "price_series"),
    ("price_series", {"today": ["x"] * 24}, "price_series"),
    ("soc", "bad", "soc"),
    ("soc", 140, "soc"),
    ("capacity_kwh", None, "capacity_kwh"),
    ("maxsoc", 4, "battery_invalid"),
    ("feed_in_ct", float("nan"), "feed_in"),
    ("pv_status", "stale", "pv_forecast_stale"),
    ("pv_hourly", {}, "horizon_short"),
    ("quantile", "p90", "quantile"),
])
def test_shadow_fails_closed_without_exception(field, value, detail):
    now, pv = day_context(6, {12: (1.0, 2.0)})
    check = PlannerShadow().evaluate(now, TZ, inputs(now, pv, **{field: value}))
    assert check["status"] == "data_missing" and check["detail"] == detail
    assert "verdict" not in check and check["controls_battery"] is False


def test_deviation_classes():
    assert deviation(DYNAMIC, DYNAMIC) == "agree"
    assert deviation(GRID_CHARGE, DYNAMIC) == "planner_grid_charge"
    assert deviation(DYNAMIC, GRID_CHARGE) == "ladder_grid_charge"
    assert deviation(ONLY_CHARGE, DYNAMIC) == "planner_hold"
    assert deviation(DYNAMIC, "Akku Pause") == "planner_discharge"
    assert deviation("Akku nur Entladen", ONLY_CHARGE) == "planner_discharge"
    assert deviation("Akku nur Entladen", "Akku Pause") == "planner_discharge"
    assert deviation(ONLY_CHARGE, "Akku Pause") == "other"
    assert deviation("Akku nur Entladen", DYNAMIC) == "planner_no_charge"
    assert deviation(DYNAMIC, None) == "other"


def test_forecast_history_is_capped_and_hourly():
    history = ForecastHistory(max_days=2)
    now, pv = day_context(0, {12: (1.0, 2.0)})
    from custom_components.opti_akku.arbitrage import _price_periods
    periods = _price_periods(prices(precharge_prices(), now), now, TZ)
    assert history.record(now, pv, periods) is True
    assert history.record(now + timedelta(minutes=20), pv, periods) is False
    for i in range(1, 80):
        at = now + timedelta(hours=i)
        assert history.record(at, {at: (1.0, 2.0)}, [(at, at + timedelta(hours=1), 20.0)]) is True
    assert len(history.entries) == 48
    entry = history.entries[-1]
    assert entry["price_minutes"] == 60 and len(entry["pv_p10"]) == len(entry["pv_p50"])
    assert history.summary()["entries"] == 48
    restored = ForecastHistory(max_days=2)
    restored.restore(history.snapshot())
    assert restored.entries == history.entries
    restored.restore({"version": 1, "entries": [{"at": "garbage"}, 3, {"at": entry["at"]}]})
    assert restored.entries == [{"at": entry["at"]}]
    restored.restore(None)
    assert restored.entries == []
