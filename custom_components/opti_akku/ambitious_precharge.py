"""Ambitious grid precharge before sunrise (experimental, issue #222).

The peak precharge of the strategy charges from the grid only for the price
peaks up to a short horizon after sunrise and only up to the reserve minus
the PV that reaches the battery before the peak. Users who expect little PV
for the whole day asked for the opposite end: charge in the cheapest night
hours as far as the battery can still take the forecast PV surplus of the
coming day without exporting it, as long as that pays off against the prices
of the day.

This module plans exactly that, only with the option ``ambitious_precharge``
and the user's acknowledgement of the risk, and only at night:

- The coming day is simulated from sunrise to the end of the known prices in
  the slots of the price series, split at half hours: PV forecast (P10 or
  median) minus the load profile charges the battery up to maxSoC and exports
  the rest at the feed-in tariff; a deficit discharges it and buys the rest
  at the slot price.
- For every start energy at sunrise in steps of one percent of the capacity,
  the day costs plus the grid energy, wear and a minimum gain for the
  energy charged tonight are compared. The cheapest start energy wins, the
  lower one on a tie. Energy that would only be exported or only replace
  cheap grid power is therefore not charged.
- The energy is bought in the cheapest slots between now and sunrise at the
  maximum charge power; the night load after the window is added to the
  target.

Missing or invalid inputs plan nothing (fail closed): the strategy then
decides as without the option. The result only ever replaces a low-priority
holding or discharging decision by "Akku Netzladen"; protections, balancing,
EV locks, manual modes and every charging decision stay untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from .arbitrage import _instant, _price_periods
from .battery_model import BatteryModel
from .peak_rank import _number
from .planner_shadow import _hour, pv_forecast_hourly

OPTION = "ambitious_precharge"
DECISION = "ambitious_precharge"
GRID_CHARGE_MODE = "Akku Netzladen"
HOLD_MODE = "Akku nur Laden"
QUANTILES = ("p10", "p50")
DEFAULT_QUANTILE = "p10"
DEFAULT_MIN_GAIN_CT = 2.0
MIN_GAIN_RANGE = (0.0, 50.0)
# Decisions of the strategy that hold or discharge for no protective reason.
# Grid charging there only adds energy that the plan expects to pay off.
OVERRIDABLE_DECISIONS = frozenset({
    "default", "below_target", "above_target", "peak_l3", "peak_l4", "extreme_peak_hold",
    "reserve_winter", "reserve_today_low", "reserve_cheap", "minimum_soc",
})
# Start below the target by this many percent, then charge up to the target,
# as the stop band of the grid branches in decisions.yaml.
START_BAND = 3.0
# (highest score, ((SoC below, share of the capacity per hour), ...)) as in
# sensor.opti_charge_power_w; the last band applies above the listed SoCs.
SCORE_BANDS: tuple[tuple[float, tuple[tuple[float, float], ...]], ...] = (
    (1, ((40, 0.40), (70, 0.30), (90, 0.20), (97, 0.10), (101, 0.05))),
    (4, ((35, 0.35), (65, 0.25), (87, 0.15), (97, 0.08), (101, 0.05))),
    (float("inf"), ((30, 0.30), (60, 0.20), (85, 0.15), (97, 0.08), (101, 0.05))),
)
# A running window keeps its slot while its price stays this close to the
# most expensive selected slot, so a shrinking need does not toggle the mode.
PRICE_BAND_CT = 0.5
STEP_PERCENT = 1.0
# At most this many window price limits are tried, spread over the night prices.
MAX_THRESHOLDS = 12
# A plan must beat doing nothing by more than this, so rounding never switches modes.
MIN_PLAN_GAIN_CT = 1.0
HALF_HOUR = timedelta(minutes=30)
HORIZON = timedelta(hours=24)
MAX_LOAD_W = 50000.0


def strategy_charge_w(*, soc: float, temperature: Any, capacity_kwh: float, max_w: Any,
                      score: Any, manual_limit: bool) -> float:
    """Charge power of ``sensor.opti_charge_power_w`` at a SoC (templates.yaml).

    Mirrors the template's temperature derating, forecast score bands and
    manual limit, without the PV peak release and the balancing taper, which
    do not apply to a night grid charge. ``test_ambitious_precharge`` checks
    it against the strategy engine.
    """
    temp = _number(temperature)
    temp = 25.0 if temp is None else temp
    if temp >= 50 or temp <= -5:
        return 0.0
    factor = 0.5 if temp >= 45 else (0.25 if temp <= 0 else 1.0)
    level = _number(score)
    band = 10 if level is None else int(level)
    cap = capacity_kwh * 1000
    limit = _number(max_w)
    max_helper = 3000.0 if limit is None else limit
    bands = next(shares for top, shares in SCORE_BANDS if band <= top)
    rate = next((share for upper, share in bands if soc < upper), bands[-1][1])
    c = cap * rate
    if manual_limit:
        max_helper = min(max(max_helper, 0.0), 10000.0)
        c = min(max_helper, c) if soc >= 97 else max_helper
    return float(round(min(c * factor, max_helper)))


def option_config(options: Any) -> dict[str, Any] | None:
    """Validated option, or None when the option is off or incomplete."""
    cfg = options.get(OPTION) if isinstance(options, Mapping) else None
    if not isinstance(cfg, Mapping) or cfg.get("enabled") is not True:
        return None
    if cfg.get("risk_accepted") is not True:
        return None
    quantile = cfg.get("quantile", DEFAULT_QUANTILE)
    gain = _number(cfg.get("min_gain_ct", DEFAULT_MIN_GAIN_CT))
    if quantile not in QUANTILES or gain is None or not MIN_GAIN_RANGE[0] <= gain <= MIN_GAIN_RANGE[1]:
        return None
    return {"quantile": quantile, "min_gain_ct": gain}


def _key(at: datetime, seconds: int) -> str:
    stamp = int(at.timestamp())
    return str(stamp - stamp % seconds)


def _load_kw(load_hours: Any, at: datetime, fallback_kw: float) -> float:
    watts = _number(load_hours.get(_key(at, 3600))) if isinstance(load_hours, Mapping) else None
    if watts is None or not 0 <= watts <= MAX_LOAD_W:
        return fallback_kw
    return float(watts) / 1000


def _pieces(periods: Sequence[tuple[datetime, datetime, float]], start: datetime,
            end: datetime) -> list[tuple[datetime, float, float, datetime]]:
    """(start, hours, price, slot start) of the price slots in [start, end), cut at half hours."""
    pieces: list[tuple[datetime, float, float, datetime]] = []
    for slot_start, slot_end, price in periods:
        cursor = max(slot_start, start)
        stop = min(slot_end, end)
        while cursor < stop:
            stamp = int(cursor.timestamp())
            boundary = datetime.fromtimestamp(stamp - stamp % 1800, UTC) + HALF_HOUR
            piece_end = min(boundary, stop)
            pieces.append((cursor, (piece_end - cursor).total_seconds() / 3600, price, slot_start))
            cursor = piece_end
    return pieces


def _simulate(start_kwh: float, steps: Sequence[tuple[float, float, float, bool]], *,
              target_kwh: float, room_kwh: float, charge_kw: Callable[[float], float],
              discharge_kw: float, model: BatteryModel, feed_in: float,
              gain_ct: float) -> tuple[float, float, float, float]:
    """Cost in ct, exported kWh, grid-charged AC kWh and its price in ct to the end of the prices.

    ``charge_kw`` gives the charge power for a stored energy (charge curve),
    ``discharge_kw`` caps the discharge power.

    In a window slot the grid supplies the house and the battery charges
    from the grid at ``charge_kw`` up to ``target_kwh``; above the target it
    holds ("Akku Netzladen", then "Akku nur Laden"). Everywhere else the
    battery follows the PV balance: surplus charges up to maxSoC and the rest
    is exported, a deficit discharges down to MinSOC and the rest is bought.
    Each grid kWh carries the minimum gain, each stored grid kWh wear and
    margin.
    """
    energy, cost, exported, bought, paid = start_kwh, 0.0, 0.0, 0.0, 0.0
    wear, margin = model.throughput_cost_ct_kwh, model.margin_ct
    eta_c, eta_d = model.charge_efficiency, model.discharge_efficiency
    for hours, net_kw, price, window in steps:
        net = net_kw * hours
        if window:
            stored = max(0.0, min(charge_kw(energy) * hours * eta_c, target_kwh - energy))
            energy += stored
            bought += stored / eta_c
            paid += stored / eta_c * price
            cost += stored / eta_c * (price + gain_ct) + stored * (wear + margin)
            cost += max(0.0, -net) * price - max(0.0, net) * feed_in
            exported += max(0.0, net)
        elif net >= 0:
            stored = max(0.0, min(min(net, charge_kw(energy) * hours) * eta_c, room_kwh - energy))
            export = net - stored / eta_c
            energy += stored
            exported += export
            cost += stored * wear - export * feed_in
        else:
            delivered = min(-net, energy * eta_d, discharge_kw * hours)
            energy -= delivered / eta_d
            cost += delivered / eta_d * wear + (-net - delivered) * price
    return cost, exported, bought, paid


def _contiguous(periods: Sequence[tuple[datetime, datetime, float]],
                now: datetime) -> list[tuple[datetime, datetime, float]]:
    """The price slots from the one covering ``now`` up to the first gap."""
    out: list[tuple[datetime, datetime, float]] = []
    for slot in periods:
        if slot[1] <= now:
            continue
        if not out:
            if not slot[0] <= now < slot[1]:
                return []
        elif slot[0] != out[-1][1]:
            break
        out.append(slot)
    return out


def _thresholds(night: Sequence[tuple[datetime, datetime, float]]) -> list[float]:
    """Window price limits: the distinct night prices, at most :data:`MAX_THRESHOLDS`."""
    prices = sorted({slot[2] for slot in night})
    if len(prices) <= MAX_THRESHOLDS:
        return prices
    last = len(prices) - 1
    return sorted({prices[round(i * last / (MAX_THRESHOLDS - 1))] for i in range(MAX_THRESHOLDS)})


# Why no PV time series is available, in diagnostic order (#262).
PV_SOURCE_STATUSES = (
    "not_configured", "source_unavailable", "no_detailed_forecast", "stale", "invalid",
)
_UNUSABLE_STATES = (None, "unknown", "unavailable")


def pv_timeline(source_pairs: Sequence[Sequence[Any]], now: datetime,
                max_age: float) -> tuple[str, dict[datetime, tuple[float | None, float | None]]]:
    """Hourly PV time series of the first usable source pair, else why there is none.

    ``source_pairs`` lists the states (``None`` for a missing entity) of the
    configured today/tomorrow forecast entities per source, first choice
    first; an empty pair means that source is not configured. The ambitious
    precharge needs the Solcast interval rows (``detailedForecast`` or
    ``detailedHourly``); daily kWh alone cannot place PV in the day. The
    reason of the first configured source is returned, so the diagnostics
    point at the source the user set up first.
    """
    reason = "not_configured"
    for states in source_pairs:
        if not states:
            continue
        status, hours = pv_forecast_hourly(states, now, max_age=max_age)
        if status == "ready":
            return "ready", hours
        if status == "no_forecast":
            usable = any(state is not None and getattr(state, "state", None) not in _UNUSABLE_STATES
                         for state in states)
            status = "no_detailed_forecast" if usable else "source_unavailable"
        if reason == "not_configured":
            reason = status
    return reason, {}


def plan(
    *,
    now: datetime,
    timezone: tzinfo,
    price_series: Any,
    pv_hours: Any,
    quantile: str,
    load_hours: Any,
    fallback_load_kw: Any,
    soc: Any,
    capacity_kwh: Any,
    minsoc: Any,
    maxsoc: Any,
    max_charge_w: Any,
    max_discharge_w: Any = None,
    charge_power: Callable[[float], float] | None = None,
    feed_in_ct: Any,
    sunrise: Any,
    sunset: Any,
    model: BatteryModel,
    min_gain_ct: float,
    charging: bool = False,
    pv_source_status: str | None = None,
) -> dict[str, Any]:
    """Plan of the ambitious precharge; never raises for invalid inputs."""
    rising, setting = _instant(sunrise), _instant(sunset)
    values = {name: _number(value) for name, value in (
        ("soc", soc), ("capacity", capacity_kwh), ("minsoc", minsoc), ("maxsoc", maxsoc))}
    if any(value is None for value in values.values()):
        return {"status": "data_missing", "detail": next(k for k, v in values.items() if v is None)}
    soc_now, capacity = float(values["soc"] or 0), float(values["capacity"] or 0)
    low, high = float(values["minsoc"] or 0), float(values["maxsoc"] or 0)
    if capacity <= 0 or not 0 <= low < high <= 100 or not 0 <= soc_now <= 100:
        return {"status": "data_missing", "detail": "battery"}
    if not model.economics_valid:
        return {"status": "battery_model_invalid"}
    if rising is None or setting is None or not now < rising < setting:
        return {"status": "daylight" if rising is not None and setting is not None else "data_missing",
                "detail": "sun"}
    periods = _contiguous(_price_periods(price_series, now, timezone), now)
    if not periods or periods[-1][1] < setting:
        return {"status": "price_horizon_short"}
    if quantile not in QUANTILES:
        return {"status": "pv_forecast_missing", "detail": "quantile"}
    if not isinstance(pv_hours, Mapping) or not pv_hours:
        reason = pv_source_status if pv_source_status in PV_SOURCE_STATUSES else "no_forecast"
        return {"status": "pv_forecast_missing", "detail": reason}
    index = QUANTILES.index(quantile)
    fallback = _number(fallback_load_kw)
    fallback = fallback if fallback is not None and 0 <= fallback <= MAX_LOAD_W / 1000 else 0.8
    charge_w = _number(max_charge_w)
    charge_kw = charge_w / 1000 if charge_w is not None and charge_w > 0 else capacity / 2
    discharge_w = _number(max_discharge_w)
    discharge_kw = discharge_w / 1000 if discharge_w is not None and discharge_w > 0 else capacity / 2
    feed_in = value if (value := _number(feed_in_ct)) is not None else 0.0

    # One day from sunrise: later days would need PV forecasts that may be missing.
    end_of_prices = min(periods[-1][1], rising + HORIZON)
    pieces: list[tuple[datetime, float, float, float]] = []
    for at, hours, price, slot in _pieces(periods, now, end_of_prices):
        pair = pv_hours.get(_hour(at))
        value = _number(pair[index]) if isinstance(pair, Sequence) and len(pair) == 2 else None
        if value is None or value < 0:
            if rising <= at < setting:
                return {"status": "pv_forecast_missing", "detail": "daylight_gap", "at": at.isoformat()}
            value = 0.0
        pieces.append((slot, hours, value - _load_kw(load_hours, at, fallback), price))
    # Only slots that end by sunrise: the plan never grid-charges into the morning.
    night = [slot for slot in periods if slot[1] > now and slot[1] <= rising]

    room = (high - low) / 100 * capacity
    stored_now = min(room, max(0.0, (soc_now - low) / 100 * capacity))

    def charge_at(energy: float) -> float:
        """Charge power in kW at a stored energy, as the strategy would command it."""
        if charge_power is None:
            return charge_kw
        return max(0.0, min(charge_kw, charge_power(low + energy / capacity * 100) / 1000))
    step = capacity * STEP_PERCENT / 100
    targets = [min(room, i * step) for i in range(int(room / step) + 1)] + [room]

    def run(limit: float | None, target: float) -> tuple[float, float, float, float]:
        window = {slot[0] for slot in night if limit is not None and slot[2] <= limit}
        steps = [(hours, net, price, at in window) for at, hours, net, price in pieces]
        return _simulate(stored_now, steps, target_kwh=target, room_kwh=room, charge_kw=charge_at,
                         discharge_kw=discharge_kw, model=model, feed_in=feed_in,
                         gain_ct=min_gain_ct)

    # Every window of the night slots up to a price limit, with every target:
    # below the target the window charges, above it the battery holds. The
    # cheapest plan wins; on a tie the smaller window and the lower target.
    # It must beat doing nothing by more than MIN_PLAN_GAIN_CT.
    baseline = run(None, 0.0)
    limit_found: float | None = None
    best_target, best = 0.0, baseline
    for limit in _thresholds(night):
        for target in dict.fromkeys(targets):
            outcome = run(limit, target)
            if outcome[0] < best[0]:
                limit_found, best_target, best = limit, target, outcome
    if best[0] >= baseline[0] - MIN_PLAN_GAIN_CT:
        limit_found = None
    chosen = [slot for slot in night if limit_found is not None and slot[2] <= limit_found]

    report: dict[str, Any] = {
        "sunrise": rising.isoformat(), "sunset": setting.isoformat(),
        "charge_kw": round(charge_kw, 3),
        "discharge_kw": round(discharge_kw, 3),
        "horizon_end": end_of_prices.isoformat(),
        "expected_export_kwh_without": round(baseline[1], 2),
    }
    if not chosen:
        return {**report, "status": "not_worth_it", "target_soc": None}
    target_soc = round(low + max(best_target, stored_now) / capacity * 100, 1)
    # What the planned grid energy costs; a pure hold buys none.
    price = best[3] / best[2] if best[2] > 0 else min(slot[2] for slot in chosen)
    # Report the gain without the minimum gain the choice demanded.
    saving_ct = baseline[0] - best[0] + best[2] * min_gain_ct
    window = chosen
    current = next((slot for slot in night if slot[0] <= now < slot[1]), None)
    highest = max(slot[2] for slot in window)
    in_window = current is not None and (current in window or (
        charging and current[2] <= highest + PRICE_BAND_CT))
    below = soc_now < target_soc - (0.0 if charging else START_BAND)
    action = ("charge" if below else "hold") if in_window else None
    return {
        **report,
        "status": "active" if action else "planned",
        "active": action is not None,
        "action": action,
        "target_soc": target_soc,
        "charge_price_ct": round(price, 2),
        "expected_grid_kwh": round(best[2], 2),
        "slots": [slot[0].isoformat() for slot in window],
        "first_at": window[0][0].isoformat(),
        "window_end": max(slot[1] for slot in window).isoformat(),
        "expected_export_kwh": round(best[1], 2),
        "expected_saving_eur": round(saving_ct / 100, 2),
    }


def apply(result: Any, report: Mapping[str, Any], *, eligible: bool, timezone: tzinfo) -> Any:
    """Replace a low-priority decision by grid charging while the plan is active."""
    if not eligible or report.get("active") is not True:
        return result
    if getattr(result, "decision_id", None) not in OVERRIDABLE_DECISIONS:
        return result
    charge = report.get("action") == "charge"
    mode = GRID_CHARGE_MODE if charge else HOLD_MODE
    if getattr(result, "mode", None) == mode:
        return result
    if charge:
        reason = (f"Ehrgeiziges Vorladen (bis {report.get('target_soc'):g} %, "
                  f"Ø {report.get('charge_price_ct'):g} ct)")
    else:
        reason = f"Ehrgeiziges Vorladen: Akku hält {report.get('target_soc'):g} % für den Tag"
    states = dict(result.states)
    states["sensor.opti_strategie_vorschau"] = mode
    attributes = {
        **result.attributes,
        "sensor.opti_strategie_vorschau": {
            **result.attributes.get("sensor.opti_strategie_vorschau", {}),
            "grund": reason,
        },
    }
    return replace(result, mode=mode, reason=reason, states=states,
                   attributes=attributes, decision_id=DECISION)


def standby_window(report: Mapping[str, Any], window: Mapping[str, Any]) -> Mapping[str, Any]:
    """Let the standby reserve keep an SMA inverter awake for this window too."""
    if window.get("status") == "planned" or report.get("status") not in ("planned", "active"):
        return window
    first, rising = _instant(report.get("first_at")), _instant(report.get("sunrise"))
    if first is None or rising is None or first >= rising:
        return window
    return {"status": "planned", "at": first.isoformat(), "kind": DECISION,
            "sunrise": rising.isoformat()}
