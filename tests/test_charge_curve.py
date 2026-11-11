"""Optional charge curve (#205): validation, step selection and minimum with every limit."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.opti_akku.charge_curve import (
    HYSTERESIS_POINTS,
    ChargeCurveError,
    active_points,
    curve_limit,
    format_curve,
    normalized_curve,
    parse_curve,
)
from custom_components.opti_akku.engine import Evaluation
from custom_components.opti_akku.coordinator import OptiCoordinator
from custom_components.opti_akku.engine import StrategyEngine
from tests.test_coordinator import device
from tests.test_engine import DAYS, POWER, SOC, TEMP, evaluate, measurements

# svenp's table from #205; the duplicate 3000 W step at 98 % stays allowed.
SVEN = [{"soc": 85, "power_w": 4000}, {"soc": 93, "power_w": 3000},
        {"soc": 98, "power_w": 3000}, {"soc": 99, "power_w": 250}, {"soc": 100, "power_w": 70}]
CHARGE_KEYS = ("charge_power_w", "charge_setpoint_w", "max_charge_w", "min_charge_w")


def point(soc, power):
    return {"soc": soc, "power_w": power}


# --- Validation -------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, [], ()])
def test_absent_or_empty_curve_is_valid_and_limits_nothing(raw):
    assert parse_curve(raw) == ()
    assert curve_limit(parse_curve(raw), 100, None) is None


def test_points_are_sorted_and_stored_as_whole_numbers():
    raw = [point(99.0, 250.0), point(85, 4000), point(100, 70)]
    assert normalized_curve(raw) == [point(85, 4000), point(99, 250), point(100, 70)]
    assert normalized_curve(SVEN) == SVEN


@pytest.mark.parametrize("raw, code", [
    ("85:4000", "charge_curve_invalid"),
    ({"soc": 85, "power_w": 4000}, "charge_curve_invalid"),
    (["85"], "charge_curve_invalid"),
    ([point(s, 100) for s in (10, 20, 30, 40, 50, 60)], "charge_curve_too_many"),
    ([point(-1, 100)], "charge_curve_soc"),
    ([point(101, 100)], "charge_curve_soc"),
    ([point(85.5, 100)], "charge_curve_soc"),
    ([point(None, 100)], "charge_curve_soc"),
    ([point(True, 100)], "charge_curve_soc"),
    ([point("85", 100)], "charge_curve_soc"),
    ([point(float("nan"), 100)], "charge_curve_soc"),
    ([{"power_w": 100}], "charge_curve_soc"),
    ([point(85, -1)], "charge_curve_power"),
    ([point(85, 10001)], "charge_curve_power"),
    ([point(85, 70.5)], "charge_curve_power"),
    ([point(85, float("inf"))], "charge_curve_power"),
    ([point(85, None)], "charge_curve_power"),
    ([point(10**309, 100)], "charge_curve_soc"),
    ([point(85, 10**309)], "charge_curve_power"),
    ([point(85, 4000), point(85, 3000)], "charge_curve_duplicate"),
    ([point(85, 3000), point(93, 4000)], "charge_curve_rising"),
    ([point(99, 2500), point(85, 250)], "charge_curve_rising"),
])
def test_invalid_curves_are_rejected_with_a_specific_code(raw, code):
    with pytest.raises(ChargeCurveError) as err:
        parse_curve(raw)
    assert err.value.code == code


def test_five_points_with_boundary_values_are_allowed():
    curve = parse_curve([point(0, 10000), point(50, 5000), point(90, 5000), point(99, 0), point(100, 0)])
    assert len(curve) == 5


def test_format_curve_summarises_without_raising():
    assert format_curve(SVEN).startswith("ab 85 % 4000 W, ab 93 % 3000 W")
    assert format_curve(None) == "—"
    assert format_curve([point(85, 3000), point(93, 4000)]) == "ungültig"
    assert format_curve([point(85, 10**309)]) == "ungültig"


# --- Step selection at the boundaries ---------------------------------------

@pytest.mark.parametrize("soc, expected", [
    (0, None), (84, None), (84.9, None),
    (85, (4000, 85)), (92.9, (4000, 85)),
    (93, (3000, 93)), (97.9, (3000, 93)), (98, (3000, 98)),
    (98.9, (3000, 98)), (99, (250, 99)), (99.9, (250, 99)),
    (100, (70, 100)), (101, (70, 100)),
])
def test_each_point_applies_from_its_soc_upwards(soc, expected):
    """Rising from below (nothing held): every point applies at once."""
    assert curve_limit(parse_curve(SVEN), soc, frozenset()) == expected


@pytest.mark.parametrize("soc", [None, float("nan")])
@pytest.mark.parametrize("held", [None, frozenset(), frozenset({85, 93})])
def test_unknown_soc_takes_the_smallest_power(soc, held):
    assert curve_limit(parse_curve(SVEN), soc, held) == (70, None)


# --- Hysteresis ----------------------------------------------------------------

def run(curve, socs, held=frozenset()):
    """Advance the hold like the coordinator, once per SoC; return the ceilings."""
    curve = parse_curve(curve)
    powers = []
    for soc in socs:
        held = active_points(curve, soc, held)
        limit = curve_limit(curve, soc, held)
        powers.append(None if limit is None else limit[0])
    return powers


def test_band_is_two_points():
    assert HYSTERESIS_POINTS == 2


def test_rising_applies_each_point_at_once_and_falling_holds_within_the_band():
    socs = [84, 85, 93, 99, 100, 99, 98, 97, 96, 92, 91, 84, 83]
    assert run(SVEN, socs) == [
        None, 4000, 3000, 250, 70,   # rising: each point at once
        70,                          # 99: 100 % point held (99 > 98)
        250,                         # 98: 100 % released, 99 % held
        3000,                        # 97: 99 % released, 98 % held
        3000,                        # 96: 98 % released, 93 % reached
        3000,                        # 92: 93 % held (92 > 91)
        4000,                        # 91: 93 % released
        4000,                        # 84: 85 % held
        None,                        # 83: 85 % released
    ]


def test_rising_again_after_release_applies_at_the_point_not_later():
    assert run(SVEN, [99, 97, 98, 99]) == [250, 3000, 3000, 250]
    assert run(SVEN, [85, 83, 84, 85]) == [4000, None, None, 4000]


def test_dense_points_with_overlapping_bands_release_one_after_another():
    curve = [point(97, 500), point(98, 300), point(99, 100)]
    assert run(curve, [99, 98, 97, 96, 95, 96, 97]) == [100, 100, 300, 500, None, None, 500]


def test_fractional_soc_releases_at_exactly_two_points_below():
    curve = [point(99, 250)]
    assert run(curve, [99, 97.1, 97.0]) == [250, 250, None]


@pytest.mark.parametrize("soc, expected", [
    (82, None), (83.5, (4000, 85)), (84, (4000, 85)), (91.5, (3000, 93)),
    (97, (3000, 98)), (98, (250, 99)), (98.5, (70, 100)), (99, (70, 100)),
])
def test_without_memory_the_stricter_reading_applies(soc, expected):
    """After start or reload a point within its band counts as reached."""
    assert curve_limit(parse_curve(SVEN), soc, None) == expected


def test_hysteresis_never_raises_a_ceiling():
    curve = parse_curve(SVEN)
    starts = [start for start, _ in curve]
    holds = [None] + [frozenset(s for i, s in enumerate(starts) if mask >> i & 1)
                      for mask in range(2 ** len(starts))]
    for held in holds:
        for tenth in range(0, 1011):
            soc = tenth / 10
            plain = curve_limit(curve, soc, frozenset())
            limit = curve_limit(curve, soc, held)
            if plain is not None:
                assert limit is not None and limit[0] <= plain[0], (held, soc)


# --- Coordinator: minimum with every existing limit --------------------------

@pytest.fixture
async def coordinator(hass):
    entry = MockConfigEntry(domain="opti_akku", title="Opti Test",
        data={"host": "127.0.0.1", "port": 15020, "unit_id": 3, "profile": "sma_stp_se"},
        options={"single_writer_confirmed": True, "single_inverter": True, "sources": {}})
    entry.add_to_hass(hass)
    engine = await hass.async_add_executor_job(StrategyEngine)
    result = OptiCoordinator(hass, entry, device(), engine)
    await result.async_restore()
    yield result
    await result.async_stop()


def with_curve(coordinator, curve):
    options = {**coordinator.entry.options}
    if curve is None:
        options.pop("charge_curve", None)
    else:
        options["charge_curve"] = curve
    coordinator.hass.config_entries.async_update_entry(coordinator.entry, options=options)


def params_for(coordinator, states, curve, held=frozenset()):
    """Parameters with a given hold; the default is rising from below."""
    with_curve(coordinator, curve)
    coordinator._charge_curve_held = held
    return coordinator._parameters(states)


@pytest.mark.parametrize("curve", [None, [], [point(100, 70)]])
@pytest.mark.parametrize("manual", [None, "Akku schnell Laden"])
async def test_without_an_applicable_point_parameters_are_unchanged(coordinator, curve, manual):
    coordinator.manual_mode = manual
    states = {SOC: "60", TEMP: "46", POWER: "1234", "sensor.opti_battery_capacity_kwh": "12.8"}
    baseline = params_for(coordinator, states, None)
    assert params_for(coordinator, states, curve) == baseline
    assert coordinator._charge_curve_limit(states) is None


@pytest.mark.parametrize("soc, cap", [(84, None), (85, 4000), (93, 3000), (99, 250), (100, 70)])
async def test_curve_lowers_every_charge_parameter_never_raises(coordinator, soc, cap):
    coordinator.settings["input_number.akkusteuerung_max_ladestaerke"] = 5000
    coordinator.settings["input_number.akkusteuerung_ladestaerke_soll"] = 4500
    coordinator.settings["input_number.akkusteuerung_min_ladestaerke"] = 100
    states = {SOC: soc, TEMP: 22, POWER: 3500, "sensor.opti_battery_capacity_kwh": 12.8}
    before = params_for(coordinator, states, None)
    after = params_for(coordinator, states, SVEN)
    for key in CHARGE_KEYS:
        assert after[key] == (before[key] if cap is None else min(before[key], cap))
        assert after[key] <= before[key]
    # Discharge side and other values stay untouched.
    assert {k: v for k, v in after.items() if k not in CHARGE_KEYS} == {
        k: v for k, v in before.items() if k not in CHARGE_KEYS}


@pytest.mark.parametrize("max_w", [700, 3000])
async def test_maximum_charge_power_still_wins_below_the_curve(coordinator, max_w):
    coordinator.settings["input_number.akkusteuerung_max_ladestaerke"] = max_w
    result = evaluate(states=measurements(**{
        SOC: 90, "input_number.akkusteuerung_max_ladestaerke": max_w}))
    params = params_for(coordinator, result.states, [point(85, 4000)])
    assert params["max_charge_w"] == max_w
    assert params["charge_power_w"] == float(result.states[POWER]) <= max_w


@pytest.mark.parametrize("temperature", [-1, 46])
@pytest.mark.parametrize("cap", [50, 4000])
async def test_temperature_derating_and_curve_combine_as_minimum(coordinator, temperature, cap):
    result = evaluate(states=measurements(**{SOC: 90, TEMP: temperature}))
    strategy = float(result.states[POWER])
    assert 0 < strategy < float(evaluate(states=measurements(**{SOC: 90})).states[POWER])
    params = params_for(coordinator, result.states, [point(85, cap)])
    assert params["charge_power_w"] == min(strategy, cap)


async def test_temperature_cutoff_stays_zero_with_curve(coordinator):
    result = evaluate(states=measurements(**{SOC: 90, TEMP: 50}))
    assert float(result.states[POWER]) == 0
    assert params_for(coordinator, result.states, [point(85, 4000)])["charge_power_w"] == 0


@pytest.mark.parametrize("soc", [93, 97])
@pytest.mark.parametrize("cap", [50, 4000])
async def test_balancing_steps_and_curve_combine_as_minimum(coordinator, soc, cap):
    result = evaluate(states=measurements(**{
        SOC: soc, DAYS: 20, "input_number.opti_balancing_intervall_tage": 14,
        "input_boolean.opti_prognose_netzladen": "off"}))
    assert "Balancing ab" in result.attributes[POWER]["branch"]
    strategy = float(result.states[POWER])
    params = params_for(coordinator, result.states, [point(90, cap)])
    assert params["charge_power_w"] == min(strategy, cap)


@pytest.mark.parametrize("cap", [50, 2000])
async def test_engine_pv_peak_release_does_not_lift_the_curve(coordinator, cap):
    result = evaluate(states=measurements(**{SOC: 60, "sensor.opti_grid_export_w": 600}))
    assert result.attributes[POWER]["drossel_frei"] is True
    assert float(result.states[POWER]) == 3000
    params = params_for(coordinator, result.states, [point(50, cap)])
    assert params["charge_power_w"] == cap
    assert params["max_charge_w"] == cap


def enable_writes(coordinator, hass):
    hass.config_entries.async_update_entry(
        coordinator.entry, data={**coordinator.entry.data, "backend": "sma_modbus"})
    coordinator.settings["input_boolean.akku_opti_automatik"] = True
    coordinator.write_enabled = True


@pytest.mark.parametrize("cap", [None, 300])
async def test_downstream_pv_peak_release_is_capped_by_the_curve(coordinator, hass, cap):
    enable_writes(coordinator, hass)
    with_curve(coordinator, None if cap is None else [point(50, cap)])
    states = {SOC: 60, TEMP: 22, POWER: 1200, "sensor.opti_battery_capacity_kwh": 12.8,
              "sensor.opti_house_consumption_w": 800, "sensor.opti_battery_power_w": 0,
              "binary_sensor.opti_pv_spitze": "on", "input_number.minsoc": 10,
              "input_number.maxsoc": 95}
    evaluation = Evaluation(states, {POWER: {"drossel_frei": False}}, "Akku nur Laden",
                            "Arbitrage-Halt", {}, decision_id="arbitrage_hold")
    with patch.object(coordinator.engine, "evaluate", return_value=evaluation):
        data = await coordinator._async_update_data()
    assert data["mode"] == "Akku nur Laden"
    params = coordinator.device.async_apply.await_args.args[1]
    limits = data["reason_details"]["charge_power_limits"]
    assert "PV-Spitze: Drossel gelöst (nachgelagerter PV-Lademodus)" in limits
    max_w = coordinator.settings["input_number.akkusteuerung_max_ladestaerke"]
    assert params["charge_power_w"] == (max_w if cap is None else cap)
    assert ("Ladekurve ab 50 % SoC: Obergrenze aktiv (frei ab 48 %)" in limits) == (cap is not None)


@pytest.mark.parametrize("mode", ["Akku schnell Laden", "Akku Netzladen", "Akku 0.2C Laden",
                                  "Akku Automatisch", "Akku nur Laden"])
async def test_manual_modes_and_hot_battery_obey_the_curve(coordinator, hass, mode):
    enable_writes(coordinator, hass)
    coordinator.manual_mode = mode
    coordinator.device.async_read.return_value.update({SOC: 90, TEMP: 46})
    with_curve(coordinator, None)
    await coordinator._async_update_data()
    before = coordinator.device.async_apply.await_args.args[1]
    with_curve(coordinator, [point(85, 250)])
    data = await coordinator._async_update_data()
    after = coordinator.device.async_apply.await_args.args[1]
    assert coordinator.device.async_apply.await_args.args[0] == mode
    for key in CHARGE_KEYS:
        assert after[key] == min(before[key], 250)
    assert "Akkutemperatur ab 45 °C: Ladeleistung im manuellen Modus begrenzt" in data[
        "reason_details"]["charge_power_limits"]
    assert "Ladekurve ab 85 % SoC: Obergrenze aktiv (frei ab 83 %)" in data["reason_details"]["charge_power_limits"]


async def test_invalid_stored_curve_blocks_charging(coordinator):
    states = {SOC: 50, TEMP: 22, POWER: 2000, "sensor.opti_battery_capacity_kwh": 12.8}
    params = params_for(coordinator, states, [point(85, 3000), point(93, 4000)])
    assert all(params[key] == 0 for key in CHARGE_KEYS)
    # Any unexpected parser error also blocks instead of skipping the curve.
    with patch("custom_components.opti_akku.coordinator.parse_curve", side_effect=OverflowError):
        broken = coordinator._parameters(states)
    assert all(broken[key] == 0 for key in CHARGE_KEYS)
    assert coordinator._charge_curve_limit(states) == (0.0, "Ladekurve ungültig: Laden gesperrt")


async def test_unknown_soc_uses_the_smallest_curve_power(coordinator):
    params = params_for(coordinator, {}, SVEN)
    assert params["max_charge_w"] == 70
    assert coordinator._charge_curve_limit({})[1] == "Ladekurve: Ladestand unbekannt, kleinste Kurvenleistung"


async def test_small_curve_power_keeps_a_small_window_with_a_minimum_charge(coordinator):
    """A 100 W minimum above a 70 W ceiling would close the SMA window (power_window)."""
    from custom_components.opti_akku.sma import power_window

    coordinator.settings["input_number.akkusteuerung_min_ladestaerke"] = 100
    states = {SOC: 100, TEMP: 22, POWER: 640, "sensor.opti_battery_capacity_kwh": 12.8}
    params = params_for(coordinator, states, SVEN)
    assert params["charge_power_w"] == params["min_charge_w"] == 70
    assert power_window(int(params["charge_power_w"]), int(params["min_charge_w"])) == (0, 70)


async def test_hold_follows_real_cycles_and_names_the_release_point(coordinator):
    with_curve(coordinator, SVEN)
    reads = coordinator.device.async_read.return_value
    notes = []
    for soc in (98, 99, 98, 97):
        reads[SOC] = soc
        data = await coordinator._async_update_data()
        notes.append([n for n in data["reason_details"]["charge_power_limits"] if n.startswith("Ladekurve")])
    assert notes == [
        # Start without memory at 98 %: the 99 % point is within its band.
        # Same text while the binding point stays, inside and above its band.
        ["Ladekurve ab 99 % SoC: Obergrenze aktiv (frei ab 97 %)"],
        ["Ladekurve ab 99 % SoC: Obergrenze aktiv (frei ab 97 %)"],
        ["Ladekurve ab 99 % SoC: Obergrenze aktiv (frei ab 97 %)"],
        ["Ladekurve ab 98 % SoC: Obergrenze aktiv (frei ab 96 %)"],
    ]
    assert coordinator._charge_curve_held == frozenset({85, 93, 98})
    assert coordinator._parameters(data["states"])["max_charge_w"] == 3000


async def test_restart_inside_the_band_starts_strict(hass, coordinator):
    """A new coordinator has no hold; it may only be stricter than before."""
    with_curve(coordinator, SVEN)
    reads = coordinator.device.async_read.return_value
    reads[SOC] = 98
    await coordinator._async_update_data()
    assert coordinator._parameters({SOC: 98, TEMP: 22, POWER: 2000,
                                    "sensor.opti_battery_capacity_kwh": 12.8})["max_charge_w"] == 250
    assert coordinator._charge_curve_held == frozenset({85, 93, 98, 99})


async def test_unknown_soc_forgets_the_hold(coordinator):
    with_curve(coordinator, SVEN)
    coordinator._charge_curve_held = frozenset({85, 93, 98, 99, 100})
    coordinator._update_charge_curve_hold({})
    assert coordinator._charge_curve_held is None
    coordinator._charge_curve_held = frozenset({85})
    with_curve(coordinator, [point(85, 3000), point(93, 4000)])
    coordinator._update_charge_curve_hold({SOC: 90})
    assert coordinator._charge_curve_held is None
    assert coordinator._charge_curve_limit({SOC: 90}) == (0.0, "Ladekurve ungültig: Laden gesperrt")


@pytest.mark.parametrize("held", [None, frozenset(), frozenset({85, 93, 98, 99, 100})])
@pytest.mark.parametrize("soc", [84, 92, 98, 99])
async def test_hold_keeps_the_minimum_with_every_other_limit(coordinator, held, soc):
    """Whatever is held, every charge parameter stays <= the value without curve."""
    coordinator.settings["input_number.akkusteuerung_max_ladestaerke"] = 5000
    coordinator.settings["input_number.akkusteuerung_min_ladestaerke"] = 100
    states = {SOC: soc, TEMP: 46, POWER: 1800, "sensor.opti_battery_capacity_kwh": 12.8}
    for manual in (None, "Akku schnell Laden"):
        coordinator.manual_mode = manual
        before = params_for(coordinator, states, None)
        after = params_for(coordinator, states, SVEN, held)
        cap = curve_limit(parse_curve(SVEN), soc, held)
        for key in CHARGE_KEYS:
            assert after[key] == (before[key] if cap is None else min(before[key], cap[0]))


async def test_a_failed_cycle_forgets_the_hold(coordinator):
    """A cycle that fails before advancing the hold must not keep a milder one."""
    with_curve(coordinator, SVEN)
    reads = coordinator.device.async_read.return_value
    for soc in (95, 98):
        reads[SOC] = soc
        await coordinator._async_update_data()
    assert coordinator._charge_curve_held == frozenset({85, 93, 98})
    with patch.object(coordinator.engine, "evaluate", side_effect=RuntimeError("boom")):
        data = await coordinator._async_update_data()
    assert data["decision_id"] == "internal_error"
    assert coordinator._charge_curve_held is None
    reads[SOC] = 98
    await coordinator._async_update_data()
    assert coordinator._charge_curve_held == frozenset({85, 93, 98, 99})
    assert coordinator._parameters({SOC: 98})["max_charge_w"] == 250


def test_note_for_a_point_without_release_band():
    from custom_components.opti_akku.coordinator import OptiCoordinator as C

    class Fake:
        entry = type("E", (), {"options": {"charge_curve": [point(1, 500)]}})()
        _charge_curve_held = None
        _parse_charge_curve = C._parse_charge_curve

    assert C._charge_curve_limit(Fake(), {SOC: 50}) == (500.0, "Ladekurve ab 1 % SoC: Obergrenze aktiv")
