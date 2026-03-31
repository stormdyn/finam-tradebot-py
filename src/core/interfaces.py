"""src/core/interfaces.py — ABC-интерфейсы (аналог C++ IStrategy, IOrderExecutor, IRiskManager)"""
from __future__ import annotations
from abc import ABC, abstractmethod
from core.domain import Bar, Quote, OrderUpdate, Signal, OrderRequest, OrderBook, Symbol


class IStrategy(ABC):
    """
    Интерфейс стратегии — без сайд-эффектов, только генерирует Signal.
    Аналог C++ IStrategy.
    """

    @abstractmethod
    def on_bar(self, bar: Bar) -> Signal:
        """Обработка завершённой свечи."""
        ...

    @abstractmethod
    def on_quote(self, quote: Quote) -> Signal:
        """Обработка котировки (bid/ask/last)."""
        ...

    @abstractmethod
    def on_order_update(self, upd: OrderUpdate) -> None:
        """Уведомление об изменении ордера."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Имя стратегии для логов."""
        ...

    @property
    def symbol(self) -> Symbol:
        """Инструмент стратегии (опционально переопределить)."""
        raise NotImplementedError


class IRiskManager(ABC):
    """
    Проверка лимитов перед отправкой ордера.
    Аналог C++ IRiskManager.
    
    Raises AppError при нарушении лимита.
    """

    @abstractmethod
    async def check(self, req: OrderRequest) -> None:
        """Выбрасывает AppError если ордер нарушает лимиты."""
        ...

    @abstractmethod
    def on_fill(self, upd: OrderUpdate) -> None:
        """Обновление PnL после исполнения."""
        ...

    @abstractmethod
    def is_tripped(self) -> bool:
        """Circuit breaker активен — блокировать все входы."""
        ...


class IOrderExecutor(ABC):
    """
    Абстракция над отправкой ордеров.
    Аналог C++ IOrderExecutor.
    
    Raises AppError при ошибке.
    """

    @abstractmethod
    async def submit(self, req: OrderRequest) -> int:
        """Отправить ордер. Возвращает локальный order_id."""
        ...

    @abstractmethod
    async def cancel(self, order_no: int, client_id: str) -> None:
        """Отменить ордер по локальному ID."""
        ...
