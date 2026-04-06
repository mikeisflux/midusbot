"""
Backtest engine — replay historical Binance prices against Polymarket outcomes.

Usage:
    from src.backtest import BacktestEngine
    engine = BacktestEngine()
    results = engine.run(lookback_days=7)
    print(results.summary())

Data sources:
    - data/session_log.jsonl  — logged window outcomes (actual UP/DOWN per window)
    - data/polymarket_outcomes.jsonl — logged trade outcomes
    - Binance REST API for historical klines
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

DATA_DIR = Path("data")
_SESSION_LOG   = DATA_DIR / "session_log.jsonl"
_OUTCOMES_LOG  = DATA_DIR / "polymarket_outcomes.jsonl"
_JOURNAL_LOG   = DATA_DIR / "journal.jsonl"


@dataclass
class BacktestTrade:
    symbol:     str
    window_start: float
    direction:  str     # "UP" | "DOWN"
    entry_price: float  # probability paid (e.g. 0.51)
    outcome:    str     # "UP" | "DOWN" (actual result)
    win:        bool
    pnl:        float   # net P&L in USDC (1.0 per share if win, -entry if loss)
    strategy:   str = "momentum"


@dataclass
class BacktestResults:
    trades:    list[BacktestTrade] = field(default_factory=list)
    start_ts:  float = 0.0
    end_ts:    float = 0.0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.win)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_trades if self.n_trades else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def by_asset(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for t in self.trades:
            s = t.symbol
            if s not in result:
                result[s] = {"trades": 0, "wins": 0, "pnl": 0.0}
            result[s]["trades"] += 1
            result[s]["pnl"]    += t.pnl
            if t.win:
                result[s]["wins"] += 1
        for s, d in result.items():
            d["win_rate"] = round(d["wins"] / d["trades"], 3) if d["trades"] else 0.0
            d["pnl"] = round(d["pnl"], 4)
        return result

    @property
    def by_strategy(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for t in self.trades:
            s = getattr(t, "strategy", "momentum") or "momentum"
            if s not in result:
                result[s] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                             "avg_win": 0.0, "avg_loss": 0.0}
            result[s]["trades"] += 1
            result[s]["pnl"]    += t.pnl
            if t.win:
                result[s]["wins"] += 1
                result[s]["avg_win"] += t.pnl
            else:
                result[s]["losses"] += 1
                result[s]["avg_loss"] += t.pnl
        for s, d in result.items():
            d["win_rate"]  = round(d["wins"] / d["trades"], 3) if d["trades"] else 0.0
            d["pnl"]       = round(d["pnl"], 4)
            d["avg_win"]   = round(d["avg_win"] / d["wins"], 4) if d["wins"] else 0.0
            d["avg_loss"]  = round(d["avg_loss"] / d["losses"], 4) if d["losses"] else 0.0
        return result

    def sharpe(self, trades_subset: list | None = None) -> float:
        """Annualised Sharpe ratio (assumes 5-min trades, ~105K per year)."""
        import statistics as _st
        trades = trades_subset if trades_subset is not None else self.trades
        pnls = [t.pnl for t in trades]
        if len(pnls) < 2:
            return 0.0
        mean = sum(pnls) / len(pnls)
        std  = _st.stdev(pnls)
        if std == 0:
            return 0.0
        # Annualise: 5-min windows ≈ 288/day, 252 trading days ≈ 72,576/year
        return round((mean / std) * (72576 ** 0.5), 2)

    def summary(self) -> str:
        if not self.trades:
            return "No backtest data."
        lines = [
            f"Backtest: {self.n_trades} trades | WR={self.win_rate:.1%} | "
            f"P&L=${self.total_pnl:+.2f} | Sharpe={self.sharpe():.2f}",
            "",
            "── By Asset ──",
        ]
        for sym, d in sorted(self.by_asset.items()):
            lines.append(
                f"  {sym}: {d['trades']}t  WR={d['win_rate']:.1%}  P&L=${d['pnl']:+.4f}"
            )
        lines += ["", "── By Strategy ──"]
        for strat, d in sorted(self.by_strategy.items()):
            lines.append(
                f"  {strat}: {d['trades']}t  WR={d['win_rate']:.1%}  "
                f"P&L=${d['pnl']:+.2f}  avgW=${d['avg_win']:+.4f}  avgL=${d['avg_loss']:+.4f}"
            )
        return "\n".join(lines)


class BacktestEngine:
    """
    Replay session_log.jsonl signals against actual window outcomes.

    Each entry in session_log has:
      symbol, window_start, signal_direction, actual_direction, pct_change

    We simulate a bet on every signaled window, paying 0.51 per share.
    Win = signal_direction == actual_direction → pnl = (1.0 - 0.51) = 0.49
    Loss = pnl = -0.51
    """

    ENTRY_PRICE = 0.51   # assumed fill price (mid-market + spread)

    def __init__(self) -> None:
        pass

    def run(
        self,
        lookback_days: float = 7.0,
        assets: list[str] | None = None,
        min_threshold: float | None = None,
    ) -> BacktestResults:
        """
        Run backtest on session_log.jsonl data.

        lookback_days: how far back to look
        assets: filter to specific symbols (None = all)
        min_threshold: minimum |pct_change| to include (filters noise)
        """
        if not _SESSION_LOG.exists():
            logger.warning("[BACKTEST] session_log.jsonl not found — run the bot to generate data")
            return BacktestResults()

        cutoff = time.time() - lookback_days * 86400
        entries: list[dict] = []
        try:
            with _SESSION_LOG.open() as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                        if e.get("window_start", 0) >= cutoff:
                            entries.append(e)
                    except Exception:
                        pass
        except Exception as exc:
            logger.error(f"[BACKTEST] Failed to read session_log: {exc}")
            return BacktestResults()

        results = BacktestResults(
            start_ts=cutoff,
            end_ts=time.time(),
        )

        for e in entries:
            sym       = e.get("symbol", "")
            sig_dir   = e.get("signal_direction")
            act_dir   = e.get("actual_direction")
            pct_chg   = abs(e.get("pct_change", 0))
            win_start = e.get("window_start", 0)

            if not sig_dir or not act_dir:
                continue   # no signal this window
            if assets and sym not in [a.upper() for a in assets]:
                continue
            if min_threshold and pct_chg < min_threshold:
                continue

            win = sig_dir == act_dir
            pnl = (1.0 - self.ENTRY_PRICE) if win else -self.ENTRY_PRICE

            results.trades.append(BacktestTrade(
                symbol=sym,
                window_start=win_start,
                direction=sig_dir,
                entry_price=self.ENTRY_PRICE,
                outcome=act_dir,
                win=win,
                pnl=round(pnl, 4),
            ))

        logger.info(f"[BACKTEST] {results.summary()}")
        return results

    def run_journal(
        self,
        lookback_days: float = 30.0,
        strategies: list[str] | None = None,
        dry_run: bool | None = None,
    ) -> BacktestResults:
        """
        Backtest from journal.jsonl — covers ALL 4 strategies since they all
        write closed trade records there.

        lookback_days: how far back to look (default 30 days)
        strategies: filter to specific strategy tags (None = all)
        dry_run: True = only sim trades, False = only live, None = both
        """
        if not _JOURNAL_LOG.exists():
            logger.warning("[BACKTEST] journal.jsonl not found — no closed trades yet")
            return BacktestResults()

        cutoff = time.time() - lookback_days * 86400
        trades: list[BacktestTrade] = []

        try:
            with _JOURNAL_LOG.open() as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue

                    if not r.get("closed", False):
                        continue

                    closed_at = float(r.get("closed_at", 0) or r.get("entry_time", 0))
                    if closed_at < cutoff:
                        continue

                    if dry_run is not None:
                        if r.get("dry_run", False) != dry_run:
                            continue

                    strat = r.get("strategy", "momentum") or "momentum"
                    if strategies and strat not in strategies:
                        continue

                    entry   = float(r.get("entry_price", 0.51) or 0.51)
                    exit_p  = float(r.get("exit_price",  0.0)  or 0.0)
                    pnl     = float(r.get("pnl_usdc",    0.0)  or 0.0)
                    win     = pnl > 0

                    from src.strategy import _detect_updown_market
                    sym = _detect_updown_market(r.get("question", "")) or strat.upper()

                    trades.append(BacktestTrade(
                        symbol=sym,
                        window_start=float(r.get("entry_time", 0) or closed_at),
                        direction=r.get("side", "YES"),
                        entry_price=entry,
                        outcome="WIN" if win else "LOSS",
                        win=win,
                        pnl=round(pnl, 4),
                        strategy=strat,
                    ))
        except Exception as exc:
            logger.error(f"[BACKTEST] Failed to read journal: {exc}")
            return BacktestResults()

        results = BacktestResults(
            trades=trades,
            start_ts=cutoff,
            end_ts=time.time(),
        )
        logger.info(f"[BACKTEST-JOURNAL] {results.summary()}")
        return results

    def run_threshold_sweep(
        self,
        thresholds: list[float] | None = None,
        lookback_days: float = 7.0,
    ) -> dict[str, Any]:
        """
        Run backtest at multiple threshold levels to find the optimal cutoff.
        Returns dict: {threshold → BacktestResults.summary()}
        """
        if thresholds is None:
            thresholds = [0.0001, 0.0003, 0.0005, 0.0008, 0.001, 0.002, 0.003]

        results = {}
        for thresh in thresholds:
            r = self.run(lookback_days=lookback_days, min_threshold=thresh)
            results[f"{thresh:.4f}"] = {
                "trades":   r.n_trades,
                "win_rate": round(r.win_rate, 3),
                "total_pnl": round(r.total_pnl, 4),
            }
        return results
