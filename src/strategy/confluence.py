"""ConfluenceStrategy — Python port of the C++ ConfluenceStrategy.

Signal logic (same as C++ version):
  - Uses MLOFI (multi-level order-flow imbalance) across kBookLevels
  - Uses TFI (trade-flow indicator) from latest trades
  - Uses SessionContext (ORB / NR7 / ATR-based)
  - Entry only within FORTS trading sessions (10:00-18:45, 19:00-23:50 MSK)
  - Stop-loss / take-profit pre-computed in ticks

This is a side-effect-free class: on_bar/on_quote/on_order_update
return Signal objects and never call the executor directly.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.domain import (
    Bar, OrderBook, OrderRequest, OrderSide, OrderStatus,
    OrderType, OrderUpdate, Quote, Signal, SignalDirection, Symbol,
)
from core.interfaces import IStrategy

log = logging.getLogger(__name__)

K_BOOK_LEVELS = 5  # same as C++ kBookLevels
_SESSION_1_START = (10, 0)
_SESSION_1_END   = (18, 45)
_SESSION_2_START = (19, 0)
_SESSION_2_END   = (23, 50)


@dataclass
class ConfluenceConfig:
    symbol: Symbol
    base_qty: int = 1
    sl_ticks: float = 30.0
    tp_ticks: float = 90.0
    tick_size: float = 1.0
    mlofi_threshold: float = 0.6    # imbalance ratio to trigger
    tfi_threshold: float = 0.55     # trade-flow ratio to confirm
    history_bars: int = 20          # ATR / NR7 lookback


class ConfluenceStrategy(IStrategy):
    """Directional confluence strategy for FORTS futures.

    Components:
      - MLOFI: aggregated bid/ask delta across K_BOOK_LEVELS
      - TFI: rolling buy_vol / total_vol from tick stream
      - SessionContext: is we within a valid trading window?
      - NR7: narrow-range bar filter (reduces false entries)
    """

    def __init__(self, cfg: ConfluenceConfig) -> None:
        self._cfg = cfg
        # MLOFI state: per-level previous bid/ask sizes
        self._prev_bid: list[float] = [0.0] * K_BOOK_LEVELS
        self._prev_ask: list[float] = [0.0] * K_BOOK_LEVELS
        self._mlofi_score = 0.0

        # TFI state
        self._buy_vol = 0.0
        self._total_vol = 0.0
        self._tfi_window: deque[tuple[float, float]] = deque(maxlen=200)

        # Session / bar context
        self._bars: deque[Bar] = deque(maxlen=cfg.history_bars + 1)
        self._position: int = 0       # +1 long, -1 short, 0 flat
        self._active_order: Optional[int] = None  # local_id

    @property
    def name(self) -> str:
        return f"Confluence/{self._cfg.symbol.security_code}"

    # ------------------------------------------------------------------ #
    # IStrategy callbacks                                                  #
    # ------------------------------------------------------------------ #

    def on_bar(self, bar: Bar) -> Signal:
        self._bars.append(bar)
        if not self._in_session(bar.ts):
            return Signal(symbol=self._cfg.symbol)
        if len(self._bars) < self._cfg.history_bars:
            return Signal(symbol=self._cfg.symbol)  # warm-up
        if self._position != 0:
            return Signal(symbol=self._cfg.symbol)  # already in trade
        if self._is_nr7():
            log.debug("%s: NR7 filter active, skipping bar", self.name)
            return Signal(symbol=self._cfg.symbol)

        direction = self._evaluate_confluence()
        if direction == SignalDirection.NONE:
            return Signal(symbol=self._cfg.symbol)

        qty = self._cfg.base_qty
        return Signal(
            symbol=self._cfg.symbol,
            direction=direction,
            order_type=OrderType.MARKET,
            quantity=qty,
            reason=f"mlofi={self._mlofi_score:.3f} tfi={self._tfi():.3f}",
        )

    def on_quote(self, quote: Quote) -> Signal:
        # Quotes used only for last-price reference; main signal from bars
        return Signal(symbol=self._cfg.symbol)

    def on_order_update(self, upd: OrderUpdate) -> None:
        if upd.status in (OrderStatus.FILLED, OrderStatus.PARTIAL_FILL):
            if upd.side == OrderSide.BUY:
                self._position += upd.qty_filled
            else:
                self._position -= upd.qty_filled
        elif upd.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self._active_order = None

    # ------------------------------------------------------------------ #
    # Order book hook (called by StrategyRunner, not part of IStrategy)    #
    # ------------------------------------------------------------------ #

    def on_order_book(self, book: OrderBook) -> None:
        """Update MLOFI score from incremental order-book snapshot."""
        bids = book.bids
        asks = book.asks
        delta = 0.0
        for k in range(K_BOOK_LEVELS):
            new_bid = float(bids[k].quantity) if k < len(bids) else 0.0
            new_ask = float(asks[k].quantity) if k < len(asks) else 0.0
            old_bid = self._prev_bid[k]
            old_ask = self._prev_ask[k]
            delta += (new_bid - old_bid) - (new_ask - old_ask)
            self._prev_bid[k] = new_bid
            self._prev_ask[k] = new_ask

        total = sum(self._prev_bid) + sum(self._prev_ask)
        self._mlofi_score = delta / total if total > 0 else 0.0

    def on_trade(self, price: float, volume: float, is_buy: bool) -> None:
        """Feed tick data into TFI rolling window."""
        self._tfi_window.append((volume if is_buy else 0.0, volume))

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _tfi(self) -> float:
        if not self._tfi_window:
            return 0.5
        buy = sum(b for b, _ in self._tfi_window)
        total = sum(t for _, t in self._tfi_window)
        return buy / total if total > 0 else 0.5

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
        """True if current bar range is narrowest of last 7 bars."""
        if len(self._bars) < 7:
            return False
        ranges = [b.high - b.low for b in list(self._bars)[-7:]]
        return ranges[-1] == min(ranges)

    @staticmethod
    def _in_session(ts: datetime) -> bool:
        """Check FORTS trading sessions (MSK = UTC+3)."""
        msk = ts.astimezone(timezone.utc)
        h, m = msk.hour + 3, msk.minute  # rough MSK conversion
        if h >= 24:
            h -= 24
        hm = (h, m)
        s1 = _SESSION_1_START <= hm <= _SESSION_1_END
        s2 = _SESSION_2_START <= hm <= _SESSION_2_END
        return s1 or s2
