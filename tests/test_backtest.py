import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

from backtest import simulate_day  # noqa: E402

FLACH_50 = [50.0] * 24
SPITZE_ABEND = [50.0] * 19 + [200.0, 200.0, 200.0, 50.0, 50.0]


def test_neu_billiger_bei_abendspitze():
    kwargs = dict(load_kw=0.9, pv_kwh_per_hour=[0.0] * 24,
                  start_soc=15.0, cap_kwh=12.8)
    alt = simulate_day(SPITZE_ABEND, FLACH_50, neu=False, **kwargs)
    neu = simulate_day(SPITZE_ABEND, FLACH_50, neu=True, **kwargs)
    # Neu laedt tagsueber bei 50 ct vor und entlaedt in der 200-ct-Spitze.
    assert neu["cost_eur"] < alt["cost_eur"]
    assert any(m == "Akku nur Entladen" for m in neu["modus_verlauf"][19:22])


def test_flacher_tag_kein_unterschied():
    kwargs = dict(load_kw=0.9, pv_kwh_per_hour=[0.0] * 24,
                  start_soc=50.0, cap_kwh=12.8)
    alt = simulate_day(FLACH_50, FLACH_50, neu=False, **kwargs)
    neu = simulate_day(FLACH_50, FLACH_50, neu=True, **kwargs)
    assert abs(neu["cost_eur"] - alt["cost_eur"]) < 0.01
