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


def pv(kwh_by_local_hour):
    return {ENTITY: {"status": "ready", "quantile": "p10",
                     "hours": {hour_key(h): kwh for h, kwh in kwh_by_local_hour.items()}}}


def run(pv_hours=None, *, overrides=None, model=MODEL, mode="Akku Dynamisch"):
    values = {"input_select.akkusteuerung_modus": mode, **(overrides or {})}
    return evaluate(W1, current=27, model=model, overrides=values,
                    attributes=pv(pv_hours) if pv_hours is not None else None)


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
                      attributes={ENTITY: {"status": "ready", "hours": hours}})
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


def _surplus(engine, at, export, imported, battery):
    values = {"sensor.opti_grid_export_w": export, "sensor.opti_grid_import_w": imported,
              "sensor.opti_battery_power_w": battery, "input_boolean.akku_opti_automatik": "off"}
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


def test_surplus_sensor_is_off_without_measurements():
    engine = _surplus_engine()
    t0 = dt.datetime(2026, 1, 14, 12, 0, tzinfo=TZ)
    assert _surplus(engine, t0, 3000, None, 0) == "off"
    assert _surplus(engine, t0 + dt.timedelta(seconds=120), 3000, None, 0) == "off"


# -- forecast hours from Solcast ------------------------------------------------

NOW = dt.datetime(2026, 1, 14, 13, 5, tzinfo=TZ)
HOUR = NOW.astimezone(UTC).replace(minute=0)


def solcast(start, rows, updated=NOW):
    detail = [{"period_start": (start + dt.timedelta(minutes=30 * i)).isoformat(),
               **({"pv_estimate10": p10} if p10 is not None else {}), "pv_estimate": 2.0}
              for i, p10 in enumerate(rows)]
    return SimpleNamespace(state="1", attributes={"detailedForecast": detail},
                           last_reported=updated, last_updated=updated)


def states(**entities):
    return entities.get


def test_forecast_hours_are_contiguous_p10_from_the_current_hour():
    # 12:00 UTC (past), then 13, 14 local; 15 local half covered -> gap.
    entity = solcast(HOUR - dt.timedelta(hours=1), [1, 1, 0.5, 0.5, 0.25, 0.75, 2.0])
    status, attrs = pv_forecast_hours(states(today=entity), [("today", None)], NOW, max_age=3600)
    assert status == "ready"
    assert attrs == {"status": "ready", "quantile": "p10", "source": "today",
                     "hours": {str(int(HOUR.timestamp())): 0.5,
                               str(int((HOUR + dt.timedelta(hours=1)).timestamp())): 0.5}}


def test_forecast_hours_use_tomorrow_and_stop_at_a_missing_p10():
    today = solcast(HOUR, [1, 1])
    tomorrow = solcast(HOUR + dt.timedelta(hours=1), [0.2, 0.2, None, 1])
    status, attrs = pv_forecast_hours(states(t=today, m=tomorrow), [("t", "m")], NOW, max_age=3600)
    assert status == "ready"
    assert list(attrs["hours"].values()) == [1.0, 0.2]


def test_forecast_hours_fall_back_to_the_next_source_pair():
    stale = solcast(HOUR, [1, 1], updated=NOW - dt.timedelta(hours=5))
    fresh = solcast(HOUR, [0.4, 0.4])
    status, attrs = pv_forecast_hours(states(a=stale, b=fresh), [(None, None), ("a", None), ("b", None)],
                                      NOW, max_age=3600)
    assert (status, attrs["source"], list(attrs["hours"].values())) == ("ready", "b", [0.4])
    status, attrs = pv_forecast_hours(states(a=stale), [("a", None)], NOW, max_age=3600)
    assert (status, attrs["hours"]) == ("stale", {})


def test_forecast_hours_cap_the_horizon_and_handle_no_source():
    long = solcast(HOUR, [0.1] * 96)
    _, attrs = pv_forecast_hours(states(a=long), [("a", None)], NOW, max_age=3600)
    # From the current hour up to now + 37 h.
    assert len(attrs["hours"]) == 38
    assert pv_forecast_hours(states(), [(None, None)], NOW, max_age=3600) == (
        "no_forecast", {"status": "no_forecast", "quantile": "p10", "source": None, "hours": {}})


def test_forecast_hours_without_p10_in_the_current_hour_are_no_forecast():
    entity = solcast(HOUR, [None, None, 1, 1])
    status, attrs = pv_forecast_hours(states(a=entity), [("a", None)], NOW, max_age=3600)
    assert (status, attrs["hours"]) == ("no_forecast", {})
