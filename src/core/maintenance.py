"""src/core/maintenance.py — окно техобслуживания MOEX FORTS (аналог C++ core/maintenance.hpp)

FORTS техобслуживание: 05:00–06:15 MSK = 02:00–03:15 UTC.
"""
from __future__ import annotations
import asyncio
import time
import logging

logger = logging.getLogger(__name__)

# UTC минуты
_MAINT_START_UTC_MIN: int = 120   # 02:00 UTC = 05:00 MSK
_MAINT_END_UTC_MIN: int = 195     # 03:15 UTC = 06:15 MSK


def is_maintenance() -> bool:
    """Проверить: сейчас в окне техобслуживания FORTS."""
    utc_min = (int(time.time()) % 86400) // 60
    return _MAINT_START_UTC_MIN <= utc_min < _MAINT_END_UTC_MIN


async def wait_if_maintenance(stop_event: asyncio.Event) -> None:
    """
    Если сейчас техобслуживание — ждёт до его окончания.
    Прерывается при stop_event.
    """
    if not is_maintenance():
        return

    logger.warning("Maintenance window active (05:00–06:15 MSK), waiting...")
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30.0)
            return  # stop_event сработал
        except asyncio.TimeoutError:
            pass
        if not is_maintenance():
            logger.info("Maintenance window ended, resuming.")
            return


# Совместимый класс для кода, который создаёт объект
class MaintenanceWindow:
    """Объектный враппер — для обратной совместимости с order_client."""

    def is_active(self) -> bool:
        return is_maintenance()

    async def wait_if_active(self, stop_event: asyncio.Event) -> bool:
        """
        Аналог C++ MaintenanceWindow::wait_if_active.
        Возвращает True если вышли из окна, False если stop_event.
        """
        if not self.is_active():
            return True
        await wait_if_maintenance(stop_event)
        return not stop_event.is_set()
