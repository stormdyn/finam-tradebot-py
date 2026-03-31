"""src/strategy/runner.py — обвязка MarketData + Strategy + OrderExecutor.

Один StrategyRunner на инструмент. Работает как один asyncio.Task.
События из MD-стримов поступают в asyncio.Queue (аналог C++ EventBus/SPSCQueue).
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from core.domain import (
    Bar, OrderBook, OrderRequest, OrderSide,
    OrderUpdate, Signal, SignalDirection, Symbol,
)
from core.errors import AppError
from core.interfaces import IOrderExecutor, IRiskManager, IStrategy
from api.market_data import MarketDataClient

logger = logging.getLogger(__name__)


@dataclass
class RunnerConfig:
    symbol: Symbol
    base_qty: int = 1
    tick_size: float = 1.0
    sl_ticks: float = 30.0
    tp_ticks: float = 90.0
    timeframe: str = "M1"


class StrategyRunner:
    """Per-symbol event loop: MD → Strategy → Executor."""

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
        # asyncio.Queue как event bus (аналог C++ SPSCQueue<MarketEvent>)
        # Trade-off: Queue vs lock-free SPSC:
        #   asyncio однопоточен — data races невозможны, Queue даёт
        #   чистый async API и легко тестируется.
        self._queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=1024)

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
        logger.info("[Runner] starting: %s", sym)

        bar_task = asyncio.create_task(
            self._md.subscribe_bars(
                sym, self._cfg.timeframe,
                callback=lambda b: self._put("bar", b),
                stop_event=stop,
            ),
            name=f"bars-{sym.security_code}",
        )
        book_task = asyncio.create_task(
            self._md.subscribe_orderbook(
                sym,
                callback=lambda ob: self._put("book", ob),
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
            logger.info("[Runner] stopped: %s", sym)

    def _put(self, event_type: str, payload: object) -> None:
        """Положить событие в очередь без блокировки. Дроп при переполнении."""
        try:
            self._queue.put_nowait((event_type, payload))
        except asyncio.QueueFull:
            logger.warning("[Runner] %s: event queue full, dropping %s", self._cfg.symbol, event_type)

    async def _dispatch(self, event_type: str, payload: object) -> None:
        if event_type == "bar":
            assert isinstance(payload, Bar)
            signal = self._strategy.on_bar(payload)
            await self._handle_signal(signal)
        elif event_type == "book":
            assert isinstance(payload, OrderBook)
            if hasattr(self._strategy, "on_order_book"):
                self._strategy.on_order_book(payload)  # type: ignore[attr-defined]
        elif event_type == "order_update":
            assert isinstance(payload, OrderUpdate)
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
            logger.info(
                "[Runner] %s signal %s → order local_id=%d reason=%s",
                self._strategy.name, signal.direction.name, local_id, signal.reason,
            )
        except AppError as e:
            logger.warning("[Runner] %s order rejected: %s", self._strategy.name, e)

    def make_order_callback(self) -> object:
        """Колбэк для подачи OrderUpdate из OrderExecutor в эту стратегию."""
        def _cb(upd: OrderUpdate) -> None:
            self._put("order_update", upd)
        return _cb
