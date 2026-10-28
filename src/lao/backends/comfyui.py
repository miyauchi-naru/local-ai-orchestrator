"""ComfyUI の操作（画像・動画生成）。

ワークフローは ComfyUI で作って「API 形式で書き出し」た JSON を使い、カタログの
bindings（"ノード番号.inputs.入力名"）に値を差し込む。生成後は /free でモデルを解放し、
ComfyUI 側の隠れたキャッシュでメモリ管理を迂回させない。
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from .base import EngineError, ManagedProcess, with_watchdog


def apply_bindings(workflow: dict, bindings: dict[str, str], values: dict[str, Any]) -> dict:
    """values のキーを bindings の位置へ書き込んだワークフローの複製を返す。"""
    wf = copy.deepcopy(workflow)
    for key, value in values.items():
        target = bindings.get(key)
        if not target:
            raise EngineError(f"ワークフローに '{key}' の差し込み先がありません（カタログの bindings を設定してください）")
        node_id, section, name = target.split(".", 2)
        if node_id not in wf:
            raise EngineError(f"ワークフローにノード {node_id} がありません（bindings.{key}）")
        wf[node_id].setdefault(section, {})[name] = value
    return wf


def unbound_keys(bindings: dict[str, str]) -> list[str]:
    return [k for k, v in bindings.items() if not v]


class ComfyUI:
    def __init__(self, command: list[str], cwd: str, url: str, log_dir: Path, watchdog_seconds: float = 900):
        self.command, self.cwd, self.url = command, cwd, url.rstrip("/")
        self.log_dir = log_dir
        self.watchdog_seconds = watchdog_seconds
        self.proc: ManagedProcess | None = None
        self.client_id = uuid.uuid4().hex
        self._client = httpx.AsyncClient(base_url=self.url, timeout=60.0)

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.running)

    async def start(self) -> None:
        if self.running:
            return
        self.proc = ManagedProcess("comfyui", self.command, self.cwd, self.log_dir)
        self.proc.start()
        await self.proc.wait_ready(f"{self.url}/system_stats")

    async def stop(self) -> None:
        if self.proc:
            await self.proc.stop()
            self.proc = None

    async def free(self) -> None:
        """ComfyUI が抱えているモデルとキャッシュを解放する。"""
        if self.running:
            await self._client.post("/free", json={"unload_models": True, "free_memory": True})

    async def upload_image(self, path: Path) -> str:
        with path.open("rb") as f:
            r = await self._client.post(
                "/upload/image",
                files={"image": (path.name, f, "application/octet-stream")},
                data={"overwrite": "false", "type": "input"},
            )
        r.raise_for_status()
        d = r.json()
        return f"{d['subfolder']}/{d['name']}" if d.get("subfolder") else d["name"]

    async def interrupt(self) -> None:
        if self.running:
            await self._client.post("/interrupt")

    async def run(
        self,
        workflow: dict,
        out_dir: Path,
        on_progress: Callable[[float | None, str], None] | None = None,
        poll: float = 1.0,
    ) -> list[Path]:
        """ワークフローを実行し、出力ファイルを out_dir に保存して返す。"""
        if not self.running:
            raise EngineError("ComfyUI が起動していません")
        r = await self._client.post("/prompt", json={"prompt": workflow, "client_id": self.client_id})
        if r.status_code >= 400:
            raise EngineError(f"ComfyUI がワークフローを受け付けませんでした：{r.text[:800]}")
        prompt_id = r.json()["prompt_id"]

        async def wait_done() -> dict:
            while True:
                if not self.running:
                    raise EngineError("ComfyUI が生成中に終了しました")
                h = await self._client.get(f"/history/{prompt_id}")
                data = h.json().get(prompt_id)
                if data:
                    status = data.get("status", {})
                    if status.get("status_str") == "error":
                        raise EngineError(f"ComfyUI の生成が失敗しました：{json.dumps(status.get('messages', []))[:800]}")
                    if status.get("completed", True):
                        return data
                if on_progress:
                    # ComfyUI の HTTP 履歴は割合を返さないので、割合は出さない
                    on_progress(None, "生成中")
                await asyncio.sleep(poll)

        data = await with_watchdog(wait_done(), self.watchdog_seconds, "ComfyUI の生成")
        out_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for node_out in data.get("outputs", {}).values():
            for key in ("images", "gifs", "videos", "audio"):
                for item in node_out.get(key, []):
                    if item.get("type") != "output":
                        continue
                    params = {"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": "output"}
                    resp = await self._client.get("/view", params=params)
                    resp.raise_for_status()
                    dest = out_dir / item["filename"]
                    dest.write_bytes(resp.content)
                    saved.append(dest)
        if not saved:
            raise EngineError("ComfyUI の出力ファイルが見つかりませんでした（ワークフローに保存ノードがあるか確認）")
        return saved

    async def close(self) -> None:
        await self._client.aclose()
