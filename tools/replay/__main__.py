"""CLI: ``python -m tools.replay {import,run,compare,report} ...``."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import metrics as metrics_module
from . import report as report_module
from . import simulate
from .battery import BatteryParams
from .importer import import_series
from .planner_policies import OraclePolicy, RecedingHorizonPolicy
from .policies import SelfConsumptionPolicy, StrategyPolicy
from .resources import describe_engine
from .sun import SunModel
from .timeseries import read_csv, write_csv


def _json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def cmd_import(args: argparse.Namespace) -> int:
    paths = []
    for item in args.raw:
        path = Path(item)
        paths.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    hours, summary = import_series(paths, _json(args.mapping), tz=args.tz, price_gap_max_hours=args.price_gap,
                                   sun=SunModel(args.latitude, args.longitude))
    summary["sun_source"] = SunModel(args.latitude, args.longitude).source
    write_csv(hours, Path(args.out))
    Path(args.out).with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def build_policy(spec: str, plant: dict, sun: SunModel, hours=None, params: BatteryParams | None = None,
                 forecast_mode: str = "calibrated", planner: dict | None = None, steps_per_hour: int = 4):
    """``oracle[@hold|@value]`` and ``rh[@naive|@calibrated]`` need the series and the battery."""
    planner = planner or {}
    if spec == "actual":
        return None
    if spec == "self_consumption":
        return SelfConsumptionPolicy()
    if spec in ("strategy", "strategy+lastprofil") or spec.startswith(("strategy@", "strategy+lastprofil@")):
        # ``strategy+lastprofil@ref``: the 24-h load statistic is replaced by the
        # previous month's mean daily load (stand-in for the learned demand profile).
        head, _, ref = spec.partition("@")
        house_daily = "lastprofil" if head.endswith("+lastprofil") else "engine"
        # The long-window statistics sample once per simulation step.
        return StrategyPolicy(plant, ref or None, sun=sun, name=spec, house_daily=house_daily,
                              sample_interval_s=3600.0 / steps_per_hour)
    if spec == "oracle" or spec.startswith("oracle@"):
        terminal = spec.partition("@")[2] or "hold"
        return OraclePolicy(hours, plant, params, wear_ct=planner.get("wear_ct", 3.0), terminal=terminal,
                            soc_step=planner.get("soc_step", 0.5), name=spec)
    if spec == "rh" or spec.startswith("rh@"):
        mode = spec.partition("@")[2] or forecast_mode
        return RecedingHorizonPolicy(hours, plant, params, forecast_mode=mode,
                                     load_mode=planner.get("load_mode", "climatology"),
                                     pv_mode=planner.get("pv_mode", "forecast"),
                                     min_benefit_ct=planner.get("min_benefit_ct", 0.0),
                                     pv_trust=planner.get("pv_trust", 1.0),
                                     grid_check=planner.get("grid_check", False),
                                     wear_ct=planner.get("wear_ct", 3.0),
                                     terminal_factor=planner.get("terminal_factor", 0.8),
                                     soc_step=planner.get("soc_step", 0.5), name=spec)
    raise SystemExit(f"Unknown policy: {spec}")


def run_policy(spec: str, hours, plant: dict, params: BatteryParams, sun: SunModel, steps_per_hour: int,
               forecast_mode: str = "calibrated", planner: dict | None = None):
    started = time.perf_counter()
    policy = build_policy(spec, plant, sun, hours, params, forecast_mode, planner, steps_per_hour)
    if policy is None:
        records = simulate.run_actual(hours, steps_per_hour)
        info: dict[str, Any] = {}
    else:
        records = simulate.run(hours, policy, params, steps_per_hour=steps_per_hour, forecast_mode=forecast_mode)
        info = policy.summary()
    info["runtime_s"] = round(time.perf_counter() - started, 1)
    info["records"] = len(records)
    return records, info


def _compare(args: argparse.Namespace) -> int:
    plant = _json(args.plant)
    params = BatteryParams.from_plant(plant, eta_charge=args.eta, eta_discharge=args.eta)
    hours = read_csv(Path(args.series), tz=args.tz)
    sun = SunModel(args.latitude, args.longitude)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    planner = {"wear_ct": args.wear_ct, "terminal_factor": args.terminal_factor, "soc_step": args.planner_step,
               "load_mode": args.rh_load, "pv_mode": args.rh_pv, "min_benefit_ct": args.min_benefit_ct,
               "pv_trust": args.pv_trust, "grid_check": args.grid_check}
    all_metrics: dict[str, dict] = {}
    meta: dict[str, Any] = {
        "series": str(args.series), "hours": len(hours),
        "from": hours[0].ts_local.isoformat() if hours else None,
        "to": hours[-1].ts_local.isoformat() if hours else None,
        "engine": describe_engine(),
        "assumptions": {
            "steps_per_hour": args.steps,
            "hourly_values_spread_evenly": True,
            "battery": params.__dict__,
            "sun_source": sun.source, "latitude": args.latitude, "longitude": args.longitude,
            "forecast_mode": args.forecast_mode,
            "forecast_levels": "reconstructed from change rows, anchored on later actual yields (future knowledge)",
            "forecast_remaining": ("today forecast x share of the day's ACTUAL PV still ahead (future knowledge)"
                                   if args.forecast_mode == "calibrated"
                                   else "today forecast x climatological hourly share of the previous month"),
            "tomorrow_prices_known_from_hour": simulate.TOMORROW_KNOWN_FROM_HOUR,
            "engine_live_inputs": "powers of the previous step, simulated SoC, battery temp 25 C",
            "block_restart_gap_hours": simulate.BLOCK_GAP.total_seconds() / 3600,
            "warmup_hours_after_restart": simulate.WARMUP_HOURS,
            "price_gaps": "days with missing hours get no price series (no price level); current price always given",
            "unknown_charge_limit_w": 0,
            "planner": planner,
        },
    }
    for spec in args.policies:
        records, info = run_policy(spec, hours, plant, params, sun, args.steps, args.forecast_mode, planner)
        name = _safe(spec)
        simulate.write_records(records, out_dir / f"records_{name}.csv")
        result = metrics_module.summarize(
            records, params=params, feed_in_ct=float(plant.get("feed_in_ct", 0)),
            step_hours=1 / args.steps, top_n=args.top, wear_ct_kwh=args.wear_ct,
        )
        (out_dir / f"metrics_{name}.json").write_text(
            json.dumps({"policy": spec, "info": info, "periods": result, "meta": meta}, indent=2, default=str),
            encoding="utf-8")
        all_metrics[spec] = result
        meta[f"policy {spec}"] = info
        print(f"{spec}: {info}", file=sys.stderr)
    (out_dir / "report.md").write_text(report_module.render(all_metrics, meta), encoding="utf-8")
    print(f"Bericht: {out_dir / 'report.md'}", file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    all_metrics, meta = {}, {}
    for path in sorted(out_dir.glob("metrics_*.json")):
        data = _json(path)
        all_metrics[data["policy"]] = data["periods"]
        meta.update({key: value for key, value in data.get("meta", {}).items() if not key.startswith("policy ")})
        meta[f"policy {data['policy']}"] = data["info"]
    (out_dir / "report.md").write_text(report_module.render(all_metrics, meta), encoding="utf-8")
    print(f"Bericht: {out_dir / 'report.md'}", file=sys.stderr)
    return 0


def _add_sim_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--series", required=True, help="CSV aus `import`")
    parser.add_argument("--plant", required=True, help="plant.json")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--tz", default="Europe/Berlin")
    parser.add_argument("--steps", type=int, default=4, help="Schritte je Stunde")
    parser.add_argument("--eta", type=float, default=0.95, help="Wirkungsgrad je Richtung")
    parser.add_argument("--latitude", type=float, default=51.0)
    parser.add_argument("--longitude", type=float, default=10.0)
    parser.add_argument("--top", type=int, default=10, help="Top-N Tage im Bericht")
    parser.add_argument("--forecast-mode", choices=simulate.FORECAST_MODES, default="calibrated",
                        help="Restprognose: calibrated (Ist-Tagesverlauf, Zukunftswissen) oder naive (Klimatologie Vormonat)")
    parser.add_argument("--wear-ct", type=float, default=3.0, help="Planer: Zyklenkosten ct je kWh Entladung")
    parser.add_argument("--terminal-factor", type=float, default=0.8,
                        help="Planer (rh): Endwert-Anteil am mittleren Horizontpreis fuer Restenergie")
    parser.add_argument("--planner-step", type=float, default=0.5, help="Planer: SoC-Raster in Prozent")
    parser.add_argument("--rh-load", choices=("climatology", "perfect"), default="climatology",
                        help="rh: Lastprognose aus Vormonats-Klimatologie oder perfekt")
    parser.add_argument("--rh-pv", choices=("forecast", "perfect"), default="forecast",
                        help="rh: PV aus Harness-Prognose oder perfekt (Diagnose der Prognosefehler)")
    parser.add_argument("--min-benefit-ct", type=float, default=0.0,
                        help="rh Variante A: Plan nur, wenn erwarteter Vorteil je Plan >= ct, sonst Eigenverbrauch")
    parser.add_argument("--pv-trust", type=float, default=1.0,
                        help="rh Variante B: Planung mit PV x Faktor (Ersatz fuer P10, der Harness hat keine Quantile)")
    parser.add_argument("--grid-check", action="store_true",
                        help="rh Variante B: Netzladen nur, wenn auch der Plan mit voller PV-Prognose netzlaedt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.replay", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="HA-Statistik-JSON -> Stundenreihe CSV")
    p_import.add_argument("--raw", nargs="+", required=True, help="JSON-Dateien oder Verzeichnis")
    p_import.add_argument("--mapping", required=True)
    p_import.add_argument("--out", required=True)
    p_import.add_argument("--tz", default="Europe/Berlin")
    p_import.add_argument("--price-gap", type=int, default=3, help="max. interpolierte Preislücke in h")
    p_import.add_argument("--latitude", type=float, default=51.0, help="für die Nacht-Null fehlender PV-Zähler")
    p_import.add_argument("--longitude", type=float, default=10.0)
    p_import.set_defaults(func=cmd_import)

    p_run = sub.add_parser("run", help="eine Policy simulieren")
    _add_sim_arguments(p_run)
    p_run.add_argument("--policy", required=True,
                       help="actual | self_consumption | strategy[+lastprofil][@ref|@pfad] | oracle[@hold|@value] | rh[@naive|@calibrated]")
    p_run.set_defaults(func=lambda args: _compare(argparse.Namespace(**vars(args), policies=[args.policy])))

    p_compare = sub.add_parser("compare", help="mehrere Policies nebeneinander")
    _add_sim_arguments(p_compare)
    p_compare.add_argument("--policies", nargs="+", required=True)
    p_compare.set_defaults(func=_compare)

    p_report = sub.add_parser("report", help="Bericht aus vorhandenen metrics_*.json neu erzeugen")
    p_report.add_argument("--out-dir", required=True)
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
