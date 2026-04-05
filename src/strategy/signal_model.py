"""
TradeSignal — output dataclass produced by every strategy analyse() call.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeSignal:
    market_id: str
    question: str
    side: str            # "YES" | "NO"
    token_id: str
    market_price: float
    fair_value: float
    edge: float
    signal: float
    confidence: str      # "LOW" | "MEDIUM" | "HIGH"
    momentum_signal: float  = 0.0
    imbalance_signal: float = 0.0
    is_latency_arb: bool = False
    lag_pct: float = 0.0
    hours_to_close: float | None = None
    best_ask: float | None = None
    best_bid: float | None = None
    no_best_ask: float | None = None   # NO token ask (fetched separately for NO trades)
    is_news_arb: bool = False
    secs_into_window: float = 0.0
    rel_strength: float = 0.0   # |window_return| / threshold — sort key for best-signal ranking
    win_mins: int = 5            # window duration in minutes (5 or 15)

    def __str__(self) -> str:
        return (
            f"[{self.confidence}] {self.side} {self.question[:60]} | "
            f"mkt={self.market_price:.3f}  fv={self.fair_value:.3f}  "
            f"edge={self.edge:+.3f}"
        )
