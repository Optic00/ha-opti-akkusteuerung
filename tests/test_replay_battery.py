"""Battery model of the replay harness: mode semantics, limits, losses, balance."""
from __future__ import annotations

import pytest

from tools.replay import battery
from tools.replay.battery import BatteryParams, step

PARAMS = BatteryParams(capacity_kwh=10, minsoc=10, maxsoc=90, max_charge_w=2000, max_discharge_w=4000)


def balanced(flows, pv, load):
    return pv + flows.import_kwh + flows.discharge_kwh == pytest.approx(load + flows.export_kwh + flows.charge_kwh)


@pytest.mark.parametrize("mode, charge, export", [
    (battery.DYNAMIC, 0.4, 0.0),
    (battery.ONLY_CHARGE, 0.4, 0.0),
    (battery.ONLY_DISCHARGE, 0.0, 0.4),
    (battery.PAUSE, 0.0, 0.4),
])
def test_surplus_goes_to_battery_only_when_charging_is_allowed(mode, charge, export):
    flows = step(mode, 50, pv_kwh=0.5, load_kwh=0.1, hours=0.25, params=PARAMS)
    assert flows.charge_kwh == pytest.approx(charge)
    assert flows.export_kwh == pytest.approx(export)
    assert flows.import_kwh == 0 and flows.discharge_kwh == 0
    assert balanced(flows, 0.5, 0.1)


@pytest.mark.parametrize("mode, discharge, imported", [
    (battery.DYNAMIC, 0.3, 0.0),
    (battery.ONLY_DISCHARGE, 0.3, 0.0),
    (battery.ONLY_CHARGE, 0.0, 0.3),
    (battery.PAUSE, 0.0, 0.3),
])
def test_deficit_comes_from_battery_only_when_discharging_is_allowed(mode, discharge, imported):
    flows = step(mode, 50, pv_kwh=0.1, load_kwh=0.4, hours=0.25, params=PARAMS)
    assert flows.discharge_kwh == pytest.approx(discharge)
    assert flows.import_kwh == pytest.approx(imported)
    assert balanced(flows, 0.1, 0.4)


def test_grid_charge_fills_at_charge_power_from_grid_and_pv():
    flows = step(battery.GRID_CHARGE, 50, pv_kwh=0.1, load_kwh=0.2, hours=0.25, params=PARAMS, charge_limit_w=1000)
    assert flows.charge_kwh == pytest.approx(0.25)
    assert flows.import_kwh == pytest.approx(0.35)
    assert flows.export_kwh == 0 and flows.discharge_kwh == 0
    assert balanced(flows, 0.1, 0.2)


def test_charge_power_limit_and_plant_maximum_cap_charging():
    big_surplus = step(battery.DYNAMIC, 50, pv_kwh=2.0, load_kwh=0.0, hours=0.25, params=PARAMS)
    assert big_surplus.charge_kwh == pytest.approx(0.5)  # 2000 W * 0.25 h
    assert big_surplus.export_kwh == pytest.approx(1.5)
    limited = step(battery.DYNAMIC, 50, pv_kwh=2.0, load_kwh=0.0, hours=0.25, params=PARAMS, charge_limit_w=400)
    assert limited.charge_kwh == pytest.approx(0.1)
    discharge = step(battery.DYNAMIC, 50, pv_kwh=0.0, load_kwh=3.0, hours=0.25, params=PARAMS)
    assert discharge.discharge_kwh == pytest.approx(1.0)  # 4000 W * 0.25 h
    assert discharge.import_kwh == pytest.approx(2.0)


def test_soc_limits_stop_flows_and_losses_apply_per_direction():
    nearly_full = step(battery.DYNAMIC, 89, pv_kwh=1.0, load_kwh=0.0, hours=1, params=PARAMS)
    # Room is 0.1 kWh of stored energy, i.e. 0.1 / 0.95 kWh at the AC side.
    assert nearly_full.charge_kwh == pytest.approx(0.1 / 0.95)
    assert nearly_full.soc_after == pytest.approx(90)
    nearly_empty = step(battery.DYNAMIC, 11, pv_kwh=0.0, load_kwh=1.0, hours=1, params=PARAMS)
    assert nearly_empty.discharge_kwh == pytest.approx(0.1 * 0.95)
    assert nearly_empty.soc_after == pytest.approx(10)
    full = step(battery.ONLY_CHARGE, 90, pv_kwh=1.0, load_kwh=0.0, hours=1, params=PARAMS)
    assert full.charge_kwh == 0 and full.export_kwh == pytest.approx(1.0)
    empty = step(battery.ONLY_DISCHARGE, 10, pv_kwh=0.0, load_kwh=1.0, hours=1, params=PARAMS)
    assert empty.discharge_kwh == 0 and empty.import_kwh == pytest.approx(1.0)


def test_round_trip_loses_energy_symmetrically():
    charged = step(battery.ONLY_CHARGE, 50, pv_kwh=1.0, load_kwh=0.0, hours=1, params=PARAMS)
    assert charged.soc_after == pytest.approx(50 + 0.95 * 10)
    discharged = step(battery.ONLY_DISCHARGE, charged.soc_after, pv_kwh=0.0, load_kwh=0.95 * 0.95, hours=1, params=PARAMS)
    assert discharged.discharge_kwh == pytest.approx(0.95 * 0.95)
    assert discharged.import_kwh == 0
    assert discharged.soc_after == pytest.approx(50)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        step("Akku schnell Laden", 50, 0.1, 0.1, 0.25, PARAMS)


def test_params_from_plant_apply_overrides():
    params = BatteryParams.from_plant({"capacity_kwh": 12.8, "minsoc": 5, "maxsoc": 95}, eta_charge=0.9)
    assert params.max_charge_w == 3000 and params.eta_charge == 0.9 and params.eta_discharge == 0.95


def test_discharge_limit_caps_discharging_like_the_setpoint():
    full = step(battery.DYNAMIC, 50, pv_kwh=0.0, load_kwh=0.5, hours=0.25, params=PARAMS)
    limited = step(battery.DYNAMIC, 50, pv_kwh=0.0, load_kwh=0.5, hours=0.25, params=PARAMS, discharge_limit_w=400)
    assert full.discharge_kwh == pytest.approx(0.5)
    assert limited.discharge_kwh == pytest.approx(0.1) and limited.import_kwh == pytest.approx(0.4)
    assert balanced(limited, 0.0, 0.5)
    assert step(battery.ONLY_DISCHARGE, 50, pv_kwh=0.0, load_kwh=0.5, hours=0.25, params=PARAMS,
                discharge_limit_w=-5).discharge_kwh == 0
