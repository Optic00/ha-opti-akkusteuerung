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
from .policies import SelfConsumptionPolicy, StrategyPolicy
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
    hours, summary = import_series(paths, _json(args.mapping), tz=args.tz, price_gap_max_hours=args.price_gap)
    write_csv(hours, Path(args.out))
    Path(args.out).with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def build_policy(spec: str, plant: dict, sun: SunModel):
    if spec == "actual":
        return None
    if spec == "self_consumption":
        return SelfConsumptionPolicy()
    if spec == "strategy" or spec.startswith("strategy@"):
        ref = spec.partition("@")[2] or None
        return StrategyPolicy(plant, ref, sun=sun, name=spec)
    raise SystemExit(f"Unknown policy: {spec}")


def run_policy(spec: str, hours, plant: dict, params: BatteryParams, sun: SunModel, steps_per_hour: int):
    started = time.perf_counter()
    policy = build_policy(spec, plant, sun)
    if policy is None:
        records = simulate.run_actual(hours, steps_per_hour)
        info: dict[str, Any] = {}
    else:
        records = simulate.run(hours, policy, params, steps_per_hour=steps_per_hour)
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
    all_metrics: dict[str, dict] = {}
    meta: dict[str, Any] = {
        "series": str(args.series), "hours": len(hours),
        "from": hours[0].ts_local.isoformat() if hours else None,
        "to": hours[-1].ts_local.isoformat() if hours else None,
        "steps_per_hour": args.steps, "battery": params.__dict__, "sun": sun.source,
    }
    for spec in args.policies:
        records, info = run_policy(spec, hours, plant, params, sun, args.steps)
        name = _safe(spec)
        simulate.write_records(records, out_dir / f"records_{name}.csv")
        result = metrics_module.summarize(
            records, capacity_kwh=params.capacity_kwh, maxsoc=params.maxsoc,
            feed_in_ct=float(plant.get("feed_in_ct", 0)), max_charge_w=params.max_charge_w,
            step_hours=1 / args.steps, top_n=args.top,
        )
        (out_dir / f"metrics_{name}.json").write_text(
            json.dumps({"policy": spec, "info": info, "periods": result}, indent=2), encoding="utf-8")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.replay", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="HA-Statistik-JSON -> Stundenreihe CSV")
    p_import.add_argument("--raw", nargs="+", required=True, help="JSON-Dateien oder Verzeichnis")
    p_import.add_argument("--mapping", required=True)
    p_import.add_argument("--out", required=True)
    p_import.add_argument("--tz", default="Europe/Berlin")
    p_import.add_argument("--price-gap", type=int, default=3, help="max. interpolierte Preislücke in h")
    p_import.set_defaults(func=cmd_import)

    p_run = sub.add_parser("run", help="eine Policy simulieren")
    _add_sim_arguments(p_run)
    p_run.add_argument("--policy", required=True, help="actual | self_consumption | strategy[@ref|@pfad]")
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
