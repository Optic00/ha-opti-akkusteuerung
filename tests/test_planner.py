"""Horizon planner core: optimality on small cases, physics, P10 effect, fail-closed shadow."""

import json
from datetime import UTC, datetime, timedelta
from itertools import product
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.arbitrage import _price_periods
from custom_components.opti_akku.planner import (
    DYNAMIC,
    GRID_CHARGE,
    ONLY_CHARGE,
    ONLY_DISCHARGE,
    Battery,
    Horizon,
    PlannerBudgetError,
    Settings,
    Slot,
    baseline_cost,
    executable_mode,
    ladder_mode,
    plan,
)
from custom_components.opti_akku import planner_shadow
from custom_components.opti_akku.planner_shadow import (
    ForecastHistory,
    PlannerInputs,
    PlannerShadow,
    build_slots,
    deviation,
    history_prices,
    history_pv,
    pv_forecast_hourly,
)

TZ = ZoneInfo("Europe/Berlin")
T0 = datetime(2026, 1, 20, 0, 0, tzinfo=TZ).astimezone(UTC)


def hourly(prices, pv=None, load=None, start=T0, hours=1.0):
    pv = pv or [0.0] * len(prices)
    load = load or [0.5] * len(prices)
    return [Slot(start + timedelta(hours=i * hours), hours, p, pv[i], load[i]) for i, p in enumerate(prices)]


def brute_force(horizon: Horizon, start_soc: float) -> tuple[float, float]:
    """Best (programme objective, true cost) over every sequence of the horizon's discrete actions."""
    start = horizon.position(start_soc)
    terminal = horizon.terminal_values(start)
    best, best_cost = float("inf"), float("inf")
    for sequence in product(*horizon.actions):
        position, objective, cost, ok = start, 0.0, 0.0, True
        for action in sequence:
            position += action.delta
            if not -1e-9 <= position <= horizon.states - 1 + 1e-9:
                ok = False
                break
            objective += action.objective_eur
            cost += action.cost_eur
        if ok:
            end = terminal[int(round(position))] if abs(position - round(position)) < 1e-9 else float("inf")
            if objective + end < best:
                best, best_cost = objective + end, cost + end
    return best, best_cost


@pytest.mark.parametrize("terminal", ["value", "hold", "none"])
@pytest.mark.parametrize("grid", [True, False])
def test_dp_matches_brute_force_on_small_cases(terminal, grid):
    # Flows are multiples of the 0.25 kWh grid step, so every action lands on a grid point.
    battery = Battery(1.0, 0, 100, 500, 500, 1.0, 1.0)
    settings = Settings(feed_in_ct=8, wear_ct_kwh=2, grid_charging=grid, terminal=terminal, soc_step=25)
    slots = hourly([10, 40, 5, 60], pv=[0, 0.25, 0.5, 0], load=[0.25, 0.5, 0.0, 0.5])
    for soc in (0, 50, 100):
        horizon = Horizon(slots, battery, settings)
        horizon.solve(soc)
        result = horizon.walk(soc)
        objective, cost = brute_force(horizon, soc)
        assert result.objective_eur == pytest.approx(cost, abs=1e-9)
        assert horizon.values[0][int(horizon.position(soc))] == pytest.approx(objective, abs=1e-9)


def test_interpolated_dp_never_beats_exact_enumeration_and_stays_close():
    # Fractional moves: the tables are interpolated (a chord above a convex
    # cost-to-go), so the programme can only be slightly pessimistic.
    battery = Battery(2.0, 0, 100, 800, 800, 0.9, 0.9)
    settings = Settings(feed_in_ct=8, wear_ct_kwh=2, terminal="none", soc_step=10)
    slots = hourly([10, 40, 5, 60], pv=[0, 0.3, 0.7, 0], load=[0.2, 0.45, 0.1, 0.4])
    horizon = Horizon(slots, battery, settings)
    horizon.solve(50)
    result = horizon.walk(50)
    start = horizon.position(50)
    exact = float("inf")
    for sequence in product(*horizon.actions):
        position, objective = start, 0.0
        for action in sequence:
            position += action.delta
            if not -1e-9 <= position <= horizon.states - 1 + 1e-9:
                break
            objective += action.objective_eur
        else:
            exact = min(exact, objective)
    assert result.objective_eur >= exact - 1e-9
    assert result.objective_eur < exact + 0.01


def test_known_solution_charges_cheap_and_discharges_expensive():
    battery = Battery(1.0, 0, 100, 1000, 1000, 1.0, 1.0)
    settings = Settings(feed_in_ct=0, wear_ct_kwh=0, terminal="none", soc_step=25)
    result = plan(hourly([10, 50], load=[0.0, 0.5]), battery, settings, 0)
    first, second = result.steps
    assert first.mode == GRID_CHARGE and first.grid_charge_kwh == pytest.approx(0.5)
    assert second.mode == DYNAMIC and second.discharge_kwh == pytest.approx(0.5)
    assert result.cost_eur == pytest.approx(0.05)


def test_physics_balance_limits_and_exact_energy():
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
        stored_change = (step.soc_after - step.soc_before) / 100 * battery.capacity_kwh
        assert stored_change == pytest.approx(step.charge_kwh * 0.9 - step.discharge_kwh / 0.8, abs=1e-9)
    assert result.steps[2].charge_kwh > 0  # PV surplus is stored
    assert result.objective_eur <= baseline_cost(slots, battery, settings, 10) + 1e-9


def test_no_discharge_without_soc_decrease_at_minsoc():
    # Review reproduction: 96 quarter hours of 0.02 kWh load at MinSOC must all be imported.
    battery = Battery(12.8, 5, 95, 3000, 8000)
    slots = hourly([30.0] * 96, pv=[0.0] * 96, load=[0.02] * 96, hours=0.25)
    result = plan(slots, battery, Settings(feed_in_ct=10), 5.0)
    assert sum(step.discharge_kwh for step in result.steps) == 0
    assert sum(step.import_kwh for step in result.steps) == pytest.approx(1.92)
    assert all(step.soc_after == pytest.approx(5.0) for step in result.steps)


def test_standing_still_survives_action_thinning():
    # Review reproduction: a small battery with many grid moves thins the action list;
    # the zero action must remain, otherwise a full-power deficit has no plan at all.
    battery = Battery(5.0, 5, 95, 3000, 8000)
    slots = hourly([25.0] * 36, load=[3.02] * 36)
    result = plan(slots, battery, Settings(feed_in_ct=10, grid_charging=False), 5.0)
    assert len(result.steps) == 36 and sum(step.discharge_kwh for step in result.steps) == 0
    for actions in Horizon(slots, battery, Settings()).actions:
        assert any(a.charge_kwh == 0 and a.discharge_kwh == 0 for a in actions)
    # Flat prices: nothing to gain from buying, so no grid purchase at all.
    flat = plan(hourly([25.0] * 36, load=[0.5] * 36), battery, Settings(feed_in_ct=10), 5.0)
    assert sum(step.grid_charge_kwh for step in flat.steps) == 0


def test_terminal_credit_never_buys_grid_energy_for_itself():
    battery = Battery(12.8, 5, 95, 3000, 8000)
    settings = Settings(feed_in_ct=8, wear_ct_kwh=0, terminal="value", terminal_factor=1.0)
    # Cheap now, no consumption ahead: holding would only be worth the credit.
    result = plan(hourly([10, 30, 30, 30, 30, 30], load=[0] * 6), battery, settings, 5)
    assert sum(step.grid_charge_kwh for step in result.steps) == pytest.approx(0.0)


def test_executable_mode_differs_from_ladder_classification():
    battery = Battery(10.0, 5, 95, 3000, 5000)
    surplus = Slot(T0, 1.0, 20, 2.0, 0.5)
    deficit = Slot(T0, 1.0, 20, 0.0, 0.5)
    assert ladder_mode(surplus, battery, 50, 1.5, 0) == DYNAMIC == executable_mode(surplus, 1.5, 0)
    assert ladder_mode(surplus, battery, 50, 0.0, 0) == ONLY_DISCHARGE == executable_mode(surplus, 0.0, 0)
    assert ladder_mode(surplus, battery, 50, 2.5, 0) == GRID_CHARGE == executable_mode(surplus, 2.5, 0)
    assert ladder_mode(deficit, battery, 50, 0, 0.5) == DYNAMIC == executable_mode(deficit, 0, 0.5)
    assert ladder_mode(deficit, battery, 50, 0, 0.0) == ONLY_CHARGE == executable_mode(deficit, 0, 0.0)
    # A partial discharge is "hold" for the ladder but must execute as Dynamisch with a setpoint.
    assert ladder_mode(deficit, battery, 50, 0, 0.2) == ONLY_CHARGE
    assert executable_mode(deficit, 0, 0.2) == DYNAMIC
    # Tiny grid top-ups in a PV slot count as PV charging in the ladder vocabulary.
    assert ladder_mode(surplus, battery, 50, 1.52, 0) == DYNAMIC
    assert executable_mode(surplus, 1.52, 0) == GRID_CHARGE


def test_steps_carry_setpoints_that_reproduce_their_flows():
    battery = Battery(10.0, 5, 95, 3000, 5000)
    settings = Settings(feed_in_ct=8, wear_ct_kwh=3, terminal="none", soc_step=10)
    horizon = Horizon(hourly([30, 60], load=[0.5, 0.5]), battery, settings)
    horizon.solve(10.4)
    for step in horizon.walk(10.4).steps:
        assert step.charge_w is None or step.charge_w == pytest.approx(step.charge_kwh * 1000)
        assert step.discharge_w is None or step.discharge_w == pytest.approx(step.discharge_kwh * 1000)
        assert (step.discharge_w is None) == (step.discharge_kwh == 0)


def test_budget_and_action_limits():
    battery = Battery(100.0, 0, 100, 50000, 50000)  # huge: many integer moves
    slots = hourly([20] * 36, load=[1.0] * 36)
    horizon = Horizon(slots, battery, Settings(soc_step=0.1))
    assert horizon.states <= 401 and all(len(actions) <= 125 for actions in horizon.actions)
    with pytest.raises(PlannerBudgetError):
        horizon.solve(50, deadline_s=0.0)
    assert horizon.values == []


def test_min_benefit_falls_back_to_self_consumption():
    battery = Battery(10.0, 5, 95, 3000, 5000)
    slots = hourly([20, 21, 20, 21], load=[0.5] * 4)  # almost no spread
    cheap = Horizon(slots, battery, Settings(feed_in_ct=8, min_benefit_ct=0.0))
    cheap.solve(50)
    optimum, baseline = cheap.recommend(50)
    assert optimum.source == "plan"
    strict = Horizon(slots, battery, Settings(feed_in_ct=8, min_benefit_ct=50.0))
    strict.solve(50)
    chosen, baseline = strict.recommend(50)
    assert chosen.source == "self_consumption" and chosen.objective_eur == baseline.objective_eur
    assert chosen.first.mode == DYNAMIC


# -- shadow inputs -------------------------------------------------------------

def solcast(start, rows, updated=None, attribute="detailedForecast", minutes=30):
    detail = []
    for i, (p10, p50) in enumerate(rows):
        row = {"period_start": (start + timedelta(minutes=minutes * i)).isoformat()}
        if p10 is not None:
            row["pv_estimate10"] = p10
        if p50 is not None:
            row["pv_estimate"] = p50
        detail.append(row)
    return SimpleNamespace(state="1", attributes={attribute: detail},
                           last_reported=updated or start, last_updated=updated or start)


def test_pv_forecast_hourly_sums_half_hours_and_fails_closed():
    now = T0 + timedelta(hours=6)
    state = solcast(now, [(1.0, 2.0), (1.0, 2.0), (0.5, 1.0), (0.5, 1.0)], updated=now)
    status, hours = pv_forecast_hourly([state, None], now)
    assert status == "ready"
    assert hours == {now: (1.0, 2.0), now + timedelta(hours=1): (0.5, 1.0)}
    stale = solcast(now, [(1.0, 2.0)], updated=now - timedelta(hours=13))
    assert pv_forecast_hourly([stale], now)[0] == "stale"
    not_covering = solcast(now + timedelta(hours=2), [(1.0, 2.0)], updated=now)
    assert pv_forecast_hourly([not_covering], now)[0] == "stale"
    broken = solcast(now, [(1.0, 2.0)], updated=now)
    broken.attributes["detailedForecast"][0]["pv_estimate10"] = "x"
    assert pv_forecast_hourly([broken], now) == ("invalid", {})
    assert pv_forecast_hourly([None], now) == ("no_forecast", {})
    other = solcast(now, [(0.0, 2.0), (1.0, 2.0)], updated=now)
    assert pv_forecast_hourly([state, other], now)[0] == "invalid"


def test_pv_forecast_hourly_default_age_is_twelve_hours_and_the_option_overrides_it():
    now = T0 + timedelta(hours=6)
    aged = solcast(now, [(1.0, 2.0), (1.0, 2.0)], updated=now - timedelta(seconds=30000))
    assert pv_forecast_hourly([aged], now)[0] == "ready"
    assert pv_forecast_hourly([aged], now, max_age=21600)[0] == "stale"
    assert pv_forecast_hourly([aged], now, max_age=0)[0] == "ready"


def test_pv_forecast_hourly_counts_only_complete_hours():
    """A half-covered hour is a gap, not half the energy; the horizon ends there."""
    now = T0 + timedelta(hours=6)
    state = solcast(now, [(1.0, 2.0), (1.0, 2.0), (0.5, 1.0)], updated=now)
    status, hours = pv_forecast_hourly([state], now)
    assert status == "ready" and hours == {now: (1.0, 2.0)}
    periods = [(now, now + timedelta(hours=1), 20.0), (now + timedelta(hours=1), now + timedelta(hours=2), 20.0)]
    assert len(build_slots(now, periods, hours, "p10", None, 500)) == 1
    # Only the second half of the current hour: nothing covers now.
    late = solcast(now + timedelta(minutes=30), [(1.0, 2.0)], updated=now)
    assert pv_forecast_hourly([late], now)[0] == "stale"
    # The two halves may come from two entities (today/tomorrow at midnight).
    first = solcast(now, [(1.0, 2.0)], updated=now)
    second = solcast(now + timedelta(minutes=30), [(1.0, 2.0)], updated=now)
    assert pv_forecast_hourly([first, second], now) == ("ready", {now: (1.0, 2.0)})
    # An hourly row covers its hour on its own.
    hourly = solcast(now, [(1.0, 2.0)], updated=now, attribute="detailedHourly", minutes=60)
    assert pv_forecast_hourly([hourly], now) == ("ready", {now: (1.0, 2.0)})


def test_pv_forecast_accepts_hourly_rows_and_single_quantiles():
    now = T0 + timedelta(hours=6)
    hourly_state = solcast(now, [(1.0, None), (0.5, None)], updated=now, attribute="detailedHourly", minutes=60)
    status, hours = pv_forecast_hourly([hourly_state], now)
    assert status == "ready"
    assert hours == {now: (1.0, None), now + timedelta(hours=1): (0.5, None)}
    periods = [(now, now + timedelta(hours=1), 20.0), (now + timedelta(hours=1), now + timedelta(hours=2), 20.0)]
    assert len(build_slots(now, periods, hours, "p10", None, 500)) == 2
    assert build_slots(now, periods, hours, "p50", None, 500) == []


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
    periods = _price_periods(prices(day, now), now, TZ)
    result = plan(build_slots(now, periods, pv, "p10", None, 600), battery, settings, 5)
    charged = [step for step in result.steps if step.grid_charge_kwh > 0]
    assert charged and all(step.slot.price_ct == 12 for step in charged)
    assert {step.mode for step in charged} == {GRID_CHARGE}
    assert all(step.mode == DYNAMIC and step.discharge_kwh > 0
               for step in result.steps if step.slot.price_ct == 48)
    assert all(step.ladder_mode == ONLY_CHARGE for step in result.steps if step.slot.price_ct == 22)
    assert all(step.indifferent for step in result.steps if step.slot.price_ct == 20)  # empty battery


def test_slots_require_coverage_of_now_and_a_continuous_axis():
    now, pv = day_context(6, {12: (1.0, 2.0)})
    periods = _price_periods(prices(precharge_prices(), now), now, TZ)
    later = [period for period in periods if period[0] > now + timedelta(hours=2)]
    assert build_slots(now, later, pv, "p10", None, 500) == []
    with_gap = [period for period in periods if period[0] != now + timedelta(hours=3)]
    assert len(build_slots(now, with_gap, pv, "p10", None, 500)) == 3


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
    assert check["verdict"] == GRID_CHARGE == check["executable_mode"]
    assert check["deviation"] == "planner_grid_charge"
    assert check["grid_charge_w"] > 0 and check["expected_saving_eur"] >= 0
    assert len(check["soc_target_next_hours"]) == 12
    assert check["solve_ms"] is not None and check["slot_minutes"] == 60
    totals = shadow.observe(now, TZ, check)
    assert totals["today"]["ready_seconds"] == 0  # first observation never counts
    later = now + timedelta(seconds=30)
    check2 = shadow.evaluate(later, TZ, inputs(later, pv, soc=21, fallback_load_w=510))
    assert check2["planned_at"] == check["planned_at"]  # cached despite load noise
    totals = shadow.observe(later, TZ, check2)
    assert totals["today"]["planner_grid_charge_seconds"] == 30
    assert totals["total"]["ready_seconds"] == 30 and totals["today_disagreement_percent"] == 100.0
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


def test_indifferent_time_is_not_disagreement():
    shadow = PlannerShadow()
    now = T0
    shadow.observe(now, TZ, {"status": "ready", "deviation": "indifferent"})
    totals = shadow.observe(now + timedelta(seconds=30), TZ, {"status": "ready", "deviation": "indifferent"})
    assert totals["today"]["indifferent_seconds"] == 30 and totals["today_disagreement_percent"] is None


def test_cached_plan_stays_consistent_with_the_shrinking_first_slot():
    # Review reproduction: flat prices, no PV, nothing to gain. Nine minutes into
    # the cached plan the saving must not turn negative through mismatched horizons.
    now, pv = day_context(6, {})
    flat = PlannerInputs(**{**inputs(now, pv).__dict__, "price_series": prices([25.0] * 24, now)})
    shadow = PlannerShadow()
    first = shadow.evaluate(now, TZ, flat)
    later = now + timedelta(minutes=9)
    second = shadow.evaluate(later, TZ, flat)
    assert first["status"] == second["status"] == "ready"
    assert second["planned_at"] == first["planned_at"]
    assert second["expected_saving_eur"] >= -1e-6
    assert second["horizon_hours"] == pytest.approx(first["horizon_hours"] - 0.15, abs=1e-6)


def test_changed_limits_and_settings_invalidate_the_cache_at_once():
    now, pv = day_context(6, {11: (0.6, 2.5), 12: (0.8, 3.0), 13: (0.8, 3.0)})
    shadow = PlannerShadow()
    first = shadow.evaluate(now, TZ, inputs(now, pv))
    assert first["verdict"] == GRID_CHARGE
    later = now + timedelta(seconds=30)
    no_grid = shadow.evaluate(later, TZ, inputs(later, pv, grid_charging=False))
    assert no_grid["status"] == "ready" and no_grid["grid_charge_w"] == 0 and no_grid["planned_at"] != first["planned_at"]
    capped = shadow.evaluate(later + timedelta(seconds=30), TZ, inputs(later, pv, max_charge_w=0))
    assert capped["status"] == "ready" and capped["grid_charge_w"] == 0 and capped["planned_charge_w"] == 0
    smaller = shadow.evaluate(later + timedelta(seconds=60), TZ, inputs(later, pv, maxsoc=30))
    assert max(smaller["soc_target_next_hours"]) <= 30


def test_grid_check_fails_closed_when_the_check_quantile_is_missing():
    now, pv = day_context(6, {11: (0.6, None), 12: (0.8, None), 13: (0.8, None)})
    pv = {hour: (a, None) for hour, (a, b) in pv.items()}
    plain = PlannerShadow().evaluate(now, TZ, inputs(now, pv))
    assert plain["verdict"] == GRID_CHARGE
    checked = PlannerShadow().evaluate(now, TZ, inputs(now, pv, grid_check_quantile="p50"))
    assert checked["status"] == "data_missing" and checked["detail"] == "grid_check_p50_missing"
    assert "verdict" not in checked


def test_tomorrow_only_prices_never_become_todays_recommendation():
    now, pv = day_context(6, {12: (1.0, 2.0)})
    tomorrow_only = PlannerInputs(**{**inputs(now, pv).__dict__,
                                     "price_series": {"today": [], "tomorrow": precharge_prices()}})
    check = PlannerShadow().evaluate(now, TZ, tomorrow_only)
    assert check["status"] == "data_missing" and check["detail"] in ("price_series", "price_now")
    assert "verdict" not in check


def test_budget_exceeded_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(planner_shadow, "SOLVE_BUDGET_S", 0.0)
    now, pv = day_context(6, {12: (1.0, 2.0)})
    check = PlannerShadow().evaluate(now, TZ, inputs(now, pv))
    assert check["status"] == "data_missing" and check["detail"] == "budget_exceeded"


def test_budget_covers_the_cached_first_slot_update(monkeypatch):
    now, pv = day_context(6, {12: (1.0, 2.0)})
    shadow = PlannerShadow()
    assert shadow.evaluate(now, TZ, inputs(now, pv))["status"] == "ready"
    monkeypatch.setattr(planner_shadow, "SOLVE_BUDGET_S", 0.0)
    check = shadow.evaluate(now + timedelta(minutes=1), TZ, inputs(now, pv))
    assert check["status"] == "data_missing" and check["detail"] == "budget_exceeded"


def test_grid_check_against_p50_blocks_grid_charging_that_p50_pv_would_cover():
    now, pv = day_context(6, {11: (0.6, 2.5), 12: (0.8, 3.0), 13: (0.8, 3.0), 14: (0.6, 2.5)})
    plain = PlannerShadow().evaluate(now, TZ, inputs(now, pv))
    checked = PlannerShadow().evaluate(now, TZ, inputs(now, pv, grid_check_quantile="p50"))
    assert plain["verdict"] == GRID_CHARGE and plain["grid_check"] is None
    assert checked["grid_check"] == "blocked_by_p50" and checked["verdict"] != GRID_CHARGE


@pytest.mark.parametrize("field, value, detail", [
    ("price_series", None, "price_series"),
    ("price_series", {"today": ["x"] * 24}, "price_series"),
    ("soc", "bad", "soc"),
    ("soc", 140, "soc"),
    ("capacity_kwh", None, "capacity_kwh"),
    ("maxsoc", 4, "battery_invalid"),
    ("feed_in_ct", float("nan"), "feed_in"),
    ("pv_status", "stale", "pv_forecast_stale"),
    ("pv_hourly", {}, "price_now"),
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


# -- forecast history ----------------------------------------------------------

def test_forecast_history_roundtrip_keeps_gaps_and_mixed_price_grids():
    history = ForecastHistory(max_days=2)
    now = T0 + timedelta(hours=6)
    pv = {now: (1.0, 2.0), now + timedelta(hours=1): (0.5, None), now + timedelta(hours=3): (0.25, 0.5)}
    periods = [(now + timedelta(minutes=15 * i), now + timedelta(minutes=15 * (i + 1)), 20.0 + i) for i in range(4)]
    periods += [(now + timedelta(hours=1 + i), now + timedelta(hours=2 + i), 30.0 + i) for i in range(2)]
    assert history.record(now, pv, periods) is True
    entry = history.entries[-1]
    assert history_pv(entry) == pv
    assert history_prices(entry) == periods
    assert len(entry["price"]) == 2 and entry["price"][0][1] == 900 and entry["price"][1][1] == 3600
    restored = ForecastHistory(max_days=2)
    restored.restore(json.loads(json.dumps(history.snapshot())))
    assert restored.entries == history.entries and restored.error is None
    restored.restore({"version": 1, "entries": []})
    assert restored.entries == [] and restored.error == "unsupported_format"
    restored.restore({"version": 3, "entries": [{"t": "garbage"}, 3, {"t": 5, "pv": {"from": 0}}, entry,
                                                {"t": 10**30}, {"t": True}]})
    assert restored.entries == [entry] and restored.error == "5_entries_invalid"
    assert restored.summary()["entries"] == 1
    # A future stamp must not block recording.
    future = {"t": int((now + timedelta(days=3)).timestamp())}
    restored.restore({"version": 3, "entries": [entry, future]})
    assert restored.record(now + timedelta(hours=1), {now + timedelta(hours=1): (3.0, 3.0)}, []) is True
    assert all(item["t"] <= int((now + timedelta(hours=1)).timestamp()) for item in restored.entries)


def test_forecast_history_keeps_single_hour_single_quantile_entries():
    history = ForecastHistory(max_days=2)
    now = T0 + timedelta(hours=6)
    assert history.record(now, {now: (0.0, None)}, []) is True
    entry = history.entries[-1]
    assert entry["pv"]["p10"] == "0" and entry["pv"]["p50"] == "-"
    assert history_pv(entry) == {now: (0.0, None)}
    restored = ForecastHistory(max_days=2)
    restored.restore(json.loads(json.dumps(history.snapshot())))
    assert restored.entries == [entry] and restored.error is None


def test_forecast_history_migrates_version_2_and_names_rejected_input():
    now = T0 + timedelta(hours=6)
    stamp = int(now.timestamp())
    old = {"version": 2, "entries": [
        {"t": stamp, "pv": {"from": stamp, "p10": [100, None, 300], "p50": [200, 250, None]},
         "price": [[stamp, 900, [20.0, 21.5]]]},
        {"t": stamp + 3600, "pv": {"from": stamp}}]}
    history = ForecastHistory(max_days=2)
    history.restore(old)
    assert history.error == "1_entries_invalid"
    assert history.entries[0]["pv"] == {"from": stamp, "p10": "100,-,300", "p50": "200,250,-"}
    assert history.entries[0]["price"] == [[stamp, 900, "20.0,21.5"]]
    assert history_pv(history.entries[0])[now] == (0.1, 0.2)
    assert history_prices(history.entries[0])[1][2] == 21.5
    unknown = ForecastHistory(max_days=2)
    unknown.restore({"version": 1, "entries": []})
    assert unknown.error == "unsupported_format"


def test_forecast_history_only_records_changes_and_stays_small():
    history = ForecastHistory(max_days=60)
    start = T0
    pv = {start + timedelta(hours=h): (float(h % 7), float(h % 5)) for h in range(48)}
    periods = [(start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)), 20.0) for i in range(192)]
    assert history.record(start, pv, periods) is True
    # Same forecast an hour later: the shifted rows carry no new information.
    assert history.record(start + timedelta(hours=1), pv, periods) is False
    # Fill 60 days as in operation: 12 PV updates and 2 price updates per day.
    writes = 0
    for day in range(1, 62):
        for hour in range(24):
            at = start + timedelta(days=day, hours=hour)
            if hour % 2 == 0:
                pv = {at + timedelta(hours=h): (float((h + day + hour) % 7), float(h % 5)) for h in range(36)}
            series = periods if hour not in (13, 14) else [
                (at + timedelta(minutes=15 * i), at + timedelta(minutes=15 * (i + 1)), 20.0 + day) for i in range(192)]
            if history.record(at, pv, series):
                writes += 1
    # The HA store writes an indented document with a hull; measure that format.
    hull = {"version": 1, "minor_version": 1, "key": "opti_akku.entry.planner_history", "data": history.snapshot()}
    size = len(json.dumps(hull, indent=2))
    per_day = writes / 61
    assert history.summary()["entries"] <= 61 * 14
    print(f"history size {size} B (indented store format), {per_day:.1f} saves/day")
    assert size < 1_000_000, size
    assert per_day <= 14.5
    assert size * per_day < 15_000_000


# -- validation and helper paths -----------------------------------------------

@pytest.mark.parametrize("battery", [
    Battery(float("nan"), 5, 95, 3000, 5000),
    Battery(10, 5, 95, -1, 5000),
    Battery(10, 5, 95, 3000, 5000, eta_charge=1.5),
    Battery(10, 95, 5, 3000, 5000),
])
def test_invalid_batteries_are_rejected_by_the_core(battery):
    assert battery.problem() == "battery_invalid"
    with pytest.raises(ValueError, match="battery_invalid"):
        Horizon(hourly([20]), battery, Settings())


def test_horizon_rejects_empty_or_inconsistent_configuration():
    battery = Battery(10, 5, 95, 3000, 5000)
    with pytest.raises(ValueError, match="no_slots"):
        Horizon([], battery, Settings())
    with pytest.raises(ValueError, match="terminal_invalid"):
        Horizon(hourly([20]), battery, Settings(terminal="forever"))
    with pytest.raises(ValueError, match="soc_step_invalid"):
        Horizon(hourly([20]), battery, Settings(soc_step=200))
    with pytest.raises(ValueError, match="unsolved"):
        Horizon(hourly([20]), battery, Settings()).resolve_first(hourly([20])[0])


def test_walk_first_and_describe_work_on_an_unsolved_horizon():
    from custom_components.opti_akku.planner import describe, interpolate

    battery = Battery(10, 5, 95, 3000, 5000)
    horizon = Horizon(hourly([10, 50], load=[0.0, 0.5]), battery, Settings(terminal="none"))
    step = horizon.first(5)  # solves on demand
    assert step is not None and step.mode == GRID_CHARGE
    fresh = Horizon(hourly([10, 50], load=[0.0, 0.5]), battery, Settings(terminal="none"))
    result = fresh.walk(5)
    assert result.soc_path()[0] == 5 and len(result.soc_path()) == 3
    summary = describe(result)
    assert summary["modes"][0] == GRID_CHARGE and summary["source"] == "plan"
    assert describe("not a plan") == {}
    assert interpolate([1.0, 2.0], -0.5) == 1.0 and interpolate([1.0, 2.0], 7) == 2.0


def test_pv_forecast_rejects_naive_timestamps_and_oversized_rows():
    now = T0 + timedelta(hours=6)
    naive = solcast(now, [(1.0, 2.0)], updated=now)
    naive.attributes["detailedForecast"][0]["period_start"] = "2026-01-20T06:00:00"
    assert pv_forecast_hourly([naive], now) == ("invalid", {})
    huge = solcast(now, [(1.0, 2.0)] * 101, updated=now)
    assert pv_forecast_hourly([huge], now) == ("invalid", {})
    bad_update = solcast(now, [(1.0, 2.0)], updated=now)
    bad_update.last_updated = "yesterday"
    assert pv_forecast_hourly([bad_update], now) == ("invalid", {})
    without_rows = SimpleNamespace(state="1", attributes={}, last_updated=now, last_reported=now)
    assert pv_forecast_hourly([without_rows, solcast(now, [(1.0, 2.0), (1.0, 2.0)], updated=now)], now)[0] == "ready"


def test_slots_take_the_hourly_load_profile_when_present():
    now, pv = day_context(6, {})
    periods = _price_periods(prices(precharge_prices(), now), now, TZ)
    key = str(int(now.timestamp()))
    slots = build_slots(now, periods, pv, "p10", {key: 1500.0, "x": "bad"}, 500)
    assert slots[0].load_kwh == pytest.approx(1.5) and slots[1].load_kwh == pytest.approx(0.5)
    assert build_slots(now, periods, pv, "p10", {key: 99999.0}, 500)[0].load_kwh == pytest.approx(0.5)


def test_shadow_reports_short_horizons_and_bad_fallback_load():
    now, pv = day_context(6, {})
    short = {hour: value for hour, value in pv.items() if hour <= now}
    check = PlannerShadow().evaluate(now, TZ, inputs(now, short))
    assert check["status"] == "data_missing" and check["detail"] == "horizon_short"
    check = PlannerShadow().evaluate(now, TZ, inputs(now, pv, fallback_load_w=-5))
    assert check["detail"] == "load"


def test_grid_check_confirms_when_the_median_plan_also_buys():
    now, pv = day_context(6, {})  # no PV at all: the P50 plan needs the grid as well
    check = PlannerShadow().evaluate(now, TZ, inputs(now, pv, grid_check_quantile="p50"))
    assert check["status"] == "ready" and check["grid_check"] == "confirmed_by_p50"
    assert check["verdict"] == GRID_CHARGE


def test_counter_restore_rejects_naive_stamps_and_wrong_buckets():
    shadow = PlannerShadow()
    good = shadow.snapshot()
    for broken in (
        {**good, "started_at": "2026-01-20T06:00:00"},
        {**good, "today": "nope"},
        {**good, "day": 5},
        {**good, "started_at": 12},
    ):
        restored = PlannerShadow()
        restored.data["total"]["ready_seconds"] = 7
        restored.restore(broken)
        assert restored.data["total"]["ready_seconds"] == 0


def test_history_rejects_unpacked_or_misaligned_rows():
    from custom_components.opti_akku.planner_shadow import _pv_unchanged

    history = ForecastHistory(max_days=2)
    history.restore({"version": 3, "entries": "not a list"})
    assert history.error == "unsupported_format"
    history.restore({"version": 3, "entries": [{"t": HISTORY_STAMP, "pv": {"from": HISTORY_STAMP, "p10": [1], "p50": [1]}}]})
    assert history.entries == [] and history.error == "1_entries_invalid"
    assert _pv_unchanged({"from": 1800, "p10": "1", "p50": "1"}, {"from": 3600, "p10": "1", "p50": "1"}) is False
    assert _pv_unchanged(None, {"from": 3600, "p10": "1", "p50": "1"}) is False


HISTORY_STAMP = int(T0.timestamp())
