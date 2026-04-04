"""
Shared data-class types used by PolymarketClient and its mixins.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Token:
    token_id: str
    outcome: str   # "Yes" | "No"
    price: float


@dataclass
class Market:
    id: str
    question: str
    condition_id: str
    slug: str
    end_date: str
    active: bool
    closed: bool
    volume: float
    liquidity: float
    yes_token: Token
    no_token: Token

    @property
    def yes_price(self) -> float:
        return self.yes_token.price

    @property
    def no_price(self) -> float:
        return self.no_token.price


@dataclass
class OrderBook:
    token_id: str
    bids: list[dict]   # [{"price": "0.64", "size": "100"}, ...]
    asks: list[dict]

    @property
    def best_bid(self) -> float:
        return float(self.bids[0]["price"]) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return float(self.asks[0]["price"]) if self.asks else 1.0

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid


@dataclass
class PricePoint:
    timestamp: int
    price: float


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    size: float
    avg_price: float
    current_price: float

    @property
    def pnl(self) -> float:
        return self.size * (self.current_price - self.avg_price)

    @property
    def pnl_pct(self) -> float:
        return (self.current_price - self.avg_price) / self.avg_price if self.avg_price else 0.0
