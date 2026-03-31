import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from src.core.domain import Symbol, Quote, Bar, OrderUpdate, Signal
from ..core.errors import Result, AppError, ErrorCode
from ..auth.manager import AuthManager
from ..api.security import SecurityClient  # для GetTradingInfo

logger = logging.getLogger(__name__)

@dataclass
class RiskConfig:
    per_trade_pct: float = 2.0          # % капитала на сделку
    max_daily_loss_pct: float = 5.0     # дневной стоп
    max_drawdown_pct: float = 15.0      # максимальная просадка
    max_positions: int = 3              # лимит открытых позиций
    require_stop_loss: bool = True      # обязательный стоп-лосс
    rollover_days: int = 5              # дней до экспирации для ролловера

@dataclass
class AccountState:
    liquid_value: float = 0.0    # стоимость портфеля
    used_margin: float = 0.0     # зарезервированное ГО
    free_margin: float = 0.0     # доступно для новых позиций
    daily_pnl: float = 0.0       # нереализованный PnL за день
    open_positions: int = 0      # количество открытых позиций
    updated_at: datetime = field(default_factory=datetime.utcnow)

class RiskManager:
    """Проверка лимитов перед каждым ордером + circuit breaker"""
    
    def __init__(self, cfg: RiskConfig, auth: AuthManager, security: SecurityClient):
        self._cfg = cfg
        self._auth = auth
        self._security = security
        self._state = AccountState()
        self._peak_liquid = 0.0
        self._tripped = False
        self._trip_reason: Optional[str] = None
        self._poll_task: Optional[asyncio.Task] = None
    
    async def start(self, poll_interval: int = 10) -> None:
        """Фоновое обновление AccountState из API"""
        async def _poll():
            while True:
                await asyncio.sleep(poll_interval)
                await self._refresh_account()
        self._poll_task = asyncio.create_task(_poll())
    
    async def check(self, req: OrderRequest) -> Result[None]:
        """Проверка всех лимитов перед отправкой ордера"""
        if self._tripped:
            return AppError(ErrorCode.RISK_LIMIT_EXCEEDED, f"circuit breaker: {self._trip_reason}")
        
        if (err := self._check_daily_loss()): return err
        if (err := self._check_drawdown()): return err
        if (err := self._check_position_count()): return err
        if (err := await self._check_margin(req)): return err
        if (err := self._check_expiry(req.symbol)): return err
        return None
    
    def on_fill(self, upd: OrderUpdate) -> None:
        """Обновление PnL после исполнения — для daily loss / drawdown"""
        if upd.qty_filled <= 0: return
        # Упрощённо: если ордер закрывает позицию — считаем PnL
        # (полная логика требует трекинга entry_price — как в C++)
        pnl = (upd.price - self._entry_price.get(upd.transaction_id, 0)) * upd.qty_filled
        self._state.daily_pnl += pnl
        self._state.liquid_value += pnl
        if self._state.liquid_value > self._peak_liquid:
            self._peak_liquid = self._state.liquid_value
    
    def trip_circuit_breaker(self, reason: str) -> None:
        if not self._tripped:
            self._tripped = True
            self._trip_reason = reason
            logger.critical(f"[Risk] CIRCUIT BREAKER: {reason}")
    
    def is_tripped(self) -> bool:
        return self._tripped
    
    # --- Внутренние проверки ---
    
    def _check_daily_loss(self) -> Optional[AppError]:
        if self._state.liquid_value <= 0: return None
        loss_pct = (-self._state.daily_pnl / self._state.liquid_value) * 100
        if loss_pct >= self._cfg.max_daily_loss_pct:
            return AppError(ErrorCode.DAILY_LOSS_LIMIT_HIT, f"daily loss {loss_pct:.1f}%")
        return None
    
    def _check_drawdown(self) -> Optional[AppError]:
        if self._peak_liquid <= 0: return None
        dd_pct = ((self._peak_liquid - self._state.liquid_value) / self._peak_liquid) * 100
        if dd_pct >= self._cfg.max_drawdown_pct:
            return AppError(ErrorCode.RISK_LIMIT_EXCEEDED, f"drawdown {dd_pct:.1f}%")
        return None
    
    def _check_position_count(self) -> Optional[AppError]:
        if self._state.open_positions >= self._cfg.max_positions:
            return AppError(ErrorCode.RISK_LIMIT_EXCEEDED, "max positions reached")
        return None
    
    async def _check_margin(self, req: OrderRequest) -> Optional[AppError]:
        """Запрос ГО через SecurityClient + проверка free_margin"""
        is_long = req.side == OrderSide.BUY
        margin_r = await self._security.get_initial_margin(req.symbol, is_long)
        if isinstance(margin_r, AppError):
            logger.warning(f"[Risk] margin fetch failed: {margin_r}")
            return None  # пропускаем, если не удалось получить (как в C++)
        
        required = margin_r * req.quantity
        if required > self._state.free_margin:
            return AppError(ErrorCode.INSUFFICIENT_MARGIN, f"need {required:.0f}, free {self._state.free_margin:.0f}")
        
        # per-trade лимит
        max_allowed = self._state.liquid_value * self._cfg.per_trade_pct / 100
        if required > max_allowed:
            return AppError(ErrorCode.RISK_LIMIT_EXCEEDED, f"per-trade limit: {required:.0f} > {max_allowed:.0f}")
        return None
    
    def _check_expiry(self, sym: Symbol) -> Optional[AppError]:
        """Блокировка входа в зоне ролловера (как core::expiry_day в C++)"""
        from ..core.contract import expiry_day, nearest_contract
        ed = expiry_day(sym)
        if ed == 0: return None  # не распарсили — пропускаем
        
        # Извлекаем год/месяц из символа "Si-6.26"
        # ... (логика как в C++ contract.hpp) ...
        # Если cur_day >= ed - rollover_days → запрещаем вход
        if in_rollover_window:  # псевдокод
            return AppError(ErrorCode.RISK_LIMIT_EXCEEDED, f"rollover window: {sym}")
        return None
    
    async def _refresh_account(self) -> None:
        """Обновление AccountState из AccountService.GetPortfolio"""
        # Реализация через AccountClient — аналогично C++
        pass
    
    async def shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try: await self._poll_task
            except asyncio.CancelledError: pass