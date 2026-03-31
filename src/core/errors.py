"""src/core/errors.py — Классификация ошибок (аналог core/interfaces.hpp ErrorCode)"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, TypeVar, Union


class ErrorCode(Enum):
    # Network / gRPC
    CONNECTION_FAILED = auto()
    STREAM_DISCONNECTED = auto()
    TIMEOUT = auto()
    RPC_ERROR = auto()
    
    # Order
    ORDER_REJECTED = auto()
    INSUFFICIENT_MARGIN = auto()
    INVALID_PRICE = auto()
    INVALID_QUANTITY = auto()
    
    # Risk
    RISK_LIMIT_EXCEEDED = auto()
    DAILY_LOSS_LIMIT_HIT = auto()
    
    # Generic
    INVALID_ARGUMENT = auto()
    NOT_IMPLEMENTED = auto()
    INTERNAL = auto()


@dataclass
class AppError:
    """Структурированная ошибка (аналог C++ Error)"""
    code: ErrorCode
    message: str
    details: Optional[dict[str, object]] = None
    
    def __str__(self) -> str:
        base = f"{self.code.name}: {self.message}"
        if self.details:
            base += f" | {self.details}"
        return base


# ── Result-тип для явной обработки ошибок (аналог std::expected) ─────────────

T = TypeVar("T")
Result = Union[T, AppError]


def is_ok(result: Result) -> bool:
    """Проверка: результат успешен (не AppError)"""
    return not isinstance(result, AppError)

def unwrap(result: Result[T]) -> T:  # Result уже имеет TypeVar T
    """Получить значение или выбросить исключение."""
    if isinstance(result, AppError):
        raise RuntimeError(f"Unwrap failed: {result}")
    return result