"""src/core/interfaces.py — Protocol-классы (аналог C++ IStrategy, IOrderExecutor)"""
from __future__ import annotations
from typing import Protocol, runtime_checkable
from core.domain import Bar, Quote, OrderUpdate, Signal, OrderRequest
from .errors import Result
from core.domain import (
    Bar, Quote, OrderUpdate, Signal, 
    OrderRequest, OrderBook, Symbol
)


@runtime_checkable
class IStrategy(Protocol):
    """
    Интерфейс стратегии (аналог C++ IStrategy).
    Без сайд-эффектов, кроме генерации Signal.
    """
    
    def on_bar(self, bar: Bar) -> Signal:
        """Обработка бара (D1 для bias, M1 для входа)"""
        ...
    
    def on_quote(self, quote: Quote) -> Signal:
        """Обработка котировки — проверка SL/TP, вход по OFI"""
        ...
    
    def on_order_update(self, upd: OrderUpdate) -> None:
        """Уведомление об изменении ордера — обновляем позицию"""
        ...
    
    @property
    def symbol(self) -> Symbol:
        """Инструмент, за который отвечает стратегия"""
        ...


@runtime_checkable
class IRiskManager(Protocol):
    """
    Проверка лимитов перед отправкой ордера (аналог C++ IRiskManager).
    """
    
    async def check(self, req: OrderRequest) -> Result[None]:
        """Возвращает AppError если ордер нарушает лимиты"""
        ...
    
    def on_fill(self, upd: OrderUpdate) -> None:
        """Обновление PnL после исполнения — для daily loss / drawdown"""
        ...
    
    def is_tripped(self) -> bool:
        """Circuit breaker активен — остановить все новые входы"""
        ...


@runtime_checkable
class IOrderExecutor(Protocol):
    """
    Абстракция над отправкой ордеров (аналог C++ IOrderExecutor).
    Позволяет dry-run моки для тестов.
    """
    
    async def submit(self, req: OrderRequest) -> Result[int]:
        """Отправляет ордер, возвращает биржевой order_no или AppError"""
        ...
    
    async def cancel(self, order_no: int, client_id: str) -> Result[None]:
        """Отменяет ордер по биржевому ID"""
        ...