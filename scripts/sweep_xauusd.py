# scripts/sweep_xauusd.py
# ─────────────────────────────────────────────────────────────────────────────
# Two-stage parameter sweep for trend_pullback on XAUUSD.
# Stage 1: pick best mode (quality_gate × structural_filters) at default params.
# Stage 2: sweep target_R x SL_buffer on the winning mode.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
import sys
import time
from itertools import product

from config.settings import PRIMARY_TF, TIMEFRAMES
from config import settings as _settings
from python.backtest.data_loader import load_all
from python.backtest.engine import BacktestEngine
from python.core.indicators import add_indicators
from python.core.mt5_connector import MT5Connector
from python.strategies import trend_pullback as tp_module
from python.strategies.trend_pullback import TrendPullback

logging.basicConfig(level=logging.WARNING, format="%(message)s")

SYMBOL = "XAUUSD"

# Sweeps must use module-level overrides directly; clear any preset for this
# symbol so SYMBOL_PARAMS doesn't shadow the values we set in run_one().
_settings.SYMBOL_PARAMS = {k: v for k, v in _settings.SYMBOL_PARAMS.items()
                           if k != SYMBOL}


def _specs() -> dict:
    conn = MT5Connector()
    if not conn.connect():
        return {"contract_size": 100, "pip_size": 0.01,
                "min_lot": 0.01, "max_lot": 50.0, "lot_step": 0.01,
                "tick_size": 0.01, "tick_value": 1.0}
    info = conn.get_symbol_info(SYMBOL)
    conn.disconnect()
    return info


def run_one(specs, enriched, *, target_r, quality, sl_buf, strict, filters):
    tp_module.TARGET_R          = target_r
    tp_module.MIN_QUALITY_SCORE = quality
    tp_module.SL_ATR_BUFFER     = sl_buf
    tp_module.STRICT_TIER_A     = strict
    tp_module.ENABLE_FILTERS    = filters
    eng = BacktestEngine(
        symbol=SYMBOL, primary_tf=PRIMARY_TF, starting_balance=10_000.0,
        contract_size=specs["contract_size"], pip_size=specs["pip_size"],
        tick_size=specs.get("tick_size"), tick_value=specs.get("tick_value"),
        min_lot=specs["min_lot"], max_lot=specs["max_lot"],
        lot_step=specs["lot_step"],
    )
    res = eng.run(TrendPullback(), enriched, pre_enriched=True)
    m = res.metrics
    return {"tR": target_r, "slB": sl_buf, "strict": strict, "filt": filters,
            "n": m.n_trades, "wr": m.win_rate, "pf": m.profit_factor,
            "exp": m.expectancy_r, "dd": m.max_dd_pct, "ret": m.total_return_pct}


def fmt(r):
    return (f"n={r['n']:>3} wr={r['wr']:.1%} pf={r['pf']:.2f} "
            f"exp={r['exp']:+.2f} dd={r['dd']:.2f}% ret={r['ret']:+.2f}%")


def main() -> None:
    print(f"Loading {SYMBOL}...", flush=True)
    specs    = _specs()
    frames   = load_all(SYMBOL, TIMEFRAMES)
    t0 = time.time()
    enriched = {tf: add_indicators(df) for tf, df in frames.items()}
    print(f"Enriched in {time.time()-t0:.1f}s", flush=True)

    # ── STAGE 1 — mode selection at tR=2.0, slB=0.5 ─────────────────────
    print("\n=== STAGE 1: mode selection (tR=2.0, slB=0.5) ===", flush=True)
    stage1 = []
    for strict, filt in [(False, False), (True, False), (False, True), (True, True)]:
        q = 4.5 if strict else 0.0
        t1 = time.time()
        r = run_one(specs, enriched, target_r=2.0, quality=q,
                    sl_buf=0.5, strict=strict, filters=filt)
        tag = f"q={'on' if strict else 'off'} flt={'on' if filt else 'off'}"
        print(f"  {tag} | {fmt(r)} [{time.time()-t1:.0f}s]", flush=True)
        stage1.append(r)

    # Pick best mode by return
    best_mode = max(stage1, key=lambda r: r["ret"])
    print(f"\nWinning mode: strict={best_mode['strict']} filt={best_mode['filt']} "
          f"-> ret={best_mode['ret']:+.2f}%", flush=True)

    # ── STAGE 2 — sweep tR x slB on winning mode ────────────────────────
    print("\n=== STAGE 2: tR x slB sweep on winning mode ===", flush=True)
    grid = list(product([1.5, 2.0, 2.5, 3.0], [0.3, 0.5, 0.8, 1.2]))
    results = []
    q = 4.5 if best_mode["strict"] else 0.0
    for idx, (tr, slb) in enumerate(grid, 1):
        t1 = time.time()
        r = run_one(specs, enriched, target_r=tr, quality=q,
                    sl_buf=slb, strict=best_mode["strict"],
                    filters=best_mode["filt"])
        print(f"  [{idx:>2}/{len(grid)}] tR={tr} slB={slb} | {fmt(r)} "
              f"[{time.time()-t1:.0f}s]", flush=True)
        results.append(r)

    valid = [r for r in results if r["n"] >= 50]
    valid.sort(key=lambda r: r["ret"], reverse=True)

    print("\n=== TOP combos by Total Return (n>=50) ===", flush=True)
    print(f"{'tR':>4} {'slB':>4} | {'n':>3} {'wr':>6} {'pf':>5} {'exp':>6} {'dd':>6} {'ret':>7}")
    for r in valid:
        print(f"{r['tR']:>4.1f} {r['slB']:>4.1f} | "
              f"{r['n']:>3} {r['wr']:>6.1%} {r['pf']:>5.2f} {r['exp']:>+6.2f} "
              f"{r['dd']:>5.2f}% {r['ret']:>+6.2f}%")

    if valid:
        winner = valid[0]
        print(f"\nBEST: tR={winner['tR']} slB={winner['slB']} "
              f"strict={best_mode['strict']} filt={best_mode['filt']}")
        print(f"      ret={winner['ret']:+.2f}% pf={winner['pf']:.2f} "
              f"dd={winner['dd']:.2f}% n={winner['n']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
