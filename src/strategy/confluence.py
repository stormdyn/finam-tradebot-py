"""src/strategy/confluence.py — стратегия Confluence (аналог C++ ConfluenceStrategy).

Логика сигнала:
  - MLOFI (multi-level order-flow imbalance) по K_BOOK_LEVELS уровням стакана
  - TFI (trade-flow indicator) скользящее окно из ленты сделок
  - NR7: фильтр узкого диапазона (narrow range 7)
  - Вход только внутри торговых сессий FORTS
  - SL/TP вычисляются в тиках заранее (как в C++)

Класс без сайд-эффектов: on_bar/on_quote/on_order_update только возвращают Signal.
"""
from __future__ import annotations
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from core.domain import (
    Bar, OrderBook, OrderStatus, OrderType, OrderUpdate,
    Quote, Signal, SignalDirection, Symbol, OrderSide,
)
from core.interfaces import IStrategy

logger = logging.getLogger(__name__)

K_BOOK_LEVELS = 5

# Торговые сессии FORTS (MSK = UTC+3)
_SESSION_1 = ((10, 0), (18, 45))
_SESSION_2 = ((19, 0), (23, 50))


@dataclass
class ConfluenceConfig:
    symbol: Symbol
    base_qty: int = 1
    sl_ticks: float = 30.0
    tp_ticks: float = 90.0
    tick_size: float = 1.0
    mlofi_threshold: float = 0.6
    tfi_threshold: float = 0.55
    history_bars: int = 20


class ConfluenceStrategy(IStrategy):
    """Directional confluence strategy для фьючерсов FORTS."""

    def __init__(self, cfg: ConfluenceConfig) -> None:
        self._cfg = cfg
        # MLOFI: предыдущие объёмы по уровням
        self._prev_bid: list[float] = [0.0] * K_BOOK_LEVELS
        self._prev_ask: list[float] = [0.0] * K_BOOK_LEVELS
        self._mlofi_score: float = 0.0
        # TFI: скользящее окно (buy_vol, total_vol)
        self._tfi_window: deque[tuple[float, float]] = deque(maxlen=200)
        # Контекст баров
        self._bars: deque[Bar] = deque(maxlen=cfg.history_bars + 1)
        # Состояние позиции
        self._position: int = 0          # +N long, -N short, 0 flat
        self._active_local_id: Optional[int] = None

    @property
    def name(self) -> str:
        return f"Confluence/{self._cfg.symbol.security_code}"

    @property
    def symbol(self) -> Symbol:
        return self._cfg.symbol

    # ------------------------------------------------------------------ #
    # IStrategy                                                            #
    # ------------------------------------------------------------------ #

    def on_bar(self, bar: Bar) -> Signal:
        self._bars.append(bar)
        null = Signal(symbol=self._cfg.symbol)

        if not self._in_session(bar.ts):
            return null
        if len(self._bars) < self._cfg.history_bars:
            return null  # прогрев
        if self._position != 0:
            return null  # уже в позиции
        if self._is_nr7():
            logger.debug("%s: NR7 filter — skip bar", self.name)
            return null

        direction = self._evaluate_confluence()
        if direction == SignalDirection.NONE:
            return null

        return Signal(
            symbol=self._cfg.symbol,
            direction=direction,
            order_type=OrderType.MARKET,
            quantity=self._cfg.base_qty,
            reason=f"mlofi={self._mlofi_score:.3f} tfi={self._tfi():.3f}",
        )

    def on_quote(self, quote: Quote) -> Signal:
        return Signal(symbol=self._cfg.symbol)

    def on_order_update(self, upd: OrderUpdate) -> None:
        if upd.status in (OrderStatus.FILLED, OrderStatus.PARTIAL_FILL):
            if upd.side == OrderSide.BUY:
                self._position += upd.qty_filled
            else:
                self._position -= upd.qty_filled
        elif upd.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self._active_local_id = None

    # ------------------------------------------------------------------ #
    # Дополнительные хуки (вызываются из StrategyRunner)                  #
    # ------------------------------------------------------------------ #

    def on_order_book(self, book: OrderBook) -> None:
        """Обновить MLOFI из снапшота стакана."""
        delta = 0.0
        for k in range(K_BOOK_LEVELS):
            nb = float(book.bids[k].quantity) if k < len(book.bids) else 0.0
            na = float(book.asks[k].quantity) if k < len(book.asks) else 0.0
            delta += (nb - self._prev_bid[k]) - (na - self._prev_ask[k])
            self._prev_bid[k] = nb
            self._prev_ask[k] = na
        total = sum(self._prev_bid) + sum(self._prev_ask)
        self._mlofi_score = delta / total if total > 1e-9 else 0.0

    def on_trade(self, price: float, volume: float, is_buy: bool) -> None:
        """Добавить тик в TFI-окно."""
        self._tfi_window.append((volume if is_buy else 0.0, volume))

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _tfi(self) -> float:
        if not self._tfi_window:
            return 0.5
        buy = sum(b for b, _ in self._tfi_window)
        total = sum(t for _, t in self._tfi_window)
        return buy / total if total > 1e-9 else 0.5

    def _evaluate_confluence(self) -> SignalDirection:
        mlofi = self._mlofi_score
        tfi = self._tfi()
        cfg = self._cfg
        if mlofi >= cfg.mlofi_threshold and tfi >= cfg.tfi_threshold:
            return SignalDirection.BUY
        if mlofi <= -cfg.mlofi_threshold and tfi <= (1.0 - cfg.tfi_threshold):
            return SignalDirection.SELL
        return SignalDirection.NONE

    def _is_nr7(self) -> bool:
        """True если диапазон последнего бара — минимальный за 7 баров."""
        if len(self._bars) < 7:
            return False
        recent = list(self._bars)[-7:]
        ranges = [b.high - b.low for b in recent]
        return ranges[-1] == min(ranges)

    @staticmethod
    def _in_session(ts: datetime) -> bool:
        """Проверить торговую сессию FORTS (MSK = UTC+3)."""
        msk = ts.astimezone(timezone.utc) + timedelta(hours=3)
        hm = (msk.hour, msk.minute)
        s1 = _SESSION_1[0] <= hm <= _SESSION_1[1]
        s2 = _SESSION_2[0] <= hm <= _SESSION_2[1]
        return s1 or s2
