# scripts/backtest.py
# ─────────────────────────────────────────────────────────────────────────────
# Run a single-strategy backtest from CSVs in data/historical/.
#
# Usage:
#   python -m scripts.backtest --strategy trend_pullback --symbol EURUSD
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import logging

from config.settings import PRIMARY_TF, SYMBOL, TIMEFRAMES
from python.backtest.data_loader import load_all
from python.backtest.engine import BacktestEngine
from python.core.mt5_connector import MT5Connector
from python.strategies.trend_pullback import TrendPullback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

STRATEGIES = {
    "trend_pullback": TrendPullback,
}


def _symbol_specs(symbol: str) -> dict:
    """Pull contract_size, pip_size, lot_step etc. from the live MT5 terminal.
    Falls back to FX defaults if the terminal is unreachable."""
    conn = MT5Connector()
    if not conn.connect():
        log.warning("MT5 not reachable — using FX defaults for %s", symbol)
        return {"contract_size": 100_000, "pip_size": 0.0001,
                "min_lot": 0.01, "max_lot": 50.0, "lot_step": 0.01}
    info = conn.get_symbol_info(symbol)
    conn.disconnect()
    if not info:
        raise SystemExit(f"Could not fetch symbol info for {symbol}")
    log.info("%s | contract=%s pip=%s lot_step=%s",
             symbol, info["contract_size"], info["pip_size"], info["lot_step"])
    return info


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="trend_pullback", choices=list(STRATEGIES))
    ap.add_argument("--symbol",   default=SYMBOL)
    ap.add_argument("--balance",  type=float, default=10_000.0)
    ap.add_argument("--tfs",      nargs="+", default=TIMEFRAMES)
    ap.add_argument("--target-r", type=float, default=None,
                    help="Override TARGET_R in trend_pullback")
    ap.add_argument("--quality",  type=float, default=None,
                    help="Override MIN_QUALITY_SCORE")
    ap.add_argument("--sl-buf",   type=float, default=None,
                    help="Override SL_ATR_BUFFER")
    ap.add_argument("--rsi-ob",   type=float, default=None,
                    help="Override RSI_OVERBOUGHT")
    ap.add_argument("--rsi-os",   type=float, default=None,
                    help="Override RSI_OVERSOLD")
    ap.add_argument("--no-quality", action="store_true",
                    help="Disable STRICT_TIER_A quality gate")
    ap.add_argument("--no-filters", action="store_true",
                    help="Disable structural filters (H1 ADX, D1 slope, ATR floor)")
    args = ap.parse_args()

    # Apply strategy overrides BEFORE constructing the strategy
    from python.strategies import trend_pullback as tp_mod
    if args.target_r is not None: tp_mod.TARGET_R          = args.target_r
    if args.quality  is not None: tp_mod.MIN_QUALITY_SCORE = args.quality
    if args.sl_buf   is not None: tp_mod.SL_ATR_BUFFER     = args.sl_buf
    if args.rsi_ob   is not None: tp_mod.RSI_OVERBOUGHT    = args.rsi_ob
    if args.rsi_os   is not None: tp_mod.RSI_OVERSOLD      = args.rsi_os
    if args.no_quality:           tp_mod.STRICT_TIER_A     = False
    if args.no_filters:           tp_mod.ENABLE_FILTERS    = False

    specs    = _symbol_specs(args.symbol)
    frames   = load_all(args.symbol, args.tfs)
    strategy = STRATEGIES[args.strategy]()
    engine   = BacktestEngine(
        symbol=args.symbol, primary_tf=PRIMARY_TF, starting_balance=args.balance,
        contract_size=specs["contract_size"], pip_size=specs["pip_size"],
        tick_size=specs.get("tick_size"), tick_value=specs.get("tick_value"),
        min_lot=specs["min_lot"], max_lot=specs["max_lot"], lot_step=specs["lot_step"],
    )
    result   = engine.run(strategy, frames)
    m        = result.metrics

    print("\n" + "=" * 60)
    print(f"Backtest: {args.strategy} on {args.symbol}")
    print("=" * 60)
    print(f"  Trades:           {m.n_trades}")
    print(f"  Win rate:         {m.win_rate:.1%}")
    print(f"  Profit factor:    {m.profit_factor:.2f}")
    print(f"  Expectancy (R):   {m.expectancy_r:+.2f}")
    print(f"  Avg win / loss R: {m.avg_win_r:+.2f} / {m.avg_loss_r:+.2f}")
    print(f"  Max drawdown:     {m.max_dd_pct:.2f}%")
    print(f"  Sharpe (trade):   {m.sharpe:.2f}")
    print(f"  Total return:     {m.total_return_pct:+.2f}%")
    print(f"  Final balance:    ${m.final_balance:,.2f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
