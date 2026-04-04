"""
Self-learning module — tracks trade outcomes and adapts strategy / risk
parameters online.

Trade journal
─────────────
Every opened and closed trade is recorded in `data/trade_journal.json`.
Each entry stores the sub-signals at entry time so we can later evaluate
which signal source is most predictive.

Adaptation algorithm (runs every ADAPT_EVERY_N trades)
──────────────────────────────────────────────────────
1.  Signal weights
    • Split recent trades by dominant signal (momentum vs imbalance).
    • Measure win-rate for each group.
    • Re-weight proportionally so the more accurate signal gets more
      influence.

2.  Signal threshold
    • If overall win-rate < 45 %: tighten threshold (+5 %) to be pickier.
    • If overall win-rate > 60 %: loosen threshold (−2 %) to capture more.
    • Clamped to [0.05, 0.50].

3.  Kelly multiplier
    • If average P&L% > +5 %: bump multiplier slightly (×1.02, cap 1.5).
    • If average P&L% < −5 %: shrink multiplier (×0.95, floor 0.3).

4.  Stop-loss / take-profit
    • Fit to the recent P&L distribution:
        SL  = mean(P&L%) − 2·std(P&L%)   (floored at −80 %)
        TP  = mean(P&L%) + 2·std(P&L%)   (floored at +20 %)

All parameters are persisted in `data/learned_params.json` so the bot
resumes with its learned state after a restart.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np
from loguru import logger
from src.utils import atomic_json_write

# ---------------------------------------------------------------------------
# Asset symbol extractor (mirrors analyst._asset, no circular import)
# ---------------------------------------------------------------------------

_ASSET_KEYS = {
    "BTC":  ["BITCOIN", "BTC"],
    "ETH":  ["ETHEREUM", "ETH"],
    "SOL":  ["SOLANA", "SOL"],
    "DOGE": ["DOGECOIN", "DOGE"],
    "XRP":  ["RIPPLE", "XRP"],
    "BNB":  ["BNB"],
    "HYPE": ["HYPERLIQUID", "HYPE"],
}

def _asset_symbol(question: str) -> str:
    q = question.upper()
    for sym, keys in _ASSET_KEYS.items():
        if any(k in q for k in keys):
            return sym
    return "UNKNOWN"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR       = Path("data")
ADAPT_EVERY_N  = 5           # run adaptation after this many new closed trades
LOOKBACK       = 50          # only look at the most recent N trades


# ---------------------------------------------------------------------------
# Parameter containers
# ---------------------------------------------------------------------------

@dataclass
class StrategyParams:
    """Mutable parameters consumed by MomentumImbalanceStrategy."""
    momentum_weight: float  = 0.50
    imbalance_weight: float = 0.50
    signal_threshold: float = 0.15
    max_signal_adjust: float = 0.07


@dataclass
class RiskParams:
    """Mutable parameters consumed by RiskManager."""
    kelly_multiplier: float  = 1.0
    stop_loss_pct: float     = -0.50
    take_profit_pct: float   =  0.80


# ---------------------------------------------------------------------------
# Journal entry
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    market_id: str
    token_id: str
    side: str                 # "YES" | "NO"
    question: str
    entry_price: float
    exit_price: float         # 0.0 while still open
    shares: float
    cost_usdc: float
    pnl_usdc: float           # filled on close
    pnl_pct: float            # filled on close
    momentum_signal: float
    imbalance_signal: float
    composite_signal: float
    confidence: str
    opened_at: float          # unix timestamp
    closed_at: float          # 0.0 while open
    closed: bool = False
    dry_run: bool = False     # True when recorded under DRY_RUN simulation

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> TradeRecord:
        return TradeRecord(**{k: v for k, v in d.items() if k in TradeRecord.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Learner
# ---------------------------------------------------------------------------

class AdaptiveLearner:
    """
    Tracks every trade, persists the journal, and periodically adapts the
    strategy and risk parameters.
    """

    def __init__(self, name: str = "main") -> None:
        """
        name — used to namespace separate journal/params files per strategy type.
          "main" → data/trade_journal.json  (backward-compatible)
          "news" → data/news_journal.json + data/news_params.json
          any other string → data/{name}_journal.json etc.
        """
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._name = name
        # Backward-compatible paths for the main learner
        if name == "main":
            self._journal_file = DATA_DIR / "journal_main.json"
            self._params_file  = DATA_DIR / "params_main.json"
            # Migrate old file names on first run
            _old_j = DATA_DIR / "trade_journal.json"
            _old_p = DATA_DIR / "learned_params.json"
            if _old_j.exists() and not self._journal_file.exists():
                _old_j.rename(self._journal_file)
            if _old_p.exists() and not self._params_file.exists():
                _old_p.rename(self._params_file)
        else:
            self._journal_file = DATA_DIR / f"{name}_journal.json"
            self._params_file  = DATA_DIR / f"{name}_params.json"

        self.strategy_params = StrategyParams()
        self.risk_params     = RiskParams()

        self._journal: list[TradeRecord]  = []
        self._closed_since_adapt: int     = 0
        self._adaptation_count: int       = 0

        self._load()

    def reset(self) -> None:
        """Wipe journal + learned params files and restore factory defaults."""
        try:
            self._journal_file.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            self._params_file.unlink(missing_ok=True)
        except Exception:
            pass
        self.strategy_params      = StrategyParams()
        self.risk_params          = RiskParams()
        self._journal             = []
        self._closed_since_adapt  = 0
        self._adaptation_count    = 0
        logger.info("AdaptiveLearner reset — journal and params cleared.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_open(
        self,
        *,
        market_id: str,
        token_id: str,
        side: str,
        question: str,
        entry_price: float,
        shares: float,
        cost_usdc: float,
        momentum_signal: float,
        imbalance_signal: float,
        composite_signal: float,
        confidence: str,
        dry_run: bool = False,
    ) -> None:
        rec = TradeRecord(
            market_id=market_id,
            token_id=token_id,
            side=side,
            question=question,
            entry_price=entry_price,
            exit_price=0.0,
            shares=shares,
            cost_usdc=cost_usdc,
            pnl_usdc=0.0,
            pnl_pct=0.0,
            momentum_signal=momentum_signal,
            imbalance_signal=imbalance_signal,
            composite_signal=composite_signal,
            confidence=confidence,
            opened_at=time.time(),
            closed_at=0.0,
            closed=False,
            dry_run=dry_run,
        )
        self._journal.append(rec)
        # Prune in-memory journal to last 500 entries to prevent unbounded growth
        if len(self._journal) > 500:
            self._journal = self._journal[-500:]
        self._save_journal()
        logger.debug(f"[Learner] Recorded open: {side} {question[:40]}")

    def record_close(
        self,
        token_id: str,
        exit_price: float,
    ) -> float:
        """
        Mark trade as closed, compute P&L, trigger adaptation if due.
        Returns the realised P&L in USDC.
        """
        rec = self._find_open(token_id)
        if not rec:
            logger.warning(f"[Learner] No open record for token {token_id}")
            return 0.0

        rec.exit_price = exit_price
        rec.closed_at  = time.time()
        rec.closed     = True
        rec.pnl_usdc   = rec.shares * (exit_price - rec.entry_price)
        rec.pnl_pct    = (exit_price - rec.entry_price) / rec.entry_price if rec.entry_price else 0.0

        self._closed_since_adapt += 1
        self._save_journal()

        logger.info(
            f"[Learner] Closed trade: {rec.side} {rec.question[:40]} "
            f"P&L=${rec.pnl_usdc:+.2f} ({rec.pnl_pct:+.1%})"
        )

        # Maybe adapt
        if self._closed_since_adapt >= ADAPT_EVERY_N:
            self._adapt()

        return rec.pnl_usdc

    def get_dashboard_dict(self) -> dict:
        """Return current learned params for the dashboard."""
        return {
            "momentum_weight":  self.strategy_params.momentum_weight,
            "imbalance_weight": self.strategy_params.imbalance_weight,
            "signal_threshold": self.strategy_params.signal_threshold,
            "kelly_multiplier": self.risk_params.kelly_multiplier,
            "stop_loss_pct":    self.risk_params.stop_loss_pct,
            "take_profit_pct":  self.risk_params.take_profit_pct,
            "adaptation_count": self._adaptation_count,
            "last_adapted":     self._last_adapted_str(),
            "journal_size":     len(self._journal),
        }

    @property
    def journal(self) -> list[TradeRecord]:
        return self._journal

    # ------------------------------------------------------------------
    # Adaptation
    # ------------------------------------------------------------------

    def _adapt(self) -> None:
        closed = [r for r in self._journal if r.closed][-LOOKBACK:]
        if len(closed) < ADAPT_EVERY_N:
            return

        logger.info(f"[Learner] Adapting on {len(closed)} recent trades …")

        # ── 1. Signal weights ─────────────────────────────────────────
        mom_led  = [r for r in closed if abs(r.momentum_signal) > abs(r.imbalance_signal)]
        imb_led  = [r for r in closed if abs(r.imbalance_signal) >= abs(r.momentum_signal)]

        mom_wr = mean([1.0 if r.pnl_usdc > 0 else 0.0 for r in mom_led]) if mom_led else 0.5
        imb_wr = mean([1.0 if r.pnl_usdc > 0 else 0.0 for r in imb_led]) if imb_led else 0.5

        total_wr = mom_wr + imb_wr
        if total_wr > 0:
            # Smooth toward new weights (EMA-like, alpha=0.3)
            target_mom = mom_wr / total_wr
            target_imb = imb_wr / total_wr
            alpha = 0.3
            self.strategy_params.momentum_weight  = (
                (1 - alpha) * self.strategy_params.momentum_weight + alpha * target_mom
            )
            self.strategy_params.imbalance_weight = (
                (1 - alpha) * self.strategy_params.imbalance_weight + alpha * target_imb
            )
            # Clamp each weight to [0.1, 0.9] then normalise so they always sum to 1.0
            self.strategy_params.momentum_weight  = float(np.clip(self.strategy_params.momentum_weight,  0.1, 0.9))
            self.strategy_params.imbalance_weight = float(np.clip(self.strategy_params.imbalance_weight, 0.1, 0.9))
            total_w = self.strategy_params.momentum_weight + self.strategy_params.imbalance_weight
            self.strategy_params.momentum_weight  /= total_w
            self.strategy_params.imbalance_weight /= total_w

        # ── 2. Signal threshold ───────────────────────────────────────
        overall_wr = mean([1.0 if r.pnl_usdc > 0 else 0.0 for r in closed])
        if overall_wr < 0.45:
            self.strategy_params.signal_threshold *= 1.05
        elif overall_wr > 0.60:
            self.strategy_params.signal_threshold *= 0.98

        self.strategy_params.signal_threshold = float(
            np.clip(self.strategy_params.signal_threshold, 0.05, 0.50)
        )

        # ── 3. Kelly multiplier ───────────────────────────────────────
        avg_pnl_pct = mean([r.pnl_pct for r in closed])
        if avg_pnl_pct > 0.05:
            self.risk_params.kelly_multiplier = min(
                1.5, self.risk_params.kelly_multiplier * 1.02
            )
        elif avg_pnl_pct < -0.05:
            self.risk_params.kelly_multiplier = max(
                0.3, self.risk_params.kelly_multiplier * 0.95
            )

        # ── 4. Stop-loss / take-profit ────────────────────────────────
        pnls = [r.pnl_pct for r in closed]
        if len(pnls) >= 5:
            mu  = float(np.mean(pnls))
            std = float(np.std(pnls))
            if std > 0:
                self.risk_params.stop_loss_pct   = float(
                    np.clip(mu - 2.0 * std, -0.80, -0.10)
                )
                self.risk_params.take_profit_pct = float(
                    np.clip(mu + 2.0 * std, 0.20, 2.00)
                )

        self._adaptation_count += 1
        self._closed_since_adapt = 0

        logger.info(
            f"[Learner] Adaptation #{self._adaptation_count} complete — "
            f"mom_w={self.strategy_params.momentum_weight:.2f}  "
            f"imb_w={self.strategy_params.imbalance_weight:.2f}  "
            f"thresh={self.strategy_params.signal_threshold:.3f}  "
            f"kelly_m={self.risk_params.kelly_multiplier:.2f}  "
            f"SL={self.risk_params.stop_loss_pct:.0%}  "
            f"TP={self.risk_params.take_profit_pct:.0%}"
        )

        # ── 5. Per-asset threshold adaptation via session accuracy ──────────
        # Use ground-truth window direction accuracy (from session_tracker) rather
        # than trade win/loss to calibrate thresholds. Session data captures every
        # window the bot scanned — not just the ones it bet on — giving 10-100×
        # more data points and no bias from entry price or position sizing.
        try:
            from src.analyst import load_params as _load_ap, save_params as _save_ap
            from src.session_tracker import get_all_stats as _session_stats
            _FLOOR = 0.0001   # absolute minimum threshold (0.01%)
            _CEIL  = 0.005    # absolute maximum threshold (0.50%)
            _MIN_WINDOWS = 10  # need at least 10 windows of session data to adapt

            ap           = _load_ap()
            asset_thresh = dict(ap.get("asset_thresholds", {}))
            global_thresh = float(ap.get("signal_threshold", 0.0008))
            stats        = _session_stats(lookback=60)

            changed = []
            for asset, s in stats.items():
                # Only act on assets where the bot actually generated signals
                if s.get("signal_windows", 0) < _MIN_WINDOWS:
                    continue
                accuracy = s.get("signal_accuracy", 0.5)
                cur = asset_thresh.get(asset, global_thresh)
                if accuracy < 0.45:
                    # Direction prediction worse than random — raise threshold,
                    # demand a stronger signal before betting
                    new_t = min(_CEIL, cur * 1.10)
                elif accuracy > 0.60:
                    # Direction prediction consistently right — lower threshold
                    # slightly to capture more of these good signals
                    new_t = max(_FLOOR, cur * 0.97)
                else:
                    continue   # 45–60% accuracy: leave threshold alone
                if abs(new_t - cur) > 1e-8:
                    asset_thresh[asset] = round(new_t, 6)
                    changed.append(
                        f"{asset}: {cur:.5f}→{new_t:.5f} "
                        f"(acc={accuracy:.0%}, n={s['signal_windows']}w)"
                    )

            if changed:
                ap["asset_thresholds"] = asset_thresh
                _save_ap(ap)
                logger.info(f"[Learner] Per-asset thresholds from session data: {', '.join(changed)}")
        except Exception as _e:
            logger.debug(f"[Learner] Per-asset threshold update skipped: {_e}")

        # ── 6. LLM analysis (non-blocking) ────────────────────────────
        # Run in a daemon thread so the bot loop never stalls waiting for
        # the LLM. The analyst writes analyst_params.json when done; the
        # strategy picks them up on the next loop automatically.
        import threading
        def _run_analyst():
            try:
                from src.analyst import analyse_and_update
                analyse_and_update(self)
            except Exception as exc:
                logger.debug(f"[Learner] LLM analysis skipped: {exc}")
        threading.Thread(target=_run_analyst, daemon=True, name="analyst").start()
        logger.info("[Learner] LLM analyst started in background thread")
        self._save_params()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_journal(self) -> None:
        try:
            atomic_json_write(self._journal_file, [r.to_dict() for r in self._journal])
        except Exception as exc:
            logger.error(f"[Learner] Failed to save journal: {exc}")

    def _save_params(self) -> None:
        try:
            payload = {
                **asdict(self.strategy_params),
                **asdict(self.risk_params),
                "adaptation_count": self._adaptation_count,
                "saved_at": datetime.utcnow().isoformat(),
            }
            atomic_json_write(self._params_file, payload)
        except Exception as exc:
            logger.error(f"[Learner] Failed to save params: {exc}")

    def _load(self) -> None:
        # Journal
        if self._journal_file.exists():
            try:
                with open(self._journal_file) as f:
                    raw = json.load(f)
                self._journal = [TradeRecord.from_dict(r) for r in raw]
                logger.info(f"[Learner] Loaded {len(self._journal)} journal entries.")
            except Exception as exc:
                logger.warning(f"[Learner] Could not load journal: {exc}")

        # Params
        if self._params_file.exists():
            try:
                with open(self._params_file) as f:
                    d = json.load(f)
                for k in StrategyParams.__dataclass_fields__:
                    if k in d:
                        setattr(self.strategy_params, k, d[k])
                for k in RiskParams.__dataclass_fields__:
                    if k in d:
                        setattr(self.risk_params, k, d[k])
                self._adaptation_count = d.get("adaptation_count", 0)
                logger.info(
                    f"[Learner] Loaded learned params (adapted {self._adaptation_count} times)."
                )
            except Exception as exc:
                logger.warning(f"[Learner] Could not load params: {exc}")

        # Recompute _closed_since_adapt from the journal so partially-completed
        # adaptation cycles survive restarts (including dry-run → live switches).
        # If the journal has enough unprocessed closed trades, adapt immediately
        # so params always reflect the full journal on startup.
        closed_total = sum(1 for r in self._journal if r.closed)
        accounted_for = self._adaptation_count * ADAPT_EVERY_N
        unprocessed = max(0, closed_total - accounted_for)
        self._closed_since_adapt = unprocessed % ADAPT_EVERY_N

        if unprocessed >= ADAPT_EVERY_N:
            logger.info(
                f"[Learner] {unprocessed} unprocessed closed trades found on load — "
                "running adaptation now to carry dry-run learning into live mode."
            )
            self._adapt()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def has_open(self, token_id: str) -> bool:
        """Return True if there is an open record for this token_id."""
        return self._find_open(token_id) is not None

    def _find_open(self, token_id: str) -> TradeRecord | None:
        for rec in reversed(self._journal):
            if rec.token_id == token_id and not rec.closed:
                return rec
        return None

    def _last_adapted_str(self) -> str:
        if self._adaptation_count == 0:
            return "never"
        closed = [r for r in self._journal if r.closed]
        if closed:
            ts = closed[-1].closed_at
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return "unknown"
