"""Application — top-level async event loop for the trading bot.

Responsibilities:
  - Bootstrap auth, risk, market-data, order executor
  - Launch per-symbol StrategyRunner tasks
  - Handle SIGINT/SIGTERM → graceful shutdown
  - Wait through maintenance windows before starting streams

Usage::

    import asyncio
    from app.application import Application
    from config import AppConfig

    asyncio.run(Application(AppConfig()).run())
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
from core.errors import FinamError
from core.maintenance import wait_if_maintenance
from risk.manager import RiskConfig, RiskManager
from api.market_data import MarketDataClient
from api.order_client import DryRunExecutor, OrderExecutor

log = logging.getLogger(__name__)


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
    """Async application lifecycle manager.

    State machine (informal)::

        INIT → CONNECTING → RUNNING → STOPPING → STOPPED
    """

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._auth: Optional[AuthManager] = None

    async def run(self) -> None:
        """Entry point: initialise everything and run until signal."""
        secret = os.environ.get("FINAM_SECRET_TOKEN", "")
        if not secret:
            raise RuntimeError("FINAM_SECRET_TOKEN env var is not set")

        # ── Auth ────────────────────────────────────────────────────────
        auth = AuthManager(self._cfg.endpoint, self._cfg.use_tls)
        await auth.init(secret)
        secret = ""  # zero-out ASAP — Python str is immutable but at least clears local ref
        self._auth = auth

        # ── Risk ────────────────────────────────────────────────────────
        risk = RiskManager(self._cfg.risk, auth)
        await risk.start()

        # ── Market data ─────────────────────────────────────────────────
        md = MarketDataClient(auth)

        # ── Order executor ──────────────────────────────────────────────
        if self._cfg.dry_run:
            log.warning("DRY-RUN mode: orders will NOT be sent to the exchange")
            executor = DryRunExecutor()
        else:
            order_exec = OrderExecutor(auth, risk)
            await order_exec.start()
            executor = order_exec

        # ── Wait through any active maintenance window ───────────────────
        await wait_if_maintenance(self._stop)

        # ── Strategy runners ─────────────────────────────────────────────
        from strategy.runner import StrategyRunner, RunnerConfig  # noqa: PLC0415 (lazy, avoids circular)

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

        # ── Signal handlers ──────────────────────────────────────────────
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._on_signal)

        log.info(
            "Bot running — %d instruments, dry_run=%s",
            len(self._tasks), self._cfg.dry_run,
        )

        # ── Main wait ────────────────────────────────────────────────────
        await self._stop.wait()
        await self._shutdown(executor if not self._cfg.dry_run else None, risk)

    def _on_signal(self) -> None:
        log.info("Shutdown signal received")
        self._stop.set()

    async def _shutdown(
        self,
        executor: Optional[OrderExecutor],
        risk: RiskManager,
    ) -> None:
        log.info("Shutting down %d runners...", len(self._tasks))
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        if executor is not None:
            await executor.stop()

        await risk.stop()

        if self._auth is not None:
            await self._auth.channel.close()

        log.info("finam-tradebot stopped")
