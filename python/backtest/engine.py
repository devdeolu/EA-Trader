# python/backtest/engine.py
# ─────────────────────────────────────────────────────────────────────────────
# Event-driven backtest engine.
#
# Design goals:
#   - Reuses the live Strategy.on_tick(snapshot, regime) interface verbatim.
#     A strategy that passes here is the same code that runs live.
#   - Bar-close stepping on the PRIMARY_TF (M15 default). Higher TFs are
#     sliced up to "now" so no look-ahead.
#   - Realistic exits: walks intra-bar OHLC after entry to detect SL/TP hit.
#     Conservative tie-breaker: if both SL and TP within the same bar, SL wins
#     (worst-case assumption — defensible for prop-firm risk modelling).
#   - Spread + commission applied at entry.
#   - One open position at a time (matches live design for now).
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from config.settings import (
    BACKTEST_COMMISSION_PER_LOT,
    BACKTEST_SPREAD_PIPS,
    LOG_LEVEL,
    PRIMARY_TF,
    SYMBOL,
)
from python.core.data_engine import MarketSnapshot
from python.core.indicators import add_indicators
from python.core.regime import classify
from python.core.sizing import compute_lots
from python.strategies.base import Candidate, Strategy
from python.backtest.metrics import Metrics, compute_metrics

logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger(__name__)


# ── Trade record ──────────────────────────────────────────────────────────────
@dataclass
class Trade:
    entry_time:  datetime
    exit_time:   datetime
    action:      str          # "BUY" / "SELL"
    entry_price: float
    exit_price:  float
    sl_price:    float
    tp_price:    float
    lots:        float
    pnl:         float        # account-currency PnL (after spread + commission)
    r_multiple:  float        # realised PnL / planned risk
    exit_reason: str          # "TP" / "SL" / "EOD"
    strategy:    str
    tier:        str
    regime:      str


@dataclass
class BacktestResult:
    trades:        list[Trade]
    equity_curve:  list[float]
    metrics:       Metrics
    starting_balance: float


# ── Engine ───────────────────────────────────────────────────────────────────
class BacktestEngine:
    """
    Replays historical bars and executes a Strategy on each closed PRIMARY_TF bar.
    """

    def __init__(
        self,
        symbol:           str = SYMBOL,
        primary_tf:       str = PRIMARY_TF,
        starting_balance: float = 10_000.0,
        spread_pips:      float = BACKTEST_SPREAD_PIPS,
        commission_per_lot: float = BACKTEST_COMMISSION_PER_LOT,
        contract_size:    float = 100_000,
        pip_size:         float = 0.0001,
        tick_size:        float | None = None,
        tick_value:       float | None = None,
        min_lot:          float = 0.01,
        max_lot:          float = 50.0,
        lot_step:         float = 0.01,
        warmup_bars:      int   = 250,
    ):
        self.symbol             = symbol
        self.primary_tf         = primary_tf
        self.starting_balance   = starting_balance
        self.spread_pips        = spread_pips
        self.commission_per_lot = commission_per_lot
        self.contract_size      = contract_size
        self.pip_size           = pip_size
        self.tick_size          = tick_size
        self.tick_value         = tick_value
        self.min_lot            = min_lot
        self.max_lot            = max_lot
        self.lot_step           = lot_step
        self.warmup_bars        = warmup_bars

    # ─────────────────────────────────────────────────────────────────────────
    def run(
        self,
        strategy: Strategy,
        frames:   dict[str, pd.DataFrame],
        pre_enriched: bool = False,
    ) -> BacktestResult:
        """
        frames: dict of TF → raw OHLCV DataFrame indexed by UTC timestamp.
                Indicators are computed once up-front.
                If pre_enriched=True, frames are assumed to already contain
                indicator columns (saves ~minutes when sweeping params).
        """
        # Pre-compute indicators on each TF (huge speedup vs. per-bar recompute)
        if pre_enriched:
            enriched = frames
        else:
            enriched = {tf: add_indicators(df) for tf, df in frames.items()}

        primary = enriched[self.primary_tf]
        if len(primary) <= self.warmup_bars:
            raise ValueError("Not enough history after warmup")

        balance       = self.starting_balance
        equity_curve  = [balance]
        trades:       list[Trade]      = []
        open_trade:   Optional[dict]   = None

        for i in range(self.warmup_bars, len(primary) - 1):
            now = primary.index[i]
            bar = primary.iloc[i]

            # ── 1. Manage open position first ───────────────────────────────
            if open_trade is not None:
                exit_info = self._check_exit(open_trade, bar)
                if exit_info is not None:
                    closed = self._close_trade(open_trade, exit_info, now)
                    trades.append(closed)
                    balance += closed.pnl
                    equity_curve.append(balance)
                    open_trade = None

            # ── 2. Look for new entry only if flat ──────────────────────────
            if open_trade is not None:
                continue

            snap = self._build_snapshot(enriched, now, balance)
            if snap is None:
                continue
            reading   = classify(snap)
            candidate = strategy.on_tick(snap, reading)
            if candidate is None:
                continue

            open_trade = self._open_trade(candidate, now, reading.regime.value, balance)

        # Force-close any dangling position at last bar's close
        if open_trade is not None:
            last_bar = primary.iloc[-1]
            closed = self._close_trade(
                open_trade,
                {"price": float(last_bar["close"]), "reason": "EOD"},
                primary.index[-1],
            )
            trades.append(closed)
            balance += closed.pnl
            equity_curve.append(balance)

        metrics = compute_metrics(trades, self.starting_balance, equity_curve)
        return BacktestResult(trades, equity_curve, metrics, self.starting_balance)

    # ── Internals ────────────────────────────────────────────────────────────

    def _build_snapshot(
        self,
        enriched: dict[str, pd.DataFrame],
        now:      pd.Timestamp,
        balance:  float,
    ) -> Optional[MarketSnapshot]:
        # Slice each TF to bars whose close is <= now (no look-ahead).
        # Strategies use iloc[-2] for "last closed" bar (live convention
        # where iloc[-1] is the currently-forming bar). To match in backtest,
        # we duplicate the last bar as a placeholder forming row.
        sliced: dict[str, pd.DataFrame] = {}
        for tf, df in enriched.items():
            sub = df.loc[:now]
            if len(sub) < 3:
                return None
            placeholder = sub.iloc[[-1]].copy()
            sub_ext = pd.concat([sub, placeholder])
            sliced[tf] = sub_ext

        primary_now = sliced[self.primary_tf].iloc[-1]
        tick = {
            "bid":    float(primary_now["close"]) - (self.spread_pips * self.pip_size) / 2,
            "ask":    float(primary_now["close"]) + (self.spread_pips * self.pip_size) / 2,
            "spread": self.spread_pips,
            "time":   now.to_pydatetime(),
        }
        account = {
            "balance":   balance,
            "equity":    balance,
            "margin":    0.0,
            "free_margin": balance,
            "currency":  "USD",
            "leverage":  100,
            "daily_pnl": 0.0,
        }
        symbol_info = {
            "digits":         5,
            "point":          self.pip_size / 10,
            "spread":         int(self.spread_pips * 10),
            "pip_size":       self.pip_size,
            "contract_size":  self.contract_size,
            "min_lot":        self.min_lot,
            "max_lot":        self.max_lot,
            "lot_step":       self.lot_step,
        }
        return MarketSnapshot(
            symbol      = self.symbol,
            taken_at    = now.to_pydatetime(),
            primary_tf  = self.primary_tf,
            frames      = sliced,
            tick        = tick,
            account     = account,
            symbol_info = symbol_info,
        )

    def _open_trade(
        self,
        c:        Candidate,
        now:      pd.Timestamp,
        regime:   str,
        balance:  float,
    ) -> dict:
        lots = compute_lots(
            balance       = balance,
            risk_pct      = c.risk_pct,
            entry_price   = c.entry_price,
            sl_price      = c.sl_price,
            contract_size = self.contract_size,
            pip_size      = self.pip_size,
            min_lot       = self.min_lot,
            max_lot       = self.max_lot,
            lot_step      = self.lot_step,
            tick_size     = self.tick_size,
            tick_value    = self.tick_value,
        )
        # Apply spread cost at entry by worsening fill price
        spread_cost = self.spread_pips * self.pip_size
        if c.action == "BUY":
            entry_fill = c.entry_price + spread_cost / 2
        else:
            entry_fill = c.entry_price - spread_cost / 2

        planned_risk = self._currency_pnl(
            entry_fill, c.sl_price, lots, c.action
        )
        planned_risk = abs(planned_risk)
        return {
            "entry_time":   now.to_pydatetime(),
            "action":       c.action,
            "entry_price":  entry_fill,
            "sl":           c.sl_price,
            "tp":           c.tp_price,
            "lots":         lots,
            "planned_risk": planned_risk,
            "strategy":     c.strategy,
            "tier":         c.tier,
            "regime":       regime,
        }

    def _currency_pnl(self, entry: float, exit_: float, lots: float, action: str) -> float:
        """Convert price diff → account-currency PnL using tick_value if available."""
        diff = (exit_ - entry) if action == "BUY" else (entry - exit_)
        if self.tick_size and self.tick_value and self.tick_size > 0:
            return (diff / self.tick_size) * self.tick_value * lots
        return diff * self.contract_size * lots

    @staticmethod
    def _check_exit(trade: dict, bar: pd.Series) -> Optional[dict]:
        """Return {'price', 'reason'} if SL/TP touched within this bar."""
        high, low = float(bar["high"]), float(bar["low"])
        sl, tp    = trade["sl"], trade["tp"]

        if trade["action"] == "BUY":
            sl_hit = low  <= sl
            tp_hit = high >= tp
        else:  # SELL
            sl_hit = high >= sl
            tp_hit = low  <= tp

        if sl_hit and tp_hit:
            # Pessimistic assumption — SL fills first
            return {"price": sl, "reason": "SL"}
        if sl_hit:
            return {"price": sl, "reason": "SL"}
        if tp_hit:
            return {"price": tp, "reason": "TP"}
        return None

    def _close_trade(
        self,
        trade:     dict,
        exit_info: dict,
        exit_time: pd.Timestamp,
    ) -> Trade:
        exit_price = exit_info["price"]
        gross      = self._currency_pnl(
            trade["entry_price"], exit_price, trade["lots"], trade["action"]
        )

        commission = self.commission_per_lot * trade["lots"]
        pnl        = gross - commission
        r_mult     = pnl / trade["planned_risk"] if trade["planned_risk"] > 0 else 0.0

        return Trade(
            entry_time  = trade["entry_time"],
            exit_time   = exit_time.to_pydatetime() if hasattr(exit_time, "to_pydatetime") else exit_time,
            action      = trade["action"],
            entry_price = trade["entry_price"],
            exit_price  = exit_price,
            sl_price    = trade["sl"],
            tp_price    = trade["tp"],
            lots        = trade["lots"],
            pnl         = pnl,
            r_multiple  = r_mult,
            exit_reason = exit_info["reason"],
            strategy    = trade["strategy"],
            tier        = trade["tier"],
            regime      = trade["regime"],
        )
