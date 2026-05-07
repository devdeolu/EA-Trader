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
from python.strategies.trend_pullback import TrendPullback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

STRATEGIES = {
    "trend_pullback": TrendPullback,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="trend_pullback", choices=list(STRATEGIES))
    ap.add_argument("--symbol",   default=SYMBOL)
    ap.add_argument("--balance",  type=float, default=10_000.0)
    ap.add_argument("--tfs",      nargs="+", default=TIMEFRAMES)
    args = ap.parse_args()

    frames   = load_all(args.symbol, args.tfs)
    strategy = STRATEGIES[args.strategy]()
    engine   = BacktestEngine(
        symbol=args.symbol, primary_tf=PRIMARY_TF, starting_balance=args.balance
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
