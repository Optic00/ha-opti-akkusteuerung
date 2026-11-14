"""Peak precharging credits the PV that reaches the battery before the peak (#155).

Fixture F (tests/precharge_fixture.py), profile W1: 14.01.2026 13:05, valley
12-15 h is the current price, peaks 17-20 h, SoC 12, reserve 25.8 %, 12.8 kWh,
fixed load 0.8 kW, default battery model (charge efficiency 0.9).
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.opti_akku.battery_model import BatteryModel
from custom_components.opti_akku.precharge_pv import ENTITY, pv_forecast_hours
from tests.precharge_fixture import ENGINE, day_prices, evaluate

TZ = ZoneInfo("Europe/Berlin")
UTC = dt.UTC
MODEL = BatteryModel.from_options(None).engine_state()
W1 = day_prices(33, 27, [((17, 18), 45), ((18, 19), 60), ((19, 20), 50)])
RESERVE = 25.8


def hour_key(local_hour, day=14):
    return str(int(dt.datetime(2026, 1, day, local_hour, tzinfo=TZ).timestamp()))


def pv(kw_by_local_hour, halfhours=None):
    """Mean P10 power per local hour, spread over both half hours; ``halfhours`` adds
    ((hour, minute), kW) entries."""
    rows = {}
    for h, kw in kw_by_local_hour.items():
        rows[hour_key(h)] = kw
        rows[str(int(hour_key(h)) + 1800)] = kw
    for (h, minute), kw in (halfhours or {}).items():
        rows[str(int(hour_key(h)) + minute * 60)] = kw
    return {ENTITY: {"status": "ready", "quantile": "p10", "halfhours": rows}}


def run(pv_hours=None, *, overrides=None, model=MODEL, mode="Akku Dynamisch", halfhours=None,
        minute=5):
    values = {"input_select.akkusteuerung_modus": mode, **(overrides or {})}
    return evaluate(W1, current=27, model=model, overrides=values, minute=minute,
                    attributes=pv(pv_hours, halfhours) if pv_hours is not None else None)


def reserve(result):
    return result.attributes["sensor.opti_peak_reserve_soc"]


def reason(result):
    return result.attributes["sensor.opti_strategie_vorschau"]["grund"]


def precharges(result):
    charged = result.decision_id == "peak_precharge"
    assert reason(result).startswith("Peak-Vorladen") is charged
    return charged


# -- PV credit in the reserve sensor ---------------------------------------------

def test_without_forecast_precharge_is_unchanged():
    result = run()
    assert precharges(result)
    attrs = reserve(result)
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == RESERVE
    assert attrs["vorlade_ziel_soc"] == RESERVE
    assert attrs["pv_vor_peak_kwh"] == 0
    assert attrs["pv_prognose"] == "fehlt"
    assert reason(result) == "Peak-Vorladen (51.67 ct Spitze, Mindestpreis 36.7 ct, bis 25.8%)"


def test_p10_surplus_that_covers_the_gap_prevents_precharge():
    # 1.3 kWh/h P10 against 0.8 kW load: 0.5 kW surplus for 3 h 55 min until
    # 17:00 = 1.96 kWh, 1.76 kWh stored = 13.8 % of 12.8 kWh. Gap 12 -> 25.8 %.
    result = run({13: 1.3, 14: 1.3, 15: 1.3, 16: 1.3, 17: 1.3})
    attrs = reserve(result)
    assert attrs["pv_vor_peak_kwh"] == pytest.approx(1.96, abs=0.01)
    assert attrs["vorlade_ziel_soc"] == pytest.approx(RESERVE - 13.8, abs=0.1)
    assert attrs["pv_prognose"] == "p10"
    assert not precharges(result)
    # The reserve itself stays and is held; PV charges on top (fixture score 0
    # makes the low-forecast hold win before L4, both are "nur Laden").
    assert float(result.states["sensor.opti_peak_reserve_soc"]) == RESERVE
    assert result.mode == "Akku nur Laden"


def test_partial_cover_lowers_the_precharge_target():
    # 0.2 kW surplus * 3.917 h = 0.78 kWh, 0.705 kWh stored = 5.5 %.
    result = run({13: 1.0, 14: 1.0, 15: 1.0, 16: 1.0})
    attrs = reserve(result)
    assert attrs["vorlade_ziel_soc"] == pytest.approx(20.3, abs=0.1)
    assert precharges(result)
    assert reason(result) == (
        "Peak-Vorladen (51.67 ct Spitze, Mindestpreis 36.7 ct, bis 20.3%, "
        "PV deckt 5.5% bis zur Spitze)")


def test_running_precharge_stops_at_the_lowered_target():
    # Stopband 0 while charging: 20.2 < 20.3 keeps charging, 20.4 stops.
    hours = {13: 1.0, 14: 1.0, 15: 1.0, 16: 1.0}
    assert precharges(run(hours, mode="Akku Netzladen", overrides={"sensor.opti_soc": 20.2}))
    assert not precharges(run(hours, mode="Akku Netzladen", overrides={"sensor.opti_soc": 20.4}))


def test_pv_after_the_peak_start_does_not_count():
    result = run({13: 0.0, 14: 0.0, 15: 0.0, 16: 0.0, 17: 5.0, 18: 5.0, 19: 5.0})
    assert reserve(result)["pv_vor_peak_kwh"] == 0
    assert reserve(result)["vorlade_ziel_soc"] == RESERVE
    assert precharges(result)


def test_half_hours_are_credited_only_for_their_remaining_part():
    """Codex review: a sunny first half hour must not be credited after it has passed."""
    # 13:00-13:30 at 5 kW, then no PV. At 13:05: 25 min of 4.2 kW surplus, capped at
    # the default 3 kW charge power = 1.25 kWh.
    early = run({}, halfhours={(13, 0): 5.0, (13, 30): 0.0, (14, 0): 0.0, (14, 30): 0.0,
                               (15, 0): 0.0, (15, 30): 0.0, (16, 0): 0.0, (16, 30): 0.0})
    assert reserve(early)["pv_vor_peak_kwh"] == pytest.approx(1.25, abs=0.01)
    # At 13:35 the sunny half hour is over: nothing left to credit.
    late = run({}, minute=35, halfhours={(13, 0): 5.0, (13, 30): 0.0, (14, 0): 0.0, (14, 30): 0.0,
                                         (15, 0): 0.0, (15, 30): 0.0, (16, 0): 0.0, (16, 30): 0.0})
    assert reserve(late)["pv_vor_peak_kwh"] == 0
    assert reserve(late)["vorlade_ziel_soc"] == RESERVE
    assert precharges(late)


def test_hour_without_forecast_counts_as_zero():
    # Only 13 h is known: 0.5 kW * 55 min = 0.46 kWh. The rest counts as 0.
    result = run({13: 1.3})
    attrs = reserve(result)
    assert attrs["pv_prognose"] == "luecke"
    assert attrs["pv_vor_peak_kwh"] == pytest.approx(0.46, abs=0.01)
    assert precharges(result)


def test_surplus_is_limited_by_the_maximum_charge_power():
    hours = {13: 5.0, 14: 5.0, 15: 5.0, 16: 5.0}
    assert not precharges(run(hours))
    # 100 W for 3.917 h = 0.39 kWh, not 16.6 kWh.
    limited = run(hours, overrides={"input_number.akkusteuerung_max_ladestaerke": 100})
    assert reserve(limited)["pv_vor_peak_kwh"] == pytest.approx(0.39, abs=0.01)
    assert precharges(limited)


@pytest.mark.parametrize("model_attrs", [
    pytest.param({k: v for k, v in MODEL[1].items() if k != "charge_efficiency"}, id="missing"),
    pytest.param({**MODEL[1], "charge_efficiency": 0}, id="zero"),
    pytest.param({**MODEL[1], "charge_efficiency": 1.5}, id="above_one"),
])
def test_without_valid_charge_efficiency_no_pv_is_credited(model_attrs):
    result = run({13: 5.0, 14: 5.0, 15: 5.0, 16: 5.0}, model=(MODEL[0], model_attrs))
    assert reserve(result)["vorlade_ziel_soc"] == RESERVE
    assert precharges(result)


@pytest.mark.parametrize("hours", [None, "x", []])
def test_malformed_forecast_hours_fall_back_to_the_full_reserve(hours):
    result = evaluate(W1, current=27, model=MODEL,
                      attributes={ENTITY: {"status": "ready", "halfhours": hours}})
    assert reserve(result)["pv_prognose"] == "fehlt"
    assert reserve(result)["vorlade_ziel_soc"] == RESERVE
    assert precharges(result)


# -- running PV surplus blocks the precharge ------------------------------------

def test_running_pv_surplus_blocks_precharge():
    result = run(overrides={"binary_sensor.opti_vorladen_pv_ueberschuss": "on"})
    assert not precharges(result)
    assert result.mode == "Akku nur Laden"
    assert precharges(run(overrides={"binary_sensor.opti_vorladen_pv_ueberschuss": "off"}))


def _surplus_engine():
    resource = ENGINE.load_resources()
    blocks = []
    for block in resource["template_blocks"]:
        sensors = [item for item in block.get("binary_sensor", [])
                   if item.get("unique_id") == "opti_vorladen_pv_ueberschuss"]
        if sensors:
            blocks.append({**{k: v for k, v in block.items() if k not in ("sensor", "binary_sensor")},
                           "binary_sensor": sensors})
    assert len(blocks) == 1
    resource["template_blocks"] = blocks
    resource["statistics"] = []
    resource["balancing_automations"] = []
    return ENGINE.StrategyEngine(resource)


def _surplus(engine, at, export, imported, battery, sun="above_horizon"):
    values = {"sensor.opti_grid_export_w": export, "sensor.opti_grid_import_w": imported,
              "sensor.opti_battery_power_w": battery, "input_boolean.akku_opti_automatik": "off",
              "sun.sun": sun}
    values = {k: v for k, v in values.items() if v is not None}
    return engine.evaluate(values, {}, at).states["binary_sensor.opti_vorladen_pv_ueberschuss"]


def test_surplus_sensor_measures_before_the_battery_with_delays():
    engine = _surplus_engine()
    t0 = dt.datetime(2026, 1, 14, 12, 0, tzinfo=TZ)
    # 300 W export while the battery idles: on after 60 s.
    assert _surplus(engine, t0, 300, 0, 0) == "off"
    assert _surplus(engine, t0 + dt.timedelta(seconds=61), 300, 0, 0) == "on"
    # Grid charging at 3 kW with 2.5 kW import: PV still covers the house by 500 W.
    assert _surplus(engine, t0 + dt.timedelta(seconds=90), 0, 2500, 3000) == "on"
    # Import for the house: off only after 300 s.
    assert _surplus(engine, t0 + dt.timedelta(seconds=120), 0, 400, 0) == "on"
    assert _surplus(engine, t0 + dt.timedelta(seconds=421), 0, 400, 0) == "off"


def test_small_permanent_surplus_locks_too():
    """Codex review: 150 W lasting surplus must lock, not only above 200 W."""
    engine = _surplus_engine()
    t0 = dt.datetime(2026, 1, 14, 12, 0, tzinfo=TZ)
    assert _surplus(engine, t0, 150, 0, 0) == "off"
    assert _surplus(engine, t0 + dt.timedelta(seconds=61), 150, 0, 0) == "on"
    # While grid charging: 3 kW into the battery, 2.9 kW import -> 100 W PV surplus.
    assert _surplus(engine, t0 + dt.timedelta(minutes=10), 0, 2900, 3000) == "on"


def test_meter_offset_during_grid_charging_does_not_lock():
    """Codex review round 3: 5 W offset between battery and grid metering while
    grid charging is noise, not PV surplus."""
    engine = _surplus_engine()
    t0 = dt.datetime(2026, 1, 14, 12, 0, tzinfo=TZ)
    assert _surplus(engine, t0, 0, 2995, 3000) == "off"
    assert _surplus(engine, t0 + dt.timedelta(minutes=10), 0, 2950, 3000) == "off"
    assert _surplus(engine, t0 + dt.timedelta(minutes=11), 0, 2940, 3000) == "off"
    assert _surplus(engine, t0 + dt.timedelta(minutes=12, seconds=1), 0, 2940, 3000) == "on"


def test_any_measured_export_locks_below_the_deadband():
    """Codex review round 4: real export of 50 W or less is still PV that leaves
    the house; grid charging must not run beside it."""
    engine = _surplus_engine()
    t0 = dt.datetime(2026, 1, 14, 12, 0, tzinfo=TZ)
    assert _surplus(engine, t0, 40, 0, 0) == "off"
    assert _surplus(engine, t0 + dt.timedelta(seconds=61), 40, 0, 0) == "on"
    # Export while the battery discharges: net surplus negative, export still locks.
    assert _surplus(engine, t0 + dt.timedelta(minutes=5), 20, 0, -500) == "on"
    # No export, offset within the deadband: off after the delay.
    assert _surplus(engine, t0 + dt.timedelta(minutes=6), 0, 2995, 3000) == "on"
    assert _surplus(engine, t0 + dt.timedelta(minutes=11, seconds=1), 0, 2995, 3000) == "off"


@pytest.mark.parametrize("sun", ["unavailable", "unknown", None])
def test_missing_measurements_with_unknown_sun_lock(sun):
    """Codex review round 3: an unavailable sun state counts as day."""
    engine = _surplus_engine()
    t0 = dt.datetime(2026, 1, 14, 12, 0, tzinfo=TZ)
    _surplus(engine, t0, None, 100, 0, sun=sun)
    assert _surplus(engine, t0 + dt.timedelta(seconds=61), None, 100, 0, sun=sun) == "on"


def test_missing_measurements_lock_by_day_and_not_at_night():
    """Codex review: a grid meter outage must not allow grid precharge during PV
    hours. At night there is no PV surplus; the precharge stays possible."""
    engine = _surplus_engine()
    t0 = dt.datetime(2026, 1, 14, 12, 0, tzinfo=TZ)
    assert _surplus(engine, t0, 3000, None, 0) == "off"
    day = _surplus(engine, t0 + dt.timedelta(seconds=61), 3000, None, 0)
    assert day == "on"
    assert not precharges(run(overrides={"binary_sensor.opti_vorladen_pv_ueberschuss": day}))
    night_engine = _surplus_engine()
    night = _surplus(night_engine, t0, None, None, 0, sun="below_horizon")
    night = _surplus(night_engine, t0 + dt.timedelta(seconds=61), None, None, 0, sun="below_horizon")
    assert night == "off"
    assert precharges(run(overrides={"binary_sensor.opti_vorladen_pv_ueberschuss": night}))


def test_l4_holds_the_full_reserve_above_the_lowered_target():
    """Codex review: L3/L4 must keep the full reserve, not the PV-lowered target.
    Good forecast so the low-forecast hold does not win before L4."""
    hours = {13: 1.3, 14: 1.3, 15: 1.3, 16: 1.3}
    overrides = {"sensor.opti_forecast_score": 8, "sensor.opti_forecast_score_tomorrow": 8,
                 "sensor.opti_soc": 20}
    result = run(hours, overrides=overrides)
    assert reserve(result)["vorlade_ziel_soc"] < 20 < RESERVE
    assert result.decision_id == "peak_l4"
    assert result.mode == "Akku nur Laden"


def test_pv_more_than_25_hours_ahead_still_counts():
    """Codex review: the reserve looks up to 36 h ahead; the PV forecast must too."""
    now = dt.datetime(2026, 1, 14, 12, 0, tzinfo=TZ)
    start = now.astimezone(UTC)
    entity = solcast(start, [0.0] * 52 + [1.0] * 8, updated=now)  # PV only 26-30 h ahead
    status, attrs = pv_forecast_hours({"a": entity}, [("a", None)], now, max_age=3600)
    assert status == "ready" and len(attrs["halfhours"]) == 60
    assert attrs["halfhours"][str(int((start + dt.timedelta(hours=27)).timestamp()))] == 1.0
    # Beyond 37 h nothing is published.
    far = solcast(start, [0.5] * 90, updated=now)
    _, attrs = pv_forecast_hours({"a": far}, [("a", None)], now, max_age=3600)
    assert len(attrs["halfhours"]) == 75


# -- forecast half hours from Solcast -------------------------------------------

NOW = dt.datetime(2026, 1, 14, 13, 5, tzinfo=TZ)
HALF = NOW.astimezone(UTC).replace(minute=0)


def solcast(start, rows, updated=NOW):
    detail = [{"period_start": (start + dt.timedelta(minutes=30 * i)).isoformat(),
               "pv_estimate10": p10, "pv_estimate": 2.0}
              for i, p10 in enumerate(rows)]
    return SimpleNamespace(state="1", attributes={"detailedForecast": detail},
                           last_reported=updated, last_updated=updated)


def key(offset_halfhours):
    return str(int((HALF + dt.timedelta(minutes=30 * offset_halfhours)).timestamp()))


def test_forecast_half_hours_are_contiguous_p10_from_now():
    # Past half hour dropped; 13:00 and 13:30 local kept; a gap ends the rows.
    entity = solcast(HALF - dt.timedelta(minutes=30), [1, 0.5, 0.25])
    gap = solcast(HALF + dt.timedelta(hours=2), [3.0])
    status, attrs = pv_forecast_hours({"today": entity, "tomorrow": gap},
                                      [("today", "tomorrow")], NOW, max_age=3600)
    assert status == "ready"
    assert attrs == {"status": "ready", "quantile": "p10", "source": "today",
                     "halfhours": {key(0): 0.5, key(1): 0.25}}


def test_forecast_half_hours_join_today_and_tomorrow():
    today = solcast(HALF, [1, 1])
    tomorrow = solcast(HALF + dt.timedelta(hours=1), [0.2, 0.3])
    _, attrs = pv_forecast_hours({"t": today, "m": tomorrow}, [("t", "m")], NOW, max_age=3600)
    assert list(attrs["halfhours"].values()) == [1.0, 1.0, 0.2, 0.3]


def test_the_source_reaching_furthest_wins():
    """Codex review: a source covering only the current hour must not shadow a longer one."""
    short = solcast(HALF, [5.0, 5.0])
    long = solcast(HALF, [0.4] * 10)
    stale = solcast(HALF, [9.0] * 20, updated=NOW - dt.timedelta(hours=5))
    states = {"short": short, "long": long, "stale": stale}
    status, attrs = pv_forecast_hours(states, [(None, None), ("stale", None), ("short", None),
                                               ("long", None)], NOW, max_age=3600)
    assert (status, attrs["source"], len(attrs["halfhours"])) == ("ready", "long", 10)
    # On a tie the first pair wins.
    _, attrs = pv_forecast_hours({"a": short, "b": solcast(HALF, [1, 1])}, [("a", None), ("b", None)],
                                 NOW, max_age=3600)
    assert attrs["source"] == "a"


def test_no_usable_forecast_is_no_forecast():
    stale = solcast(HALF, [1, 1], updated=NOW - dt.timedelta(hours=5))
    later = solcast(HALF + dt.timedelta(hours=1), [1, 1])
    broken = solcast(HALF, [1, 1])
    broken.attributes["detailedForecast"][0]["pv_estimate10"] = "x"
    for entity in (stale, later, broken):
        assert pv_forecast_hours({"a": entity}, [("a", None)], NOW, max_age=3600) == (
            "no_forecast", {"status": "no_forecast", "quantile": "p10", "source": None, "halfhours": {}})
    assert pv_forecast_hours({}, [(None, None)], NOW, max_age=3600)[0] == "no_forecast"
