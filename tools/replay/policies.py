"""Mode policies: the real StrategyEngine, and a self-consumption baseline."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .battery import DYNAMIC, MODES
from .resources import describe_engine, describe_strategy, load_engine_module, load_strategy
from .sun import SunModel

CAPACITY = "sensor.opti_battery_capacity_kwh"
# Plant parameters mapped onto the engine's helper entities.
PLANT_SETTINGS = {
    "minsoc": "input_number.minsoc",
    "maxsoc": "input_number.maxsoc",
    "feed_in_ct": "input_number.opti_einspeiseverguetung_ct",
    "netzlade_spread_ct": "input_number.opti_netzlade_spread_ct",
    "halte_spread_ct": "input_number.opti_halte_spread_ct",
    "max_charge_w": "input_number.akkusteuerung_max_ladestaerke",
    "max_discharge_w": "input_number.akkusteuerung_max_entladestaerke",
    "pv_surplus_charging": "input_boolean.opti_pv_ueberschuss_ladung",
    "grid_precharge": "input_boolean.opti_prognose_netzladen",
}


@dataclass(frozen=True)
class PowerSnapshot:
    """Powers of the previous step as the engine's live measurements (W)."""

    pv_w: float = 0.0
    load_w: float = 0.0
    export_w: float = 0.0
    import_w: float = 0.0
    battery_w: float = 0.0  # positive = charging, as sensor.opti_battery_power_w


@dataclass(frozen=True)
class StepContext:
    now: dt.datetime
    soc: float
    previous: PowerSnapshot
    price_ct: float | None
    price_today: list[float] | None
    price_tomorrow: list[float]
    forecast_today_kwh: float | None
    forecast_tomorrow_kwh: float | None
    forecast_remaining_kwh: float | None
    load_day_kwh: float | None = None  # climatological daily load (previous month), for the profile denominator


@dataclass(frozen=True)
class Decision:
    mode: str
    decision_id: str = "unknown"
    reason: str = ""
    charge_limit_w: float | None = None
    ok: bool = True
    target_soc: float | None = None
    discharge_limit_w: float | None = None  # only planner policies set it
    extra: dict[str, Any] = field(default_factory=dict)  # observed internal states (scores, horizon)


# Internal engine states copied into the step records for stability metrics.
OBSERVED_STATES = {
    "score": "sensor.opti_forecast_score",
    "score_sonnentag": "sensor.opti_forecast_score_sonnentag",
    "horizon_long": "binary_sensor.opti_peak_horizont_lang",
}
DAILY_LOAD_STATISTIC = "sensor.opti_house_consumption_24h_w"
HOUSE_DAILY_MODES = ("engine", "lastprofil")


class SelfConsumptionPolicy:
    """Always Dynamisch: charge surplus, discharge deficit (the inverter default)."""

    name = "self_consumption"

    def decide(self, context: StepContext) -> Decision:
        return Decision(DYNAMIC, "self_consumption", "Eigenverbrauch")

    def reset(self) -> None:
        """No state to discard at a measurement block boundary."""

    def summary(self) -> dict[str, Any]:
        return {}


@dataclass
class StrategyPolicy:
    """Feed the real engine with synthetic live inputs derived from the replay."""

    plant: dict
    strategy_ref: str | None = None
    sun: SunModel = field(default_factory=SunModel)
    battery_temp_c: float = 25.0
    name: str = "strategy"
    resources: dict | None = None  # overrides strategy_ref (tests, patched variants)
    house_daily: str = "engine"  # "lastprofil": 24-h load statistic replaced by the previous month's mean daily load

    def __post_init__(self) -> None:
        if self.house_daily not in HOUSE_DAILY_MODES:
            raise ValueError(f"Unknown house_daily mode: {self.house_daily}")
        self._engine_module = load_engine_module()
        self._injected = self.resources is not None
        if self.resources is None:
            self.resources = load_strategy(self.strategy_ref)
        self.engine = self._engine_module.StrategyEngine(self.resources)
        self.steps = 0
        self.error_steps = 0
        self.charge_limit_unknown_steps = 0
        self.resets = 0
        self.error_types: dict[str, int] = {}
        self.settings: dict[str, Any] = {"input_boolean.akku_opti_automatik": "on"}
        for key, entity in PLANT_SETTINGS.items():
            if key in self.plant:
                value = self.plant[key]
                self.settings[entity] = ("on" if value else "off") if isinstance(value, bool) else value
        self.settings.update(self.plant.get("settings", {}))

    def decide(self, context: StepContext) -> Decision:
        sun_state, sun_attrs = self.sun.state(context.now)
        previous = context.previous
        states: dict[str, Any] = {
            **self.settings,
            "sensor.opti_soc": round(context.soc, 2),
            CAPACITY: self.plant["capacity_kwh"],
            "sensor.opti_battery_temp": self.battery_temp_c,
            "sensor.opti_house_consumption_w": round(previous.load_w),
            "sensor.opti_pv_power_w": round(previous.pv_w),
            "sensor.opti_grid_export_w": round(previous.export_w),
            "sensor.opti_grid_import_w": round(previous.import_w),
            "sensor.opti_battery_power_w": round(previous.battery_w),
            "input_boolean.opti_score_house_includes_ev": "off",
            "sun.sun": sun_state,
        }
        attributes: dict[str, dict] = {"sun.sun": sun_attrs}
        if self.house_daily == "lastprofil" and context.load_day_kwh is not None:
            # Variant C: the learned profile's daily demand stands in for the 24-h statistic.
            states[DAILY_LOAD_STATISTIC] = round(context.load_day_kwh / 24 * 1000)
            attributes[DAILY_LOAD_STATISTIC] = {"age_coverage_ratio": 1.0, "sample_count": 24}
        if context.forecast_today_kwh is not None:
            states["sensor.opti_forecast_today_kwh"] = round(context.forecast_today_kwh, 3)
            states["sensor.opti_forecast_tomorrow_kwh"] = round(context.forecast_tomorrow_kwh or 0.0, 3)
            states["sensor.opti_forecast_remaining_today_kwh"] = round(context.forecast_remaining_kwh or 0.0, 3)
            for key in ("today", "tomorrow", "remaining_today"):
                attributes[f"sensor.opti_forecast_{key}_kwh"] = {"estimate10": None}
        if context.price_ct is not None:
            # The current price is known even when the day's series has gaps;
            # only the series itself (and with it the price level) is withheld.
            states["sensor.opti_price_current_ct_kwh"] = round(context.price_ct, 3)
            if context.price_today:
                states["sensor.opti_price_series"] = round(context.price_ct, 3)
                attributes["sensor.opti_price_series"] = {
                    "today": list(context.price_today), "tomorrow": list(context.price_tomorrow),
                }
        result = self.engine.evaluate(states, attributes, context.now)
        self.steps += 1
        ok = result.states.get("sensor.opti_engine_diagnostics") == "ok"
        if not ok:
            self.error_steps += 1
            for name in result.attributes.get("sensor.opti_engine_diagnostics", {}).get("template_errors", {}):
                self.error_types[name] = self.error_types.get(name, 0) + 1
        mode = result.mode if result.mode in MODES else "Akku Pause"
        limit = _number(result.states.get("sensor.opti_charge_power_w"))
        if limit is None:
            # The coordinator enforces 0 W when the derived limit is unavailable.
            limit = 0.0
            self.charge_limit_unknown_steps += 1
        return Decision(
            mode, result.decision_id, result.reason, limit, ok,
            _number(result.states.get("sensor.opti_target_soc")),
            extra={key: result.states.get(entity) for key, entity in OBSERVED_STATES.items()},
        )

    def reset(self) -> None:
        """Fresh engine state at a measurement block boundary (like a cold start)."""
        self.engine = self._engine_module.StrategyEngine(self.resources)
        self.resets += 1

    def summary(self) -> dict[str, Any]:
        return {
            "strategy_ref": self.strategy_ref or "worktree",
            **({"strategy_origin": "injected resources", "strategy_sha256": describe_strategy(None, self.resources)["strategy_sha256"]}
               if self._injected else describe_strategy(self.strategy_ref, self.resources)),
            **describe_engine(),
            "source_revision": self.resources.get("source_revision"),
            "engine_steps": self.steps,
            "engine_error_steps": self.error_steps,
            "engine_error_types": dict(self.error_types),
            "charge_limit_unknown_steps": self.charge_limit_unknown_steps,
            "block_restarts": self.resets,
            "sun_source": self.sun.source,
            "battery_temp_c": self.battery_temp_c,
            "house_daily": self.house_daily,
        }


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
