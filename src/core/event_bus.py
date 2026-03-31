"""src/core/event_bus.py — Шина событий на asyncio.Queue (аналог C++ SPSCQueue)"""
from __future__ import annotations
import asyncio
from typing import TypeVar, Generic, Optional
from dataclasses import dataclass
from core.domain import Quote, Bar, OrderUpdate, OrderBook

T = TypeVar("T")


@dataclass
class MarketEvent:
    """Обёртка для событий рынка (аналог C++ MarketEvent variant)"""
    quote: Optional[Quote] = None
    bar: Optional[Bar] = None
    order_book: Optional[OrderBook] = None
    order_update: Optional[OrderUpdate] = None
    
    @classmethod
    def from_quote(cls, q: Quote) -> MarketEvent:
        return cls(quote=q)
    
    @classmethod
    def from_bar(cls, b: Bar) -> MarketEvent:
        return cls(bar=b)
    
    @classmethod
    def from_order_update(cls, u: OrderUpdate) -> MarketEvent:
        return cls(order_update=u)


class EventBus(Generic[T]):
    """
    Асинхронная шина событий на asyncio.Queue.
    Замена C++ SPSCQueue<MarketEvent>.
    
    Trade-off: asyncio.Queue вместо lock-free очереди.
    В asyncio нет data races внутри event loop, поэтому lock-free избыточен.
    Queue даёт чистый async API и проще тестировать.
    """
    
    def __init__(self, maxsize: int = 1024) -> None:
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=maxsize)
        self._dropped = 0
    
    async def publish(self, event: T) -> bool:
        """
        Опубликовать событие.
        Возвращает False если очередь полна (event dropped).
        """
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            return False
    
    async def subscribe(self) -> asyncio.Queue[T]:
        """
        Создать новую подписку (отдельная очередь).
        Для простоты возвращаем ту же очередь — в production
        можно реализовать fan-out через asyncio.Queue per subscriber.
        """
        return self._queue
    
    async def get(self) -> T:
        """Получить следующее событие (блокирует)"""
        return await self._queue.get()
    
    def get_nowait(self) -> T | None:
        """Получить событие без блокировки"""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
    
    @property
    def dropped_count(self) -> int:
        """Сколько событий потеряно из-за полной очереди"""
        return self._dropped
    
    def qsize(self) -> int:
        """Текущий размер очереди"""
        return self._queue.qsize()