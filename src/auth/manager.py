"""src/auth/manager.py — управление JWT для Finam Trade API v2.

Двухуровневая авторизация:
  1. secret-token (постоянный, из env FINAM_SECRET_TOKEN)
  2. JWT (временный, получается через Auth(secret), авто-обновляется через SubscribeJwtRenewal)

Bезопасность:
  - secret не хранится в атрибутах после запуска renewal-loop
  - JWT хранится как plain str, защищён asyncio.Lock
  - JWT и secret никогда не попадают в логи
"""
from __future__ import annotations
import asyncio
import logging
from typing import Optional

import grpc
import grpc.aio

from core.errors import AppError, ErrorCode
from core.backoff import ExponentialBackoff

logger = logging.getLogger(__name__)


class AuthManager:
    """Получение и авто-обновление JWT для Finam Trade API v2."""

    def __init__(self, endpoint: str = "api.finam.ru:443", use_tls: bool = True) -> None:
        self._endpoint = endpoint
        self._use_tls = use_tls
        self._channel: Optional[grpc.aio.Channel] = None
        self._jwt: Optional[str] = None
        self._account_ids: list[str] = []
        self._jwt_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._renewal_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def init(self, secret: str) -> None:
        """
        Инициализация: создать канал, получить JWT, account_ids, запустить renewal.

        Raises:
            AppError: при ошибке gRPC или если нет account_ids.
        """
        creds = grpc.ssl_channel_credentials() if self._use_tls else grpc.local_channel_credentials()
        self._channel = grpc.aio.secure_channel(self._endpoint, creds)

        # Lazy import stubs — генерируются grpcio-tools из proto/
        # Ожидаемый путь после `make proto`: proto/grpc_trade_api/v1/auth/
        try:
            from proto.grpc_trade_api.v1.auth import authservice_pb2 as pb  # type: ignore
            from proto.grpc_trade_api.v1.auth import authservice_pb2_grpc as pb_grpc  # type: ignore
        except ModuleNotFoundError as exc:
            raise AppError(
                ErrorCode.INTERNAL,
                "Proto stubs not found. Run: make proto",
            ) from exc

        stub = pb_grpc.AuthServiceStub(self._channel)

        # 1. Auth(secret) → JWT
        try:
            resp = await stub.Auth(pb.AuthRequest(secret=secret))
        except grpc.aio.AioRpcError as e:
            raise AppError(ErrorCode.RPC_ERROR, f"Auth failed: {e.details()}") from e

        await self._store_jwt(resp.token)
        logger.info("[Auth] JWT obtained")

        # 2. TokenDetails(jwt) → account_ids
        try:
            details = await stub.TokenDetails(
                pb.TokenDetailsRequest(token=resp.token),
                metadata=self._metadata(),
            )
        except grpc.aio.AioRpcError as e:
            raise AppError(ErrorCode.RPC_ERROR, f"TokenDetails failed: {e.details()}") from e

        self._account_ids = list(details.account_ids)
        if not self._account_ids:
            raise AppError(ErrorCode.INVALID_ARGUMENT, "no account_ids in token")
        logger.info("[Auth] accounts: %d", len(self._account_ids))

        # 3. Запустить renewal-стрим в фоне
        self._stop_event.clear()
        self._renewal_task = asyncio.create_task(
            self._run_renewal(secret, stub, pb),
            name="jwt-renewal",
        )
        # Не храним secret после передачи в задачу

    @property
    def jwt(self) -> str:
        if self._jwt is None:
            raise RuntimeError("AuthManager.init() not called")
        return self._jwt

    @property
    def channel(self) -> grpc.aio.Channel:
        if self._channel is None:
            raise RuntimeError("AuthManager.init() not called")
        return self._channel

    @property
    def primary_account_id(self) -> str:
        if not self._account_ids:
            raise RuntimeError("AuthManager.init() not called")
        return self._account_ids[0]

    @property
    def account_ids(self) -> list[str]:
        return self._account_ids

    def metadata(self) -> list[tuple[str, str]]:
        """gRPC metadata с текущим JWT."""
        return self._metadata()

    async def shutdown(self) -> None:
        """Graceful stop: отмена renewal, закрытие канала."""
        self._stop_event.set()
        if self._renewal_task and not self._renewal_task.done():
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except asyncio.CancelledError:
                pass
        if self._channel:
            await self._channel.close()
        logger.info("[Auth] shutdown complete")

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _metadata(self) -> list[tuple[str, str]]:
        return [("authorization", f"Bearer {self._jwt}")] if self._jwt else []

    async def _store_jwt(self, token: str) -> None:
        async with self._jwt_lock:
            self._jwt = token

    async def _run_renewal(self, secret: str, stub, pb) -> None:
        """Фоновая задача: SubscribeJwtRenewal с reconnection."""
        backoff = ExponentialBackoff(base_ms=500, max_ms=30_000)

        while not self._stop_event.is_set():
            try:
                req = pb.SubscribeJwtRenewalRequest(secret=secret)
                async for resp in stub.SubscribeJwtRenewal(req, metadata=self._metadata()):
                    if resp.token:
                        await self._store_jwt(resp.token)
                        backoff.reset()
                        logger.debug("[Auth] JWT renewed")
            except grpc.aio.AioRpcError as e:
                if e.code() in (grpc.StatusCode.CANCELLED, grpc.StatusCode.OK):
                    break
                logger.warning("[Auth] renewal stream error: %s — reconnecting", e.code())
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[Auth] renewal unexpected error")

            if not self._stop_event.is_set():
                should_continue = await backoff.wait(self._stop_event)
                if not should_continue:
                    break

        logger.info("[Auth] renewal stopped")
