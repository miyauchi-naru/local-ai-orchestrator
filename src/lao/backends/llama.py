"""llama.cpp（llama-server）の操作。Ollama は使わない（R05）。

部品ごとに llama-server を 1 プロセス起動する。並列スロットは 1（推論は一つずつ）。
トークン数は llama-server 自身の chat template と tokenizer で数える（R15）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .base import EngineError, ManagedProcess, with_watchdog


def find_model_files(folder: Path) -> tuple[Path, Path | None]:
    """フォルダから本体 GGUF と mmproj（マルチモーダル用投影）を探す。"""
    ggufs = sorted(folder.rglob("*.gguf"))
    mmproj = [p for p in ggufs if p.name.lower().startswith("mmproj")]
    main = [p for p in ggufs if p not in mmproj]
    if not main:
        raise FileNotFoundError(f"{folder} に GGUF がありません")
    # 分割ファイルは 1 本目を渡す
    first = [p for p in main if "-00001-of-" in p.name] or main
    return first[0], (mmproj[0] if mmproj else None)


class LlamaServer:
    def __init__(
        self,
        name: str,
        binary: str,
        model_dir: Path,
        port: int,
        context: int,
        log_dir: Path,
        args: list[str] | None = None,
        watchdog_seconds: float = 900,
    ):
        self.name = name
        self.binary = binary
        self.model_dir = model_dir
        self.port = port
        self.context = context
        self.args = list(args or [])
        self.url = f"http://127.0.0.1:{port}"
        self.watchdog_seconds = watchdog_seconds
        self.proc: ManagedProcess | None = None
        self.log_dir = log_dir
        self._client = httpx.AsyncClient(base_url=self.url, timeout=None)

    def command(self) -> list[str]:
        model, mmproj = find_model_files(self.model_dir)
        cmd = [
            self.binary,
            "-m", str(model),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-c", str(self.context),
            "--parallel", "1",
            "--no-webui",
        ]
        if mmproj:
            cmd += ["--mmproj", str(mmproj)]
        return cmd + self.args

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.running)

    async def load(self) -> None:
        if self.running:
            return
        self.proc = ManagedProcess(self.name, self.command(), None, self.log_dir)
        self.proc.start()
        await self.proc.wait_ready(f"{self.url}/health")

    async def unload(self) -> None:
        if self.proc:
            await self.proc.stop()
            self.proc = None

    async def count_tokens(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        """テキスト部分のトークン数。画像・音声の展開分は呼び出し側で別に見積もる。"""
        text_only = [_strip_media(m) for m in messages]
        body: dict[str, Any] = {"messages": text_only}
        if tools:
            body["tools"] = tools
        r = await self._client.post("/apply-template", json=body)
        r.raise_for_status()
        prompt = r.json()["prompt"]
        r = await self._client.post("/tokenize", json={"content": prompt})
        r.raise_for_status()
        return len(r.json()["tokens"])

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> dict:
        if not self.running:
            raise EngineError(f"{self.name} がロードされていません")
        body: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens, "stream": False}
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature

        async def call():
            r = await self._client.post("/v1/chat/completions", json=body)
            if r.status_code >= 400:
                raise EngineError(f"{self.name}: HTTP {r.status_code} {r.text[:500]}")
            return r.json()

        try:
            return await with_watchdog(call(), self.watchdog_seconds, f"{self.name} の推論")
        except EngineError:
            if not self.running:
                raise EngineError(f"{self.name} が異常終了しました（ログ：{self.proc and self.proc.log_path}）")
            raise

    async def close(self) -> None:
        await self._client.aclose()


def _strip_media(m: dict) -> dict:
    c = m.get("content")
    if isinstance(c, list):
        texts = [p.get("text", "") for p in c if p.get("type") == "text"]
        return {**m, "content": "\n".join(texts)}
    return m
