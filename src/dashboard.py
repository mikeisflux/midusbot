"""
Rich terminal dashboard — live-updating full-screen UI.

Rendered at the end of every bot loop iteration via `rich.live.Live`.
Shows: bot status, open positions with P&L, recent signals, performance
metrics, and the current learned parameters.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from src.bot import OpenPosition
    from src.strategy import TradeSignal

import config

_EQUITY_FILE = Path("data/equity_curve.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pnl_color(val: float) -> str:
    if val > 0:
        return "green"
    if val < 0:
        return "red"
    return "dim"


def _fmt_duration(seconds: float) -> str:
    d = timedelta(seconds=int(seconds))
    hours, rem = divmod(d.seconds, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if d.days:
        parts.append(f"{d.days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# State container (bot pushes updates here each loop)
# ---------------------------------------------------------------------------

class DashboardState:
    def __init__(self) -> None:
        self.start_time: float = time.time()
        self.loop_count: int = 0
        self.markets_scanned: int = 0
        self.candidates: int = 0
        self.exposure: float = 0.0

        # Positions: list of (OpenPosition, current_price)
        self.positions: list[tuple[OpenPosition, float]] = []

        # Most recent signals (kept trimmed to last 8)
        self.recent_signals: list[TradeSignal] = []

        # Lifetime performance numbers
        self.total_trades: int = 0
        self.wins: int = 0
        self.total_pnl: float = 0.0    # net P&L after fees
        self.total_fees: float = 0.0   # cumulative fees + gas paid
        self.best_trade: float = 0.0
        self.worst_trade: float = 0.0
        self.pnl_history: list[float] = []

        # Learned params (dict from learner)
        self.learned: dict = {}

        # Simulation portfolio stats (from SimPortfolio.get_stats())
        self.sim_stats: dict = {}

        # Web UI extras
        self.equity_curve: list[dict] = []   # [{"t": ms, "v": portfolio_value}, ...]
        self.exec_log: list[dict] = []        # [{"ts": "HH:MM:SS", "kind": str, "text": str}, ...]
        self.scan_latency_ms: int = 0
        self.orders_placed: int = 0
        self.btc_price: float = 0.0
        self.daily_pnl: float = 0.0
        self.entry_latencies: list[float] = []  # secs-into-window for recent entries
        # Seed = your original deposit. Used as the baseline for real P&L.
        # Set STARTING_WALLET_USDC in .env (or config.py) to your deposit amount.
        _cfg_start = getattr(config, "STARTING_WALLET_USDC", 0.0)
        self._seed: float = _cfg_start if _cfg_start > 0 else config.MAX_TOTAL_EXPOSURE_USDC
        self.wallet_balance: float = 0.0      # live wallet USDC (updated each loop in live mode)

        self._load_equity_curve()

    # convenience -----------------------------------------------------------

    @property
    def uptime(self) -> str:
        return _fmt_duration(time.time() - self.start_time)

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades else 0.0

    def push_signal(self, sig: TradeSignal) -> None:
        self.recent_signals.insert(0, sig)
        self.recent_signals = self.recent_signals[:8]

    def record_closed_trade(self, pnl_usdc: float, fee_usdc: float = 0.0) -> None:
        net = pnl_usdc - fee_usdc
        self.total_trades += 1
        self.total_pnl += net
        self.total_fees += fee_usdc
        self.daily_pnl += net
        self.pnl_history.append(net)
        if net > 0:
            self.wins += 1
        self.best_trade = max(self.best_trade, net)
        self.worst_trade = min(self.worst_trade, net)
        self.add_equity_point()

    def add_equity_point(self) -> None:
        """Append current portfolio value to the equity curve (max 2000 points)."""
        self.equity_curve.append({
            "t": int(time.time() * 1000),
            "v": round(self._seed + self.total_pnl, 4),
        })
        if len(self.equity_curve) > 2000:
            self.equity_curve = self.equity_curve[-2000:]
        self._save_equity_curve()

    def add_exec_log(self, kind: str, text: str) -> None:
        """
        Add an entry to the execution log (shown in web UI right panel).
        kind: 'divergence' | 'exec' | 'filled' | 'slipped' | 'scan' | 'info'
        """
        self.exec_log.insert(0, {
            "ts":   datetime.now().strftime("%H:%M:%S"),
            "kind": kind,
            "text": text,
        })
        self.exec_log = self.exec_log[:120]  # keep last 120 entries

    def restore_from_journal(self, journal) -> None:
        """
        Reconstruct performance stats from the persisted trade journal on startup.
        Only counts LIVE (non-dry-run) trades in the main stats so the P&L
        reflects real wallet movements, not paper-trading simulations.
        """
        closed = [r for r in journal if r.closed]
        if not closed:
            return

        # Stats: live trades only — paper/dry-run trades are not real P&L
        live   = [r for r in closed if not getattr(r, "dry_run", False)]
        paper  = [r for r in closed if     getattr(r, "dry_run", False)]

        stats_src = live if live else closed  # fall back to all if no live trades yet

        self.total_trades = len(stats_src)
        self.wins         = sum(1 for r in stats_src if r.pnl_usdc > 0)
        self.total_pnl    = sum(r.pnl_usdc for r in stats_src)
        self.best_trade   = max((r.pnl_usdc for r in stats_src), default=0.0)
        self.worst_trade  = min((r.pnl_usdc for r in stats_src), default=0.0)
        self.pnl_history  = [r.pnl_usdc for r in stats_src]

        # Rebuild equity curve from live trades only (anchored to real deposit)
        if not self.equity_curve and stats_src:
            cumulative = 0.0
            for r in sorted(stats_src, key=lambda x: x.closed_at):
                cumulative += r.pnl_usdc
                self.equity_curve.append({
                    "t": int(r.closed_at * 1000),
                    "v": round(self._seed + cumulative, 4),
                })

        logger.info(
            f"[Dashboard] Restored from journal: {self.total_trades} live trades "
            f"({len(paper)} paper trades excluded), "
            f"P&L=${self.total_pnl:+.2f}, {self.wins}W/{self.total_trades}T"
        )

    def reset_training_stats(self) -> None:
        """Clear all performance counters and equity curve (called on Reset Training)."""
        self.total_trades  = 0
        self.wins          = 0
        self.total_pnl     = 0.0
        self.total_fees    = 0.0
        self.daily_pnl     = 0.0
        self.best_trade    = 0.0
        self.worst_trade   = 0.0
        self.pnl_history   = []
        self.equity_curve  = []
        self.learned       = {}
        try:
            _EQUITY_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def _load_equity_curve(self) -> None:
        """Load persisted equity curve from disk on startup (if it exists)."""
        try:
            if _EQUITY_FILE.exists():
                with open(_EQUITY_FILE) as f:
                    data = json.load(f)
                if isinstance(data, list) and data:
                    self.equity_curve = data[-2000:]
                    logger.info(f"[Dashboard] Loaded {len(self.equity_curve)} equity curve points from disk.")
        except Exception as exc:
            logger.warning(f"[Dashboard] Could not load equity curve: {exc}")

    def _save_equity_curve(self) -> None:
        """Persist equity curve to disk (non-blocking best-effort write)."""
        try:
            _EQUITY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_EQUITY_FILE, "w") as f:
                json.dump(self.equity_curve, f)
        except Exception:
            pass  # don't crash the bot loop on a disk write failure

    @property
    def balance(self) -> float:
        # In live mode use the actual wallet balance; fall back to seed + P&L
        if self.wallet_balance > 0:
            return self.wallet_balance
        return self._seed + self.total_pnl


# ---------------------------------------------------------------------------
# Dashboard renderer
# ---------------------------------------------------------------------------

class Dashboard:
    """
    Wraps `rich.live.Live`.  Call `.start()` before the bot loop and
    `.refresh(state)` at the end of each iteration.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._console = Console()
        self._live: Live | None = None

    def start(self) -> None:
        if not self._enabled:
            return
        self._live = Live(
            console=self._console,
            refresh_per_second=1,
            screen=True,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.stop()

    def refresh(self, state: DashboardState) -> None:
        if not self._enabled or not self._live:
            return
        self._live.update(self._render(state))

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self, s: DashboardState) -> Panel:
        parts: list = []

        # ── Header ────────────────────────────────────────────────────
        parts.append(self._header(s))

        # ── Positions ─────────────────────────────────────────────────
        parts.append(self._positions_table(s))

        # ── Signals ───────────────────────────────────────────────────
        parts.append(self._signals_table(s))

        # ── Performance ──────────────────────────────────────────────
        parts.append(self._performance_panel(s))

        # ── Learned params ───────────────────────────────────────────
        if s.learned:
            parts.append(self._learned_panel(s))

        return Panel(
            Group(*parts),
            title="[bold cyan] MIDUSBOT [/bold cyan] [dim]Polymarket Trading Bot[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )

    # -- sub-renderers -------------------------------------------------

    def _header(self, s: DashboardState) -> Table:
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column(style="bold")
        t.add_column()
        t.add_column(style="bold")
        t.add_column()
        t.add_column(style="bold")
        t.add_column()

        mode = "[green]DRY-RUN[/green]" if config.DRY_RUN else "[bold red]LIVE[/bold red]"
        t.add_row(
            "Status", "[green]RUNNING[/green]",
            "Mode", mode,
            "Uptime", s.uptime,
        )
        t.add_row(
            "Loop", f"#{s.loop_count}",
            "Scanned", f"{s.candidates}/{s.markets_scanned}",
            "Exposure", f"${s.exposure:.2f} / ${config.MAX_TOTAL_EXPOSURE_USDC:.0f}",
        )
        return t

    def _positions_table(self, s: DashboardState) -> Panel:
        t = Table(
            title=f"Open Positions ({len(s.positions)})",
            expand=True,
            title_style="bold yellow",
        )
        t.add_column("Market", style="white", max_width=45, no_wrap=True)
        t.add_column("Side", justify="center", width=5)
        t.add_column("Shares", justify="right", width=8)
        t.add_column("Entry", justify="right", width=7)
        t.add_column("Now", justify="right", width=7)
        t.add_column("P&L $", justify="right", width=9)
        t.add_column("P&L %", justify="right", width=8)

        if not s.positions:
            t.add_row("[dim]No open positions[/dim]", "", "", "", "", "", "")
        else:
            for pos, cur_price in s.positions:
                pnl_usdc = pos.shares * (cur_price - pos.entry_price)
                pnl_pct = (cur_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0.0
                c = _pnl_color(pnl_usdc)
                side_style = "green" if pos.side == "YES" else "red"
                t.add_row(
                    pos.question[:45],
                    f"[{side_style}]{pos.side}[/{side_style}]",
                    f"{pos.shares:.2f}",
                    f"{pos.entry_price:.3f}",
                    f"{cur_price:.3f}",
                    f"[{c}]{pnl_usdc:+.2f}[/{c}]",
                    f"[{c}]{pnl_pct:+.1%}[/{c}]",
                )

        return Panel(t, border_style="yellow", padding=0)

    def _signals_table(self, s: DashboardState) -> Panel:
        t = Table(
            title="Latest Signals",
            expand=True,
            title_style="bold magenta",
        )
        t.add_column("Conf", width=8, justify="center")
        t.add_column("Side", width=5, justify="center")
        t.add_column("Market", max_width=40, no_wrap=True)
        t.add_column("Mkt Price", justify="right", width=9)
        t.add_column("Fair Val", justify="right", width=9)
        t.add_column("Edge", justify="right", width=8)
        t.add_column("Signal", justify="right", width=8)

        if not s.recent_signals:
            t.add_row("[dim]No signals yet[/dim]", "", "", "", "", "", "")
        else:
            for sig in s.recent_signals[:6]:
                conf_style = {"HIGH": "bold green", "MEDIUM": "yellow", "LOW": "dim"}.get(
                    sig.confidence, "dim"
                )
                side_style = "green" if sig.side == "YES" else "red"
                t.add_row(
                    f"[{conf_style}]{sig.confidence}[/{conf_style}]",
                    f"[{side_style}]{sig.side}[/{side_style}]",
                    sig.question[:40],
                    f"{sig.market_price:.3f}",
                    f"{sig.fair_value:.3f}",
                    f"[green]{sig.edge:+.3f}[/green]",
                    f"{sig.signal:+.3f}",
                )

        return Panel(t, border_style="magenta", padding=0)

    def _performance_panel(self, s: DashboardState) -> Panel:
        t = Table(show_header=False, box=None, padding=(0, 3), expand=True)
        t.add_column(style="bold")
        t.add_column()
        t.add_column(style="bold")
        t.add_column()
        t.add_column(style="bold")
        t.add_column()

        pnl_c = _pnl_color(s.total_pnl)
        wr_c = "green" if s.win_rate >= 0.5 else "red" if s.total_trades > 5 else "dim"
        avg_pnl = s.total_pnl / s.total_trades if s.total_trades else 0.0

        t.add_row(
            "Total P&L",  f"[{pnl_c}]${s.total_pnl:+.2f}[/{pnl_c}]",
            "Win Rate",   f"[{wr_c}]{s.win_rate:.0%}[/{wr_c}] ({s.wins}/{s.total_trades})",
            "Avg P&L",    f"[{_pnl_color(avg_pnl)}]${avg_pnl:+.2f}[/{_pnl_color(avg_pnl)}]",
        )
        t.add_row(
            "Best Trade",  f"[green]${s.best_trade:+.2f}[/green]",
            "Worst Trade", f"[red]${s.worst_trade:+.2f}[/red]",
            "", "",
        )

        return Panel(t, title="[bold blue]Performance[/bold blue]", border_style="blue", padding=0)

    def _learned_panel(self, s: DashboardState) -> Panel:
        lp = s.learned
        t = Table(show_header=False, box=None, padding=(0, 3), expand=True)
        t.add_column(style="bold")
        t.add_column()
        t.add_column(style="bold")
        t.add_column()
        t.add_column(style="bold")
        t.add_column()

        t.add_row(
            "Mom Weight",   f"{lp.get('momentum_weight', 0.5):.2f}",
            "Imb Weight",   f"{lp.get('imbalance_weight', 0.5):.2f}",
            "Threshold",    f"{lp.get('signal_threshold', 0.15):.3f}",
        )
        t.add_row(
            "Kelly Mult",   f"{lp.get('kelly_multiplier', 1.0):.2f}",
            "Stop Loss",    f"{lp.get('stop_loss_pct', -0.5):.0%}",
            "Take Profit",  f"{lp.get('take_profit_pct', 0.8):.0%}",
        )
        t.add_row(
            "Adaptations",  str(lp.get("adaptation_count", 0)),
            "Last Adapted",  lp.get("last_adapted", "never"),
            "", "",
        )

        return Panel(t, title="[bold white]Learned Parameters[/bold white]", border_style="white", padding=0)
