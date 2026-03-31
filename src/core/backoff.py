"""src/core/backoff.py — экспоненциальный backoff с jitter (аналог C++ core/backoff.hpp)"""
from __future__ import annotations
import asyncio
import random
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class BackoffState:
    attempt: int
    delay: float  # seconds


async def exponential_backoff(
    base: float = 0.5,
    cap: float = 30.0,
    jitter: float = 0.25,
) -> AsyncIterator[BackoffState]:
    """
    Async-генератор для reconnection-циклов.

    Использование::

        async for attempt in exponential_backoff():
            try:
                await connect_and_stream()
                return  # успех
            except SomeError:
                pass  # генератор сам поспит и продолжит

    Trade-off vs tenacity:
        tenacity удобен для unary-вызовов (@retry декоратор),
        но для streaming loops нужен явный async for с контролем
        CancelledError. Свой генератор прозрачнее и легче тестировать.
    """
    attempt = 0
    while True:
        raw = base * (2 ** min(attempt, 6))   # max 64x base
        capped = min(raw, cap)
        delay = capped * random.uniform(1.0 - jitter, 1.0 + jitter)
        yield BackoffState(attempt=attempt, delay=delay)
        await asyncio.sleep(delay)
        attempt += 1


# Совместимый класс для мест, где нужен объект (auth/manager.py)
class ExponentialBackoff:
    """
    Объектный вариант backoff — используется внутри _run_renewal,
    где нужен reset() при успешном обновлении токена.
    """

    def __init__(self, base_ms: int = 500, max_ms: int = 30_000, jitter_pct: float = 0.25) -> None:
        self._base = base_ms / 1000.0
        self._cap = max_ms / 1000.0
        self._jitter = jitter_pct
        self._attempt = 0

    async def wait(self, stop_event: asyncio.Event) -> bool:
        """
        Ждёт с задержкой, прерывается по stop_event.
        Возвращает True — продолжать, False — остановиться.
        """
        raw = self._base * (2 ** min(self._attempt, 6))
        capped = min(raw, self._cap)
        delay = capped * random.uniform(1.0 - self._jitter, 1.0 + self._jitter)
        self._attempt += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return False  # stop_event сработал
        except asyncio.TimeoutError:
            return True   # таймаут — реконнект

    def reset(self) -> None:
        self._attempt = 0

    @property
    def attempt(self) -> int:
        return self._attempt
