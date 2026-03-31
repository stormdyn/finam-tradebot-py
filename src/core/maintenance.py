"""src/core/maintenance.py — Окно техобслуживания MOEX (аналог core/maintenance.hpp)"""
from __future__ import annotations
import asyncio
import time
import logging
logger = logging.getLogger(__name__)

class MaintenanceWindow:
    """
    MOEX FORTS техобслуживание: 05:00–06:15 MSK ежедневно.
    MSK = UTC+3, поэтому окно: [02:00, 03:15) UTC.
    
    Порт C++ core::MaintenanceWindow.
    """
    
    def __init__(
        self,
        start_utc_min: int = 120,  # 02:00 UTC (05:00 MSK)
        end_utc_min: int = 195,    # 03:15 UTC (06:15 MSK)
    ) -> None:
        self._start_utc_min = start_utc_min
        self._end_utc_min = end_utc_min
    
    def is_active(self) -> bool:
        """Проверка: сейчас в окне техобслуживания"""
        utc_min = self._utc_min_now()
        return self._start_utc_min <= utc_min < self._end_utc_min
    
    async def wait_if_active(self, stop_event: asyncio.Event) -> bool:
        """
        Блокирует до выхода из окна или stop_event.
        Возвращает: True — вышли из окна, False — остановка по stop_event
        """
        if not self.is_active():
            return True
        
        logger.warning("maintenance_window_active: window=05:00-06:15 MSK")
        
        while not stop_event.is_set():
            await asyncio.sleep(30)  # проверка каждые 30с
            if not self.is_active():
                logger.info("maintenance_window_ended")
                return True
        
        return False
    
    @staticmethod
    def _utc_min_now() -> int:
        """Текущая минута суток UTC"""
        return (int(time.time()) % 86400) // 60