"""src/core/errors.py — классификация ошибок (аналог C++ ErrorCode + Error)"""
from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


class ErrorCode(IntEnum):
    # Network / gRPC
    CONNECTION_FAILED = 100
    STREAM_DISCONNECTED = 101
    TIMEOUT = 102
    RPC_ERROR = 103
    # Order
    ORDER_REJECTED = 200
    INSUFFICIENT_MARGIN = 201
    INVALID_PRICE = 202
    INVALID_QUANTITY = 203
    # Risk
    RISK_LIMIT_EXCEEDED = 300
    DAILY_LOSS_LIMIT_HIT = 301
    # Generic
    INVALID_ARGUMENT = 400
    NOT_IMPLEMENTED = 401
    INTERNAL = 500


class AppError(Exception):
    """Структурированная ошибка. Используется через raise, не как Result-тип."""

    def __init__(self, code: ErrorCode, message: str, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        base = f"{self.code.name}: {self.message}"
        if self.details:
            base += f" | {self.details}"
        return base
