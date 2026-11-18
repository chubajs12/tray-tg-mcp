"""Ограничение частоты записи в Telegram и учет FloodWait."""

from __future__ import annotations

import asyncio
import time


class WriteLimiter:
    """Не чаще одной операции записи в min_interval секунд; после FloodWait — блок до истечения.

    Работает только внутри одного asyncio-loop (loop клиента).
    """

    def __init__(self, min_interval: float = 1.0, clock=time.monotonic, sleep=asyncio.sleep):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._blocked_until = 0.0

    def blocked_for(self) -> float:
        return max(0.0, self._blocked_until - self._clock())

    def block(self, seconds: float) -> None:
        """Telegram ответил FloodWait: запись запрещена на seconds."""
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            if now < self._next_at:
                await self._sleep(self._next_at - now)
                now = self._clock()
            self._next_at = now + self.min_interval
