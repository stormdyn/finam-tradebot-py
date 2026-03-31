"""src/core/types.py — Базовые типы данных (аналог core/interfaces.hpp)"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Literal
from decimal import Decimal


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


@dataclass(frozen=True)
class Symbol:
    """
    Формат Finam API v2: {SECURITY_CODE}@{SECURITY_BOARD}
    Примеры из C++ connectivity_check: SiM6@RTSX, RIM6@RTSX
    """
    security_code: str   # "SiM6", "Si-6.26" — зависит от API
    security_board: str  # "RTSX" или "FORTS"
    
    def __str__(self) -> str:
        return f"{self.security_code}@{self.security_board}"
    
    @classmethod
    def from_forts(cls, ticker: str, month: int, year: int) -> Symbol:
        """Si, 6, 2026 → Symbol('Si-6.26', 'FORTS')"""
        return cls(security_code=f"{ticker}-{month}.{year % 100}", security_board="FORTS")


@dataclass
class Quote:
    """Котировка (bid/ask/last)"""
    symbol: Symbol
    bid: float
    ask: float
    last: float
    volume: int
    ts: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Bar:
    """Свеча с таймфреймом (как в C++ Bar::timeframe)"""
    symbol: Symbol
    timeframe: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]
    open: float
    high: float
    low: float
    close: float
    volume: int
    ts: datetime = field(default_factory=datetime.utcnow)
    date: str = ""  # YYYY-MM-DD для бэктеста


@dataclass
class OrderBookRow:
    """Уровень стакана"""
    price: float
    quantity: int


@dataclass
class OrderBook:
    """Снапшот стакана"""
    symbol: Symbol
    bids: list[OrderBookRow] = field(default_factory=list)
    asks: list[OrderBookRow] = field(default_factory=list)
    ts: datetime = field(default_factory=datetime.utcnow)

@dataclass
class OrderUpdate:
    """Уведомление об изменении ордера (из SubscribeOrders)"""
    order_no: int          # биржевой ID (64-bit в C++)
    transaction_id: int    # локальный ID для трекинга
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
    """Запрос на выставление ордера"""
    client_id: str
    symbol: Symbol
    side: OrderSide
    type: OrderType = OrderType.MARKET
    price: float = 0.0
    quantity: int = 0


@dataclass
class Signal:
    """
    Сигнал от стратегии (аналог C++ Signal).
    direction=None → нет сигнала.
    """
    class Direction(Enum):
        BUY = auto()
        SELL = auto()
        CLOSE = auto()
        NONE = auto()
    
    symbol: Symbol
    direction: Direction = Direction.NONE
    order_type: OrderType = OrderType.MARKET
    price: float = 0.0
    quantity: int = 0
    reason: str = ""
    
    def is_entry(self) -> bool:
        return self.direction in (self.Direction.BUY, self.Direction.SELL)
    
    def is_exit(self) -> bool:
        return self.direction == self.Direction.CLOSE


# ── События для стратегии (OFI pipeline) ──────────────────────────────────────

@dataclass
class BookLevelEvent:
    """
    Дельта уровня стакана (аналог C++ BookLevelEvent).
    Вызывается на каждое изменение уровня 0..4.
    """
    level: int           # 0-based, 0 = лучший уровень
    price: float
    old_bid_size: float
    new_bid_size: float
    old_ask_size: float
    new_ask_size: float
    ts: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TradeEvent:
    """Исполненная сделка (из SubscribeLatestTrades)"""
    price: float
    volume: float
    is_buy: bool         # true = buyer-initiated (hit ask)
    ts: datetime = field(default_factory=datetime.utcnow)