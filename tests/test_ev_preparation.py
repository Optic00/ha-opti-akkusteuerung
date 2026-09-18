from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from copy import deepcopy
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.ev_preparation import (
    EVPreparation,
    apply_preparation,
    command_signals,
    deadline_plan,
    signals,
)
from custom_components.opti_akku.engine import Evaluation

NOW = datetime(2026, 9, 15, 9, tzinfo=UTC)
CFG = {
    "enabled": True,
    "vehicle_soc": "sensor.car",
    "charging": "binary_sensor.charging",
    "vehicle_threshold": 40,
    "house_target": 80,
}


def state(v, unit=None, now=NOW):
    return SimpleNamespace(
        state=str(v), attributes={"unit_of_measurement": unit}, last_reported=now, last_updated=now
    )


def fixture():
    return {"sensor.car": state(20, "%"), "binary_sensor.charging": state("off")}, {
        "sensor.opti_soc": 50,
        "sensor.opti_grid_export_w": 1000,
        "sensor.opti_grid_import_w": 0,
        "sensor.opti_battery_power_w": 0,
    }


def step(model, states, measurements, seconds=0, **kw):
    return model.update(CFG, states, NOW + timedelta(seconds=seconds), measurements, 95, True, **kw)


def evaluation(mode="Akku nur Entladen", reason="ueber Ziel-SoC", decision_id=None):
    return Evaluation({}, {}, mode, reason, {}, decision_id=decision_id or ("above_target" if reason == "ueber Ziel-SoC" else "unknown"))


def test_prepares_without_presence_and_surplus_does_not_disappear_when_charging():
    s, m = fixture()
    model = EVPreparation()
    assert not step(model, s, m)["ready"]
    m["sensor.opti_grid_export_w"] = 0
    m["sensor.opti_battery_power_w"] = 1000
    p = step(model, s, m, 60)
    assert p["ready"] and p["surplus_before_battery_w"] == 1000
    result, report = apply_preparation(evaluation(), p)
    assert result.mode == "Akku nur Laden"
    assert report["status"] == "preparing"
    assert "PV bis 80%" in result.reason


@pytest.mark.parametrize(
    "reason,mode",
    [
        ("Peak-Leiter L1 (VE entladen)", "Akku nur Entladen"),
        ("MinSOC-Schutz", "Akku nur Laden"),
        ("Balancing-Watchdog (Netz-Vollladung)", "Akku Netzladen"),
        ("EV-Sperre", "Akku nur Laden"),
    ],
)
def test_preparation_preserves_higher_priorities(reason, mode):
    e = evaluation(mode, reason)
    result, p = apply_preparation(e, {"ready": True, "target_soc": 80})
    assert result is e and p["status"] == "higher_priority"


@pytest.mark.parametrize("v", ["on", "unavailable"])
def test_charging_or_unknown_blocks_competition_even_at_night(v):
    s, m = fixture()
    s["binary_sensor.charging"] = state(v)
    p = step(EVPreparation(), s, m, can_prepare=False)
    result, _ = apply_preparation(evaluation(), p)
    assert result.mode == "Akku Pause"
    result, _ = apply_preparation(
        evaluation("Akku nur Entladen", "Peak-Leiter L1 (VE entladen)"), p
    )
    assert result.mode == "Akku Pause"
    result, _ = apply_preparation(evaluation("Akku nur Laden", "MinSOC-Schutz"), p)
    assert result.mode == "Akku nur Laden"


def test_house_ceiling_and_vehicle_hysteresis():
    s, m = fixture()
    model = EVPreparation()
    step(model, s, m)
    assert step(model, s, m, 60)["ready"]
    m["sensor.opti_soc"] = 80
    assert step(model, s, m, 70)["status"] == "target_reached"
    m["sensor.opti_soc"] = 79
    assert step(model, s, m, 80)["status"] == "target_reached"
    m["sensor.opti_soc"] = 78
    assert not step(model, s, m, 90)["ready"]
    assert step(model, s, m, 150)["ready"]
    s["sensor.car"] = state(44, "%")
    assert step(model, s, m, 160)["ready"]
    s["sensor.car"] = state(45, "%")
    assert step(model, s, m, 170)["status"] == "no_demand"


@pytest.mark.parametrize("bad", ["stale", "unit", "nan", "missing"])
def test_invalid_car_soc_never_starts(bad):
    s, m = fixture()
    s["sensor.car"] = state(
        20, "%" if bad != "unit" else "kWh", NOW - timedelta(hours=25) if bad == "stale" else NOW
    )
    if bad == "nan":
        s["sensor.car"] = state("nan", "%")
    if bad == "missing":
        s.pop("sensor.car")
    assert step(EVPreparation(), s, m)["status"] == "data_missing"


def test_restarts_clock_gaps_and_no_pv_do_not_keep_old_authorization():
    s, m = fixture()
    model = EVPreparation()
    step(model, s, m)
    assert step(model, s, m, 60)["ready"]
    assert not step(model, s, m, 200)["ready"]
    assert not step(model, s, m, 190)["ready"]
    m["sensor.opti_grid_export_w"] = 1000
    m["sensor.opti_battery_power_w"] = -1000
    assert not step(model, s, m, 250)["ready"]
    assert not step(EVPreparation(), s, m, 260)["ready"]


def test_maxsoc_and_disabled_manual_policy():
    s, m = fixture()
    model = EVPreparation()
    p = model.update(CFG, s, NOW, m, 60, True)
    assert p["target_soc"] == 60
    p = model.update(CFG, s, NOW, m, 60, False)
    assert p["status"] == "disabled"
    e = evaluation()
    before = deepcopy(e)
    assert apply_preparation(e, p)[0] == before


def test_preparation_waits_when_pv_policy_does_not_allow_reserving_energy():
    states, measurements = fixture()
    result = step(EVPreparation(), states, measurements, can_prepare=False)
    assert result["status"] == "waiting_surplus"
    assert result["ready"] is False


@pytest.mark.parametrize(
    "cfg,maximum",
    [
        ({**CFG, "vehicle_threshold": 96}, 95),
        ({**CFG, "house_target": 49}, 95),
        (CFG, None),
    ],
)
def test_invalid_preparation_limits_fail_closed(cfg, maximum):
    states, measurements = fixture()
    result = EVPreparation().update(cfg, states, NOW, measurements, maximum, True)
    assert result["status"] == "data_missing"
    assert result["ready"] is False


def test_missing_house_measurement_never_authorizes_preparation():
    states, measurements = fixture()
    measurements.pop("sensor.opti_grid_import_w")
    result = step(EVPreparation(), states, measurements)
    assert result["status"] == "data_missing"
    assert result["ready"] is False


def test_signal_guard_detects_arrival_during_bus_wait_and_expiry():
    s, m = fixture()
    before = signals(CFG, s, NOW)
    s["binary_sensor.charging"] = state("on")
    assert signals(CFG, s, NOW) != before
    s["binary_sensor.charging"] = state("off")
    assert signals(CFG, s, NOW + timedelta(days=2)) != before


@pytest.mark.parametrize(
    "reason", ["70% Ueberschuss (tag, entprellt)", "AC Ueberschuss (tag, entprellt)"]
)
def test_car_priority_stops_existing_surplus_branches(reason):
    # The real branches choose Dynamisch, not Nur Laden.
    result, _ = apply_preparation(evaluation("Akku Dynamisch", reason), {"charging_guard": True})
    assert result.mode == "Akku Pause"


def test_loss_of_vehicle_data_returns_to_ordinary_self_consumption_not_forced_export():
    s, m = fixture()
    model = EVPreparation()
    step(model, s, m)
    assert step(model, s, m, 60)["ready"]
    s["sensor.car"] = state("unavailable", "%")
    p = step(model, s, m, 70)
    result, _ = apply_preparation(evaluation(), p)
    assert result.mode == "Akku nur Entladen"  # house self-consumption, no forced setpoint
    assert not p["ready"]


def test_priority_uses_decision_id_not_display_text():
    result, report = apply_preparation(
        evaluation(reason="Beliebiger übersetzter Text", decision_id="above_target"),
        {"ready": True, "target_soc": 90},
    )
    assert result.mode == "Akku nur Laden" and report["status"] == "preparing"
    protected = evaluation(reason="ueber Ziel-SoC", decision_id="minimum_soc")
    assert apply_preparation(protected, {"ready": True, "target_soc": 90})[0] is protected
    unknown = evaluation(decision_id="unknown")
    assert apply_preparation(unknown, {"ready": True, "target_soc": 90})[0] is unknown


def test_command_signals_keep_soc_ticks_but_invalidate_boundaries_and_stale_data():
    from custom_components.opti_akku.ev_preparation import command_signals
    s, _ = fixture()
    initial = command_signals(CFG, s, NOW)
    s["sensor.car"] = state(21, "%")
    assert command_signals(CFG, s, NOW) == initial
    s["sensor.car"] = state(40, "%")
    band = command_signals(CFG, s, NOW)
    assert band != initial
    s["sensor.car"] = state(44, "%")
    assert command_signals(CFG, s, NOW) == band
    s["sensor.car"] = state(45, "%")
    assert command_signals(CFG, s, NOW) != band
    assert command_signals(CFG, s, NOW + timedelta(hours=25))[0] == "invalid"
    s["binary_sensor.charging"] = state("on")
    assert command_signals(CFG, s, NOW)[1] is True
    s["binary_sensor.charging"] = state("unavailable")
    assert command_signals(CFG, s, NOW)[1] is None
    holiday = {**CFG, "away": "input_boolean.holiday"}
    assert command_signals(holiday, s, NOW)[2] is None
    s["input_boolean.holiday"] = state("on")
    assert command_signals(holiday, s, NOW)[2] is True


@pytest.mark.parametrize("decision_id", ["negative_price", "peak_precharge"])
def test_vehicle_charging_retains_explicit_price_charging_priority(decision_id):
    e = evaluation("Akku Netzladen", "Explicit price priority", decision_id)
    result, report = apply_preparation(e, {"charging_guard": True})
    assert result is e
    assert report["controls_battery"] is False


def test_vehicle_charging_overrides_residual_value_hold():
    held = evaluation(
        "Akku nur Laden", "Restwert halten", decision_id="arbitrage_hold"
    )
    result, report = apply_preparation(held, {"charging_guard": True})
    assert result.mode == "Akku Pause"
    assert result.decision_id == "ev_priority"
    assert report["controls_battery"] is True


def test_deadline_plan_reports_energy_latest_start_and_feasibility():
    cfg = {
        **CFG,
        "departure": "input_datetime.departure",
        "vehicle_target_soc": 80,
        "vehicle_capacity_kwh": 60,
        "charge_power_kw": 11,
        "vehicle_charge_efficiency_percent": 90,
    }
    states, _ = fixture()
    states["input_datetime.departure"] = state(
        (NOW + timedelta(hours=4)).isoformat()
    )
    plan = deadline_plan(cfg, states, NOW, 20)
    assert plan["deadline_status"] == "ready"
    assert plan["vehicle_required_energy_kwh"] == 36
    assert plan["vehicle_required_ac_energy_kwh"] == 40
    assert plan["vehicle_charge_hours"] == pytest.approx(3.64, abs=0.01)
    assert plan["vehicle_required_average_kw"] == 10
    assert plan["vehicle_deadline_feasible"] is True
    assert plan["deadline_urgent"] is False


def test_deadline_target_drives_preparation_above_legacy_threshold():
    cfg = {
        **CFG,
        "departure": "input_datetime.departure",
        "vehicle_target_soc": 80,
        "vehicle_capacity_kwh": 60,
        "charge_power_kw": 11,
        "vehicle_charge_efficiency_percent": 90,
    }
    states, measurements = fixture()
    states["sensor.car"] = state(60, "%")
    states["input_datetime.departure"] = state(
        (NOW + timedelta(hours=8)).isoformat()
    )
    model = EVPreparation()
    first = model.update(cfg, states, NOW, measurements, 95, True)
    ready = model.update(
        cfg, states, NOW + timedelta(seconds=60), measurements, 95, True
    )
    assert first["deadline_status"] == "ready"
    assert ready["ready"] is True
    assert ready["vehicle_required_energy_kwh"] == 12


def test_deadline_configuration_fails_closed_when_source_or_values_are_missing():
    states, measurements = fixture()
    cfg = {**CFG, "departure": "input_datetime.departure", "vehicle_target_soc": 80}
    report = EVPreparation().update(cfg, states, NOW, measurements, 95, True)
    assert report["status"] == "data_missing"
    assert report["ready"] is False


def test_recurring_deadline_and_write_guard_cross_latest_start():
    cfg = {
        **CFG,
        "departure": "input_datetime.departure",
        "vehicle_target_soc": 80,
        "vehicle_capacity_kwh": 60,
        "charge_power_kw": 10,
        "vehicle_charge_efficiency_percent": 90,
    }
    states, _ = fixture()
    states["sensor.car"] = state(70, "%")
    states["input_datetime.departure"] = state("11:00:00")
    before = command_signals(cfg, states, NOW)
    after = command_signals(cfg, states, NOW + timedelta(hours=1, minutes=30))
    assert before[3] == after[3] == "ready"
    assert before[4] is False
    assert after[4] is True
    tomorrow = deadline_plan(cfg, states, NOW + timedelta(hours=3), 70)
    assert datetime.fromisoformat(tomorrow["departure_at"]).date() == (
        NOW + timedelta(days=1)
    ).date()


def test_naive_local_datetime_uses_configured_timezone():
    cfg = {
        **CFG,
        "departure": "input_datetime.departure",
        "vehicle_target_soc": 80,
        "vehicle_capacity_kwh": 60,
        "charge_power_kw": 11,
        "vehicle_charge_efficiency_percent": 90,
    }
    states, _ = fixture()
    states["input_datetime.departure"] = state("2026-09-15 12:00:00")
    plan = deadline_plan(cfg, states, NOW, 20, ZoneInfo("Europe/Berlin"))
    assert plan["deadline_time_resolution"] == "local_datetime"
    assert datetime.fromisoformat(plan["departure_at"]).hour == 10


def test_expired_absolute_deadline_falls_back_to_legacy_preparation():
    cfg = {
        **CFG,
        "departure": "input_datetime.departure",
        "vehicle_target_soc": 80,
        "vehicle_capacity_kwh": 60,
        "charge_power_kw": 11,
        "vehicle_charge_efficiency_percent": 90,
    }
    states, measurements = fixture()
    states["input_datetime.departure"] = state(
        (NOW - timedelta(hours=1)).isoformat()
    )
    model = EVPreparation()
    first = model.update(cfg, states, NOW, measurements, 95, True)
    ready = model.update(
        cfg, states, NOW + timedelta(seconds=60), measurements, 95, True
    )
    assert first["deadline_status"] == "deadline_passed"
    assert ready["ready"] is True


def test_recurring_deadline_documents_dst_gap_and_first_fold():
    berlin = ZoneInfo("Europe/Berlin")
    cfg = {
        **CFG,
        "departure": "input_datetime.departure",
        "vehicle_target_soc": 80,
        "vehicle_capacity_kwh": 60,
        "charge_power_kw": 11,
        "vehicle_charge_efficiency_percent": 90,
    }
    states, _ = fixture()
    states["input_datetime.departure"] = state("02:30:00")
    spring = deadline_plan(
        cfg,
        states,
        datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        20,
        berlin,
    )
    assert spring["deadline_time_resolution"] == "recurring_shifted_forward_dst_gap"
    assert datetime.fromisoformat(spring["departure_at"]).astimezone(berlin).hour == 3
    autumn = deadline_plan(
        cfg,
        states,
        datetime(2026, 10, 25, 0, 0, tzinfo=UTC),
        20,
        berlin,
    )
    assert autumn["deadline_time_resolution"] == "recurring_first_occurrence"
    assert datetime.fromisoformat(autumn["departure_at"]).astimezone(berlin).fold == 0
