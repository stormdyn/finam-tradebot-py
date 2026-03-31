"""src/risk/manager.py — риск-менеджер (аналог C++ RiskManager).

Проверки перед каждым ордером:
  1. Circuit breaker
  2. Дневной убыток (max_daily_loss_pct)
  3. Просадка (max_drawdown_pct)
  4. Количество позиций (max_positions)
  5. ГО (initial margin из GetTradingInfo)
  6. Per-trade лимит (per_trade_pct % от капитала)
  7. Зона ролловера (rollover_days до экспирации)
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import grpc
import grpc.aio

from core.domain import (
    OrderRequest, OrderSide, OrderStatus, OrderUpdate, Symbol,
)
from core.errors import AppError, ErrorCode
from core.contract import expiry_day
from auth.manager import AuthManager

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    per_trade_pct: float = 2.0
    max_daily_loss_pct: float = 5.0
    max_drawdown_pct: float = 15.0
    max_positions: int = 3
    require_stop_loss: bool = True
    rollover_days: int = 5


@dataclass
class AccountState:
    liquid_value: float = 0.0
    used_margin: float = 0.0
    free_margin: float = 0.0
    daily_pnl: float = 0.0
    open_positions: int = 0
    updated_at: datetime = field(default_factory=datetime.utcnow)


class RiskManager:
    """Риск-менеджер с circuit breaker и фоновым опросом портфеля."""

    def __init__(self, cfg: RiskConfig, auth: AuthManager) -> None:
        self._cfg = cfg
        self._auth = auth
        self._state = AccountState()
        self._peak_liquid: float = 0.0
        self._tripped: bool = False
        self._trip_reason: str = ""
        self._poll_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        # entry_price[transaction_id] для расчёта PnL
        self._entry_prices: dict[int, float] = {}

    async def start(self, poll_interval: int = 10) -> None:
        """Запустить фоновый опрос AccountState."""
        self._poll_task = asyncio.create_task(
            self._poll_loop(poll_interval), name="risk-poll"
        )

    async def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # IRiskManager interface                                               #
    # ------------------------------------------------------------------ #

    async def check(self, req: OrderRequest) -> None:
        """
        Проверить все лимиты перед отправкой ордера.

        Raises:
            AppError: при нарушении любого лимита.
        """
        self._check_circuit_breaker()
        self._check_daily_loss()
        self._check_drawdown()
        self._check_position_count()
        self._check_expiry(req.symbol)
        await self._check_margin(req)

    def on_fill(self, upd: OrderUpdate) -> None:
        """Обновить PnL после частичного/полного исполнения."""
        if upd.qty_filled <= 0:
            return
        entry = self._entry_prices.get(upd.transaction_id, upd.price)
        if upd.side == OrderSide.SELL:
            pnl = (upd.price - entry) * upd.qty_filled
        else:
            pnl = (entry - upd.price) * upd.qty_filled
        self._state.daily_pnl += pnl
        self._state.liquid_value += pnl
        if self._state.liquid_value > self._peak_liquid:
            self._peak_liquid = self._state.liquid_value
        if upd.status == OrderStatus.FILLED:
            self._entry_prices.pop(upd.transaction_id, None)

    def is_tripped(self) -> bool:
        return self._tripped

    def trip_circuit_breaker(self, reason: str) -> None:
        if not self._tripped:
            self._tripped = True
            self._trip_reason = reason
            logger.critical("[Risk] CIRCUIT BREAKER TRIPPED: %s", reason)

    @property
    def account_state(self) -> AccountState:
        return self._state

    # ------------------------------------------------------------------ #
    # Internal checks                                                      #
    # ------------------------------------------------------------------ #

    def _check_circuit_breaker(self) -> None:
        if self._tripped:
            raise AppError(ErrorCode.RISK_LIMIT_EXCEEDED, f"circuit breaker: {self._trip_reason}")

    def _check_daily_loss(self) -> None:
        if self._state.liquid_value <= 0:
            return
        loss_pct = -self._state.daily_pnl / self._state.liquid_value * 100
        if loss_pct >= self._cfg.max_daily_loss_pct:
            raise AppError(
                ErrorCode.DAILY_LOSS_LIMIT_HIT,
                f"daily loss {loss_pct:.1f}% >= {self._cfg.max_daily_loss_pct}%",
            )

    def _check_drawdown(self) -> None:
        if self._peak_liquid <= 0:
            return
        dd_pct = (self._peak_liquid - self._state.liquid_value) / self._peak_liquid * 100
        if dd_pct >= self._cfg.max_drawdown_pct:
            raise AppError(
                ErrorCode.RISK_LIMIT_EXCEEDED,
                f"drawdown {dd_pct:.1f}% >= {self._cfg.max_drawdown_pct}%",
            )

    def _check_position_count(self) -> None:
        if self._state.open_positions >= self._cfg.max_positions:
            raise AppError(
                ErrorCode.RISK_LIMIT_EXCEEDED,
                f"max positions {self._cfg.max_positions} reached",
            )

    def _check_expiry(self, sym: Symbol) -> None:
        """Блокировать вход в зоне ролловера (rollover_days до экспирации)."""
        ed = expiry_day(sym)
        if ed == 0:
            return
        # Парсим год/месяц из 'Si-6.26'
        code = sym.security_code
        dash, dot = code.find("-"), code.find(".")
        if dash == -1 or dot == -1:
            return
        try:
            exp_month = int(code[dash + 1:dot])
            exp_year = 2020 + int(code[dot + 1:])
        except ValueError:
            return
        now = datetime.now(timezone.utc)
        from core.contract import third_friday  # noqa: PLC0415
        exp_date = datetime(exp_year, exp_month, third_friday(exp_year, exp_month), tzinfo=timezone.utc)
        days_left = (exp_date - now).days
        if days_left <= self._cfg.rollover_days:
            raise AppError(
                ErrorCode.RISK_LIMIT_EXCEEDED,
                f"rollover window: {days_left}d left for {sym}",
            )

    async def _check_margin(self, req: OrderRequest) -> None:
        """Проверка ГО через GetTradingInfo."""
        margin = await self._fetch_initial_margin(req.symbol, req.side == OrderSide.BUY)
        if margin is None:
            logger.warning("[Risk] could not fetch margin for %s, skipping check", req.symbol)
            return
        required = margin * req.quantity
        async with self._lock:
            if self._state.free_margin > 0 and required > self._state.free_margin:
                raise AppError(
                    ErrorCode.INSUFFICIENT_MARGIN,
                    f"need {required:.0f}, free {self._state.free_margin:.0f}",
                )
            if self._state.liquid_value > 0:
                max_allowed = self._state.liquid_value * self._cfg.per_trade_pct / 100
                if required > max_allowed:
                    raise AppError(
                        ErrorCode.RISK_LIMIT_EXCEEDED,
                        f"per-trade limit: {required:.0f} > {max_allowed:.0f}",
                    )

    async def _fetch_initial_margin(self, symbol: Symbol, is_long: bool) -> Optional[float]:
        """
        Получить ГО из SecurityService.GetTradingInfo.
        Возвращает None при ошибке (проверка пропускается, как в C++).
        """
        try:
            from proto.grpc_trade_api.v1.assets import assetsservice_pb2 as pb  # type: ignore
            from proto.grpc_trade_api.v1.assets import assetsservice_pb2_grpc as pb_grpc  # type: ignore
        except ModuleNotFoundError:
            return None
        try:
            stub = pb_grpc.AssetsServiceStub(self._auth.channel)
            resp = await stub.GetTradingInfo(
                pb.GetTradingInfoRequest(
                    symbol=str(symbol),
                    account_id=self._auth.primary_account_id,
                ),
                metadata=self._auth.metadata(),
            )
            if is_long:
                return float(resp.long_initial_margin.value or 0)
            return float(resp.short_initial_margin.value or 0)
        except grpc.aio.AioRpcError as e:
            logger.warning("[Risk] GetTradingInfo failed: %s", e.details())
            return None

    async def _poll_loop(self, interval: int) -> None:
        """Фоновое обновление AccountState из AccountService.GetAccount."""
        while True:
            await asyncio.sleep(interval)
            await self._refresh_account()

    async def _refresh_account(self) -> None:
        try:
            from proto.grpc_trade_api.v1.accounts import accountsservice_pb2 as pb  # type: ignore
            from proto.grpc_trade_api.v1.accounts import accountsservice_pb2_grpc as pb_grpc  # type: ignore
        except ModuleNotFoundError:
            return
        try:
            stub = pb_grpc.AccountsServiceStub(self._auth.channel)
            resp = await stub.GetAccount(
                pb.GetAccountRequest(account_id=self._auth.primary_account_id),
                metadata=self._auth.metadata(),
            )
            async with self._lock:
                # equity = liquid_value
                try:
                    lv = float(resp.equity.value)
                except (AttributeError, ValueError):
                    lv = self._state.liquid_value
                # FORTS: portfolioforts.available_cash = free margin
                try:
                    fm = float(resp.portfolio_forts.available_cash.value)
                except (AttributeError, ValueError):
                    fm = self._state.free_margin
                try:
                    pnl = float(resp.unrealized_profit.value)
                except (AttributeError, ValueError):
                    pnl = self._state.daily_pnl

                self._state.liquid_value = lv
                self._state.free_margin = fm
                self._state.daily_pnl = pnl
                self._state.updated_at = datetime.utcnow()
                if lv > self._peak_liquid:
                    self._peak_liquid = lv
        except grpc.aio.AioRpcError as e:
            logger.warning("[Risk] GetAccount failed: %s", e.details())
