"""StrategyRunner — wires together MarketDataClient, IStrategy, IOrderExecutor.

Per-symbol event loop:
  1. Subscribe to bars + order book
  2. On bar: call strategy.on_bar(), if Signal → executor.submit()
  3. On order update: call strategy.on_order_update()
  4. Handles rollover: watch expiry, reconnect to next contract

Design note: StrategyRunner runs as a single asyncio.Task per symbol.
It does NOT spawn additional threads; all callbacks are dispatched via
asyncio.Queue to stay on the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from core.domain import Bar, OrderBook, OrderRequest, OrderSide, OrderUpdate, Signal, SignalDirection, Symbol
from core.errors import FinamError
from core.interfaces import IOrderExecutor, IRiskManager, IStrategy
from api.market_data import MarketDataClient

log = logging.getLogger(__name__)


@dataclass
class RunnerConfig:
    symbol: Symbol
    base_qty: int = 1
    tick_size: float = 1.0
    sl_ticks: float = 30.0
    tp_ticks: float = 90.0
    timeframe: str = "M1"
    poll_interval_ms: int = 100


class StrategyRunner:
    """Per-symbol runner. Instantiate one per instrument."""

    def __init__(
        self,
        cfg: RunnerConfig,
        md: MarketDataClient,
        executor: IOrderExecutor,
        risk: IRiskManager,
        strategy: Optional[IStrategy] = None,
    ) -> None:
        self._cfg = cfg
        self._md = md
        self._executor = executor
        self._risk = risk
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1024)

        # Default strategy: ConfluenceStrategy
        if strategy is None:
            from strategy.confluence import ConfluenceStrategy, ConfluenceConfig  # noqa: PLC0415
            strategy = ConfluenceStrategy(ConfluenceConfig(
                symbol=cfg.symbol,
                base_qty=cfg.base_qty,
                sl_ticks=cfg.sl_ticks,
                tp_ticks=cfg.tp_ticks,
                tick_size=cfg.tick_size,
            ))
        self._strategy = strategy

    async def run(self, stop: asyncio.Event) -> None:
        sym = self._cfg.symbol
        log.info("StrategyRunner starting: %s", sym)

        # Bar subscription feeds events into the queue
        bar_task = asyncio.create_task(
            self._md.subscribe_bars(
                sym, self._cfg.timeframe,
                callback=lambda b: self._queue.put_nowait(("bar", b)),
                stop_event=stop,
            ),
            name=f"bars-{sym.security_code}",
        )

        book_task = asyncio.create_task(
            self._md.subscribe_orderbook(
                sym,
                callback=lambda ob: self._queue.put_nowait(("book", ob)),
                stop_event=stop,
            ),
            name=f"book-{sym.security_code}",
        )

        try:
            while not stop.is_set():
                try:
                    event_type, payload = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                await self._dispatch(event_type, payload)
        except asyncio.CancelledError:
            pass
        finally:
            bar_task.cancel()
            book_task.cancel()
            await asyncio.gather(bar_task, book_task, return_exceptions=True)
            log.info("StrategyRunner stopped: %s", sym)

    async def _dispatch(self, event_type: str, payload) -> None:
        if event_type == "bar":
            signal = self._strategy.on_bar(payload)
            await self._handle_signal(signal)
        elif event_type == "book":
            if hasattr(self._strategy, "on_order_book"):
                self._strategy.on_order_book(payload)
        elif event_type == "order_update":
            self._strategy.on_order_update(payload)

    async def _handle_signal(self, signal: Signal) -> None:
        if signal.direction == SignalDirection.NONE:
            return
        if signal.quantity <= 0:
            return

        side = OrderSide.BUY if signal.direction == SignalDirection.BUY else OrderSide.SELL
        req = OrderRequest(
            client_id=str(self._cfg.symbol),
            symbol=self._cfg.symbol,
            side=side,
            order_type=signal.order_type,
            price=signal.price,
            quantity=signal.quantity,
        )
        try:
            local_id = await self._executor.submit(req)
            log.info(
                "[%s] Signal %s → order local_id=%d reason=%s",
                self._strategy.name, signal.direction, local_id, signal.reason,
            )
        except FinamError as e:
            log.warning("[%s] Order rejected: %s", self._strategy.name, e)

    def make_order_callback(self) -> object:
        """Returns a callback to feed OrderUpdates back into this runner."""
        def _cb(upd: OrderUpdate) -> None:
            self._queue.put_nowait(("order_update", upd))
        return _cb
