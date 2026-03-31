import asyncio
import logging
from typing import Optional
import grpc
from grpc.aio import Channel, UnaryStreamCall
from .grpc_stubs.auth_service_pb2 import (
    AuthRequest, AuthResponse,
    SubscribeJwtRenewalRequest, SubscribeJwtRenewalResponse,
    TokenDetailsRequest, TokenDetailsResponse
)
from .grpc_stubs.auth_service_pb2_grpc import AuthServiceStub
from ..core.errors import AppError, ErrorCode, Result

logger = logging.getLogger(__name__)

class AuthManager:
    """Управление JWT: получение, авто-обновление, безопасный доступ"""
    
    def __init__(self, endpoint: str = "api.finam.ru:443", use_tls: bool = True):
        self._endpoint = endpoint
        self._use_tls = use_tls
        self._channel: Optional[Channel] = None
        self._stub: Optional[AuthServiceStub] = None
        self._jwt: Optional[str] = None
        self._account_ids: list[str] = []
        self._renewal_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
    
    async def init(self, secret: str) -> Result[None]:
        """Инициализация: канал, первичный JWT, account_ids, запуск renewal"""
        try:
            credentials = grpc.ssl_channel_credentials() if self._use_tls else grpc.local_channel_credentials()
            self._channel = grpc.aio.secure_channel(self._endpoint, credentials)
            self._stub = AuthServiceStub(self._channel)
            
            # 1. Auth(secret) → JWT
            resp: AuthResponse = await self._stub.Auth(AuthRequest(secret=secret))
            self._jwt = resp.token
            logger.info("[Auth] JWT obtained")
            
            # 2. TokenDetails → account_ids
            details: TokenDetailsResponse = await self._stub.TokenDetails(
                TokenDetailsRequest(token=self._jwt),
                metadata=[("authorization", f"Bearer {self._jwt}")]
            )
            self._account_ids = list(details.account_ids)
            if not self._account_ids:
                return AppError(ErrorCode.INVALID_ARGUMENT, "no account_ids in token")
            logger.info(f"[Auth] accounts: {len(self._account_ids)}")
            
            # 3. Запуск renewal-стрима в фоне
            self._stop_event.clear()
            self._renewal_task = asyncio.create_task(self._run_renewal(secret))
            return None
        except grpc.RpcError as e:
            return AppError(ErrorCode.RPC_ERROR, str(e.details()))
        except Exception as e:
            return AppError(ErrorCode.INTERNAL, f"init failed: {e}")
    
    @property
    def jwt(self) -> Optional[str]:
        """Текущий JWT — читать только внутри event loop"""
        return self._jwt
    
    @property
    def primary_account_id(self) -> str:
        if not self._account_ids:
            raise RuntimeError("AuthManager not initialized")
        return self._account_ids[0]
    
    def _make_metadata(self) -> list[tuple[str, str]]:
        return [("authorization", f"Bearer {self._jwt}")] if self._jwt else []
    
    async def _run_renewal(self, secret: str) -> None:
        """Фоновая задача: SubscribeJwtRenewal с reconnection"""
        from ..core.backoff import ExponentialBackoff
        backoff = ExponentialBackoff(base_ms=500, max_ms=30_000)
        
        while not self._stop_event.is_set():
            try:
                call: UnaryStreamCall[SubscribeJwtRenewalRequest, SubscribeJwtRenewalResponse] = (
                    self._stub.SubscribeJwtRenewal(
                        SubscribeJwtRenewalRequest(secret=secret),
                        metadata=self._make_metadata()
                    )
                )
                async for resp in call:
                    if resp.token:
                        self._jwt = resp.token
                        logger.debug("[Auth] JWT renewed")
                        backoff.reset()
            except grpc.RpcError as e:
                code = e.code()
                if code in (grpc.StatusCode.CANCELLED, grpc.StatusCode.OK):
                    break  # нормальное завершение
                logger.warning(f"[Auth] renewal error: {code} — reconnecting")
            except Exception as e:
                logger.exception("[Auth] renewal unexpected error")
            
            if not self._stop_event.is_set():
                await backoff.wait(self._stop_event)
        
        # Безопасное обнуление секрета (в Python менее критично, но для паранойи)
        secret = "\0" * len(secret)
        logger.info("[Auth] renewal stopped")
    
    async def shutdown(self) -> None:
        """Graceful stop: отмена renewal, закрытие канала"""
        self._stop_event.set()
        if self._renewal_task:
            await self._renewal_task
        if self._channel:
            await self._channel.close()
        logger.info("[Auth] shutdown complete")