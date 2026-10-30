"""全エンジン共通の推論ロック（R08）。

モデル推論は補助モデルを含めて一度に一つ。対話モデルがロックを持ったまま生成完了を
待つことはしない（呼び出し側で、ツール呼出しを出したらロックを手放す）。

待ちが重なったときは優先度の高い順に渡す（利用者の決まり）：
  0. Claude にできないこと（画像・動画・音声の生成、画像・音声・動画の理解）
  1. Claude のトークンを多く使う下請け（lao mcp の ask_local）
  2. それ以外（画面の対話、定期実行、先読み）
同じ優先度の中では来た順。動いている処理を途中で止めることはしない。
"""

from __future__ import annotations

import asyncio
import contextvars
import heapq
import itertools
import time
from contextlib import asynccontextmanager

GENERATE, DELEGATE, OTHER = 0, 1, 2
PRIORITY_LABELS = {GENERATE: "Claude にできないこと", DELEGATE: "下請け", OTHER: "その他"}
# 呼び出しの流れごとに優先度を上書きする（下請けの API が設定する）
PRIORITY: contextvars.ContextVar[int | None] = contextvars.ContextVar("lao_priority", default=None)
_BY_HOLDER = {"generate": GENERATE, "tts": GENERATE, "perceive": GENERATE}


def priority_of(holder: str) -> int:
    p = PRIORITY.get()
    if p is not None:
        return p
    return _BY_HOLDER.get(holder.split(":", 1)[0], OTHER)


class InferenceLock:
    def __init__(self) -> None:
        self._busy = False
        self._queue: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()
        self.holder: str | None = None
        self.since: float | None = None
        self.priority: int | None = None

    @property
    def waiting(self) -> int:
        return sum(1 for *_, f in self._queue if not f.done())

    async def _acquire(self, priority: int) -> None:
        if not self._busy and not self.waiting:
            self._busy = True
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._queue, (priority, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self._release()  # 渡された直後に取り消された：次へ回す
            raise

    def _release(self) -> None:
        while self._queue:
            *_, fut = heapq.heappop(self._queue)
            if not fut.done():
                fut.set_result(None)  # _busy のまま次の人に渡す
                return
        self._busy = False

    @asynccontextmanager
    async def hold(self, holder: str, priority: int | None = None):
        pr = priority_of(holder) if priority is None else priority
        await self._acquire(pr)
        self.holder, self.since, self.priority = holder, time.monotonic(), pr
        try:
            yield
        finally:
            self.holder, self.since, self.priority = None, None, None
            self._release()

    def status(self) -> dict:
        waiting = sorted((p, s) for p, s, f in self._queue if not f.done())
        return {
            "holder": self.holder,
            "held_seconds": round(time.monotonic() - self.since, 1) if self.since else 0,
            "waiting": len(waiting),
            "waiting_by_priority": {PRIORITY_LABELS[p]: sum(1 for q, _ in waiting if q == p) for p in PRIORITY_LABELS
                                    if any(q == p for q, _ in waiting)},
        }
