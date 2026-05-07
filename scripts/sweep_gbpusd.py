# scripts/sweep_gbpusd.py
# ─────────────────────────────────────────────────────────────────────────────
# Fast parameter sweep for trend_pullback on GBPUSD.
# Loads + enriches frames ONCE, then reruns engine for each param combo.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
import sys
import time
from itertools import product

from config.settings import PRIMARY_TF, TIMEFRAMES
from python.backtest.data_loader import load_all
from python.backtest.engine import BacktestEngine
from python.core.indicators import add_indicators
from python.core.mt5_connector import MT5Connector
from python.strategies import trend_pullback as tp_module
from python.strategies.trend_pullback import TrendPullback

logging.basicConfig(level=logging.WARNING, format="%(message)s")

SYMBOL = "GBPUSD"


def _specs() -> dict:
    conn = MT5Connector()
    if not conn.connect():
        return {"contract_size": 100_000, "pip_size": 0.0001,
                "min_lot": 0.01, "max_lot": 50.0, "lot_step": 0.01,
                "tick_size": 0.00001, "tick_value": 1.0}
    info = conn.get_symbol_info(SYMBOL)
    conn.disconnect()
    return info


def main() -> None:
    print(f"Loading {SYMBOL}...", flush=True)
    specs    = _specs()
    frames   = load_all(SYMBOL, TIMEFRAMES)

    print("Enriching with indicators (one-time)...", flush=True)
    t0 = time.time()
    enriched = {tf: add_indicators(df) for tf, df in frames.items()}
    print(f"  done in {time.time()-t0:.1f}s", flush=True)

    grid = list(product(
        [1.5, 2.0, 2.5],          # target R
        [0.0, 4.5],                # quality gate
        [0.3, 0.5, 0.8],           # SL ATR buffer
    ))
    print(f"Running {len(grid)} combos...", flush=True)

    results = []
    for idx, (tr, q, slb) in enumerate(grid, 1):
        tp_module.TARGET_R          = tr
        tp_module.MIN_QUALITY_SCORE = q
        tp_module.SL_ATR_BUFFER     = slb
        strat = TrendPullback()
        eng = BacktestEngine(
            symbol=SYMBOL, primary_tf=PRIMARY_TF, starting_balance=10_000.0,
            contract_size=specs["contract_size"], pip_size=specs["pip_size"],
            tick_size=specs.get("tick_size"), tick_value=specs.get("tick_value"),
            min_lot=specs["min_lot"], max_lot=specs["max_lot"],
            lot_step=specs["lot_step"],
        )
        t1 = time.time()
        res = eng.run(strat, enriched, pre_enriched=True)
        m = res.metrics
        dt = time.time() - t1
        print(f"  [{idx:>2}/{len(grid)}] tR={tr} q={q} slB={slb} | "
              f"n={m.n_trades:>3} wr={m.win_rate:.1%} pf={m.profit_factor:.2f} "
              f"exp={m.expectancy_r:+.2f} dd={m.max_dd_pct:.2f}% "
              f"ret={m.total_return_pct:+.2f}% [{dt:.0f}s]", flush=True)
        results.append({"tR": tr, "q": q, "slB": slb,
                        "n": m.n_trades, "wr": m.win_rate, "pf": m.profit_factor,
                        "exp": m.expectancy_r, "dd": m.max_dd_pct,
                        "ret": m.total_return_pct})

    valid = [r for r in results if r["n"] >= 30]
    valid.sort(key=lambda r: r["ret"], reverse=True)

    print(f"\n=== TOP combos by Total Return (n>=30) ===", flush=True)
    print(f"{'tR':>4} {'q':>4} {'slB':>4} | {'n':>3} {'wr':>6} {'pf':>5} {'exp':>6} {'dd':>6} {'ret':>7}")
    for r in valid:
        print(f"{r['tR']:>4.1f} {r['q']:>4.1f} {r['slB']:>4.1f} | "
              f"{r['n']:>3} {r['wr']:>6.1%} {r['pf']:>5.2f} {r['exp']:>+6.2f} "
              f"{r['dd']:>5.2f}% {r['ret']:>+6.2f}%")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
