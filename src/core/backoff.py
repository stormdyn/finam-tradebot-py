"""src/core/backoff.py — Экспоненциальная задержка с jitter (аналог core/backoff.hpp)"""
from __future__ import annotations
import asyncio
import random
from dataclasses import dataclass


@dataclass
class BackoffConfig:
    """Конфигурация backoff"""
    base_ms: int = 500       # начальная задержка
    max_ms: int = 30_000     # максимальная задержка (30с)
    jitter_pct: float = 0.25 # ±25% джиттер


class ExponentialBackoff:
    """
    Экспоненциальный backoff с jitter.
    Использование:
        bo = ExponentialBackoff()
        while not stop:
            connect_and_run()
            await bo.wait(stop_event)  # ждём перед реконнектом
        bo.reset()  # успешное соединение — сбросить задержку
    """
    
    def __init__(self, cfg: BackoffConfig = BackoffConfig()) -> None:
        self._cfg = cfg
        self._attempt = 0
    
    async def wait(self, stop_event: asyncio.Event) -> bool:
        """
        Ждём с экспоненциальной задержкой, прерывается при stop_event.
        Возвращает: True — надо реконнектиться, False — остановка.
        """
        delay = self._compute_delay()
        
        # Проверяем stop_event каждые 100ms для быстрого прерывания
        elapsed_ms = 0
        while elapsed_ms < delay:
            if stop_event.is_set():
                return False
            await asyncio.sleep(0.1)
            elapsed_ms += 100
        
        self._attempt += 1
        return True
    
    def _compute_delay(self) -> int:
        """Вычисляет задержку для текущего attempt без изменения состояния"""
        # base * 2^attempt, зажато до max
        raw = self._cfg.base_ms * (2 ** min(self._attempt, 6))  # max 64x
        capped = min(raw, self._cfg.max_ms)
        
        # Джиттер: равномерно в [capped*(1-jitter), capped*(1+jitter)]
        jitter = capped * self._cfg.jitter_pct * (random.random() * 2 - 1)
        return max(0, int(capped + jitter))
    
    def reset(self) -> None:
        """Сбросить счётчик попыток"""
        self._attempt = 0
    
    @property
    def attempt(self) -> int:
        """Текущий номер попытки"""
        return self._attempt