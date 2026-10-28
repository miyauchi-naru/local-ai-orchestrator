"""全エンジン共通の推論ロック（R08）。

モデル推論は補助モデルを含めて一度に一つ。対話モデルがロックを持ったまま生成完了を
待つことはしない（呼び出し側で、ツール呼出しを出したらロックを手放す）。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager


class InferenceLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.holder: str | None = None
        self.since: float | None = None
        self.waiting = 0

    @asynccontextmanager
    async def hold(self, holder: str):
        self.waiting += 1
        try:
            await self._lock.acquire()
        finally:
            self.waiting -= 1
        self.holder, self.since = holder, time.monotonic()
        try:
            yield
        finally:
            self.holder, self.since = None, None
            self._lock.release()

    def status(self) -> dict:
        return {
            "holder": self.holder,
            "held_seconds": round(time.monotonic() - self.since, 1) if self.since else 0,
            "waiting": self.waiting,
        }
