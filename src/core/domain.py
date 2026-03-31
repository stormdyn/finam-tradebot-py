"""src/core/domain.py — базовые типы данных (аналог C++ core/interfaces.hpp structs)"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Literal


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"
    PARTIAL_FILL = "partial_fill"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class SignalDirection(Enum):
    BUY = auto()
    SELL = auto()
    CLOSE = auto()
    NONE = auto()


@dataclass(frozen=True)
class Symbol:
    """
    Формат Finam API v2: {SECURITY_CODE}@{SECURITY_BOARD}
    Примеры: Si-6.26@FORTS, RTS-6.26@FORTS
    """
    security_code: str
    security_board: str = "FORTS"

    def __str__(self) -> str:
        return f"{self.security_code}@{self.security_board}"

    @classmethod
    def from_forts(cls, ticker: str, month: int, year: int) -> Symbol:
        """Si, 6, 2026 → Symbol('Si-6.26', 'FORTS')"""
        return cls(security_code=f"{ticker}-{month}.{year % 100}", security_board="FORTS")


@dataclass
class Quote:
    symbol: Symbol
    bid: float
    ask: float
    last: float
    volume: int
    ts: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Bar:
    symbol: Symbol
    timeframe: str  # "M1", "M5", "H1", "D1" ...
    open: float
    high: float
    low: float
    close: float
    volume: int
    ts: datetime = field(default_factory=datetime.utcnow)
    date: str = ""


@dataclass
class OrderBookRow:
    price: float
    quantity: int


@dataclass
class OrderBook:
    symbol: Symbol
    bids: list[OrderBookRow] = field(default_factory=list)
    asks: list[OrderBookRow] = field(default_factory=list)
    ts: datetime = field(default_factory=datetime.utcnow)


@dataclass
class OrderUpdate:
    order_no: int
    transaction_id: int
    symbol: Symbol
    client_id: str
    side: OrderSide
    status: OrderStatus
    type: OrderType
    price: float
    qty_total: int
    qty_filled: int
    message: str = ""
    ts: datetime = field(default_factory=datetime.utcnow)


@dataclass
class OrderRequest:
    client_id: str
    symbol: Symbol
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    price: float = 0.0
    quantity: int = 0


@dataclass
class Signal:
    """
    Сигнал от стратегии (аналог C++ Signal).
    direction=NONE → нет сигнала.
    """
    symbol: Symbol
    direction: SignalDirection = SignalDirection.NONE
    order_type: OrderType = OrderType.MARKET
    price: float = 0.0
    quantity: int = 0
    reason: str = ""

    def is_entry(self) -> bool:
        return self.direction in (SignalDirection.BUY, SignalDirection.SELL)

    def is_exit(self) -> bool:
        return self.direction == SignalDirection.CLOSE


@dataclass
class BookLevelEvent:
    """Дельта уровня стакана (аналог C++ BookLevelEvent)."""
    level: int
    price: float
    old_bid_size: float
    new_bid_size: float
    old_ask_size: float
    new_ask_size: float
    ts: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TradeEvent:
    """Исполненная сделка из SubscribeLatestTrades."""
    price: float
    volume: float
    is_buy: bool
    ts: datetime = field(default_factory=datetime.utcnow)
