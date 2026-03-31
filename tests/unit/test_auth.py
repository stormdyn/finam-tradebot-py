"""tests/unit/test_auth.py — unit-тесты AuthManager с mock gRPC сервером."""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from auth.manager import AuthManager
from core.errors import AppError, ErrorCode


@pytest.mark.asyncio
async def test_auth_raises_on_missing_stubs():
    """
    Без сгенерированных proto stubs init() должен бросить AppError(INTERNAL).
    Это обеспечивает чёткую диагностику: 'Run: make proto'.
    """
    mgr = AuthManager(endpoint="localhost:1234", use_tls=False)
    # Мокаем grpc.aio.secure_channel чтобы не делать реальный коннект
    with patch("grpc.aio.secure_channel"), \
         patch("grpc.local_channel_credentials"):
        with pytest.raises(AppError) as exc_info:
            await mgr.init("any_secret")
    assert exc_info.value.code == ErrorCode.INTERNAL
    assert "Proto stubs not found" in exc_info.value.message


@pytest.mark.asyncio
async def test_jwt_property_raises_before_init():
    mgr = AuthManager()
    with pytest.raises(RuntimeError, match="init"):
        _ = mgr.jwt


@pytest.mark.asyncio
async def test_primary_account_raises_before_init():
    mgr = AuthManager()
    with pytest.raises(RuntimeError, match="init"):
        _ = mgr.primary_account_id


@pytest.mark.asyncio
async def test_shutdown_idempotent():
    """shutdown() без init() не должен падать."""
    mgr = AuthManager()
    await mgr.shutdown()  # не должно выбрасывать
