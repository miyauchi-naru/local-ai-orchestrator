"""TTS エンジンの操作。

TTS は候補ごとに API が違うため、本ツールは次の小さな HTTP 契約だけを前提にする。
各エンジンにはこの契約に合わせた薄い中継（docs/tts-contract.md）を用意する。

  GET  /health                -> 200
  POST /tts  (JSON)           -> audio/wav のバイト列
       {"text": str, "reference_audio": str|null, "reference_text": str|null,
        "style": str|null, "speed": float, "seed": int|null}
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .base import EngineError, ManagedProcess, with_watchdog


class TTSServer:
    def __init__(self, command: list[str], cwd: str, url: str, log_dir: Path, watchdog_seconds: float = 900):
        self.command, self.cwd, self.url = command, cwd, url.rstrip("/")
        self.log_dir = log_dir
        self.watchdog_seconds = watchdog_seconds
        self.proc: ManagedProcess | None = None
        self._client = httpx.AsyncClient(base_url=self.url, timeout=None)

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.running)

    async def start(self) -> None:
        if self.running:
            return
        if not self.command:
            raise EngineError("TTS エンジンの起動コマンドが未設定です（machine.toml の [engines.tts]）")
        self.proc = ManagedProcess("tts", self.command, self.cwd, self.log_dir)
        self.proc.start()
        await self.proc.wait_ready(f"{self.url}/health")

    async def stop(self) -> None:
        if self.proc:
            await self.proc.stop()
            self.proc = None

    async def synthesize(self, out: Path, **params) -> Path:
        async def call():
            r = await self._client.post("/tts", json=params)
            if r.status_code >= 400:
                raise EngineError(f"TTS: HTTP {r.status_code} {r.text[:500]}")
            return r.content

        audio = await with_watchdog(call(), self.watchdog_seconds, "音声生成")
        out.write_bytes(audio)
        return out

    async def close(self) -> None:
        await self._client.aclose()
