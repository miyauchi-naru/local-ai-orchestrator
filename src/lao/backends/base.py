"""エンジン共通の口：起動・停止・健全性・取消。

本ツールがロードと停止を直接統括する（R05）。エンジン側の隠れた自動ロードに任せない。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx


class EngineError(RuntimeError):
    """エンジンが異常終了・無応答になった（GPU タイムアウトを含む）。"""


class ManagedProcess:
    """エンジンのプロセスを起動し、準備完了を待ち、止める。"""

    def __init__(self, name: str, cmd: list[str], cwd: str | None, log_dir: Path, env: dict | None = None):
        self.name, self.cmd, self.cwd = name, cmd, cwd or None
        self.log_path = log_dir / f"{name}.log"
        self.env = env
        self.proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("ab")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        env = {**os.environ, **(self.env or {})}
        self.proc = subprocess.Popen(
            self.cmd, cwd=self.cwd, stdout=log, stderr=subprocess.STDOUT, env=env, creationflags=flags
        )

    async def wait_ready(self, url: str, timeout: float = 600.0, ok_status=(200,)) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.monotonic() < deadline:
                if not self.running:
                    raise EngineError(f"{self.name} が起動中に終了しました（ログ：{self.log_path}）")
                try:
                    r = await client.get(url)
                    if r.status_code in ok_status:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise EngineError(f"{self.name} が {timeout:.0f} 秒以内に準備完了になりませんでした")

    async def stop(self, grace: float = 15.0) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                await asyncio.to_thread(self.proc.wait, grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                await asyncio.to_thread(self.proc.wait)
        self.proc = None


async def with_watchdog(coro, seconds: float, what: str):
    """一定時間で終わらない推論・生成をエンジン異常として扱う（GPU タイムアウト対策）。"""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError as e:
        raise EngineError(f"{what} が {seconds:.0f} 秒以内に終わりませんでした") from e
    except httpx.TransportError as e:
        raise EngineError(f"{what} の途中でエンジンとの接続が切れました：{e}") from e
