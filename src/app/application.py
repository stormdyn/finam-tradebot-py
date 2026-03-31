"""src/app/application.py — точка входа asyncio event loop (аналог C++ main.cpp).

Жизненный цикл:
  INIT → CONNECTING → RUNNING → STOPPING → STOPPED

Graceful shutdown:
  SIGINT/SIGTERM → stop_event.set() → отмена всех Task → закрытие канала.
"""
from __future__ import annotations
import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Optional

from auth.manager import AuthManager
from core.contract import nearest_contract
from core.errors import AppError
from core.maintenance import wait_if_maintenance
from risk.manager import RiskConfig, RiskManager
from api.market_data import MarketDataClient
from api.order_client import DryRunExecutor, OrderExecutor

logger = logging.getLogger(__name__)


@dataclass
class SymbolConfig:
    ticker: str
    tick_size: float = 1.0
    base_qty: int = 1
    sl_ticks: float = 30.0
    tp_ticks: float = 90.0
    rollover_days: int = 5


@dataclass
class AppConfig:
    endpoint: str = "api.finam.ru:443"
    use_tls: bool = True
    dry_run: bool = False
    debug: bool = False
    risk: RiskConfig = field(default_factory=RiskConfig)
    symbols: list[SymbolConfig] = field(default_factory=lambda: [
        SymbolConfig(ticker="Si"),
        SymbolConfig(ticker="RI", tick_size=10.0),
        SymbolConfig(ticker="GD"),
        SymbolConfig(ticker="MX", tick_size=0.05),
        SymbolConfig(ticker="BR", tick_size=0.01),
    ])


class Application:
    """Главный класс приложения."""

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._auth: Optional[AuthManager] = None
        self._risk: Optional[RiskManager] = None
        self._executor: Optional[OrderExecutor] = None

    async def run(self) -> None:
        """Точка входа: инициализация, запуск, ожидание сигнала."""
        secret = os.environ.get("FINAM_SECRET_TOKEN", "")
        if not secret:
            raise RuntimeError("FINAM_SECRET_TOKEN env var is not set")

        # ── Auth ─────────────────────────────────────────────────────────
        auth = AuthManager(self._cfg.endpoint, self._cfg.use_tls)
        await auth.init(secret)
        secret = ""  # убираем ссылку на секрет как можно раньше
        self._auth = auth

        # ── Risk ─────────────────────────────────────────────────────────
        risk = RiskManager(self._cfg.risk, auth)
        await risk.start()
        self._risk = risk

        # ── Market data ──────────────────────────────────────────────────
        md = MarketDataClient(auth)

        # ── Order executor ───────────────────────────────────────────────
        if self._cfg.dry_run:
            logger.warning("DRY-RUN mode: orders will NOT be sent to the exchange")
            executor = DryRunExecutor()
        else:
            order_exec = OrderExecutor(auth, risk)
            await order_exec.start()
            self._executor = order_exec
            executor = order_exec

        # ── Ждём конца техобслуживания ───────────────────────────────────
        await wait_if_maintenance(self._stop)
        if self._stop.is_set():
            await self._shutdown()
            return

        # ── Стратегии ────────────────────────────────────────────────────
        from strategy.runner import StrategyRunner, RunnerConfig  # noqa: PLC0415

        for sym_cfg in self._cfg.symbols:
            symbol = nearest_contract(sym_cfg.ticker, sym_cfg.rollover_days)
            runner = StrategyRunner(
                RunnerConfig(
                    symbol=symbol,
                    base_qty=sym_cfg.base_qty,
                    tick_size=sym_cfg.tick_size,
                    sl_ticks=sym_cfg.sl_ticks,
                    tp_ticks=sym_cfg.tp_ticks,
                ),
                md=md,
                executor=executor,
                risk=risk,
            )
            task = asyncio.create_task(
                runner.run(self._stop),
                name=f"runner-{symbol.security_code}",
            )
            self._tasks.append(task)

        # ── SIGINT / SIGTERM ─────────────────────────────────────────────
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        logger.info(
            "Bot running — %d instruments, dry_run=%s",
            len(self._tasks), self._cfg.dry_run,
        )

        await self._stop.wait()
        await self._shutdown()

    async def _shutdown(self) -> None:
        logger.info("Shutting down %d runners...", len(self._tasks))
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        if self._executor is not None:
            await self._executor.stop()

        if self._risk is not None:
            await self._risk.stop()

        if self._auth is not None:
            await self._auth.shutdown()

        logger.info("finam-tradebot stopped")
