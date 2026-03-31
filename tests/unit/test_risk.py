"""tests/unit/test_risk.py — unit-тесты RiskManager без gRPC."""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from core.domain import OrderRequest, OrderSide, OrderType, Symbol, OrderUpdate, OrderStatus
from core.errors import AppError, ErrorCode
from risk.manager import RiskConfig, RiskManager, AccountState


def _make_req(qty: int = 1, side: OrderSide = OrderSide.BUY) -> OrderRequest:
    return OrderRequest(
        client_id="test",
        symbol=Symbol(security_code="Si-6.26"),
        side=side,
        order_type=OrderType.MARKET,
        quantity=qty,
    )


@pytest.fixture
def risk() -> RiskManager:
    auth = MagicMock()
    auth.primary_account_id = "ACC001"
    auth.metadata.return_value = []
    auth.channel = MagicMock()
    mgr = RiskManager(RiskConfig(), auth)
    # Подменяем _fetch_initial_margin чтобы не ходить в gRPC
    mgr._fetch_initial_margin = AsyncMock(return_value=None)
    return mgr


@pytest.mark.asyncio
async def test_check_passes_by_default(risk: RiskManager):
    """Пустой AccountState — проверка проходит."""
    await risk.check(_make_req())


@pytest.mark.asyncio
async def test_circuit_breaker_blocks(risk: RiskManager):
    risk.trip_circuit_breaker("test trip")
    with pytest.raises(AppError) as exc_info:
        await risk.check(_make_req())
    assert exc_info.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert "circuit breaker" in str(exc_info.value)


@pytest.mark.asyncio
async def test_daily_loss_limit(risk: RiskManager):
    risk._state = AccountState(liquid_value=100_000, daily_pnl=-6_000)  # 6% убытка
    with pytest.raises(AppError) as exc_info:
        await risk.check(_make_req())
    assert exc_info.value.code == ErrorCode.DAILY_LOSS_LIMIT_HIT


@pytest.mark.asyncio
async def test_max_positions(risk: RiskManager):
    risk._state = AccountState(open_positions=3)  # max=3 в RiskConfig по умолчанию
    with pytest.raises(AppError) as exc_info:
        await risk.check(_make_req())
    assert exc_info.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert "max positions" in str(exc_info.value)


@pytest.mark.asyncio
async def test_insufficient_margin(risk: RiskManager):
    risk._state = AccountState(liquid_value=100_000, free_margin=1_000)
    risk._fetch_initial_margin = AsyncMock(return_value=5_000.0)  # нужно 5000, есть 1000
    with pytest.raises(AppError) as exc_info:
        await risk.check(_make_req(qty=1))
    assert exc_info.value.code == ErrorCode.INSUFFICIENT_MARGIN
