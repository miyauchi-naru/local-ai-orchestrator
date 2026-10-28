"""モデルの取得計画と取得（企画書 6 章）。

既定は「計画を表示するだけ」。--execute を付けたときだけダウンロードする。
取得は Hugging Face の公式配布から行い、中断しても .part から再開し、公開されている
sha256 と照合してから使う。量子化や形式変換はしない（R14）。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .config import Component, Configuration, MachineConfig, check_policy

HF = "https://huggingface.co"


@dataclass
class FileTask:
    component: str
    repo: str
    revision: str
    path: str
    size: int
    sha256: str | None
    dest: Path

    @property
    def done(self) -> bool:
        return self.dest.exists() and self.dest.stat().st_size == self.size


@dataclass
class Plan:
    configuration: Configuration
    tasks: list[FileTask] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)  # P0 で埋める必要がある項目
    blocked: list[str] = field(default_factory=list)  # ルール違反など

    @property
    def total_bytes(self) -> int:
        return sum(t.size for t in self.tasks)

    @property
    def remaining_bytes(self) -> int:
        return sum(t.size for t in self.tasks if not t.done)

    def summary(self, free_bytes: int | None = None) -> str:
        c = self.configuration
        lines = [f"構成 {c.id}（{c.label}、{c.status}）"]
        for comp in c.components.values():
            lines.append(f"  - {comp.key}: {comp.model}  [{comp.engine}]  ライセンス {comp.license}  "
                         f"量子化 {comp.quantization}  常駐見積もり {comp.resident_gb:.0f}GB")
        lines.append(f"  取得：{len(self.tasks)} ファイル、合計 {self.total_bytes / 1e9:.1f}GB"
                     f"（未取得 {self.remaining_bytes / 1e9:.1f}GB）")
        if free_bytes is not None:
            ok = free_bytes > self.remaining_bytes * 1.1
            lines.append(f"  ディスク空き：{free_bytes / 1e9:.0f}GB {'OK' if ok else '不足'}")
        for t in self.todo:
            lines.append(f"  TODO: {t}")
        for b in self.blocked:
            lines.append(f"  不可: {b}")
        return "\n".join(lines)


def _headers() -> dict:
    tok = os.environ.get("HF_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _file_specs(comp: Component) -> list[dict]:
    """files は "パターン" か {path, dest} の表。dest は保存先の下位フォルダ。"""
    out = []
    for f in comp.raw.get("source", {}).get("files", []):
        out.append({"path": f, "dest": ""} if isinstance(f, str) else f)
    return out


def component_dir(machine: MachineConfig, conf: Configuration, comp: Component) -> Path:
    if comp.engine == "comfyui":
        # ComfyUI の models フォルダ配下（dest で diffusion_models 等を指定）
        return Path(machine["engines"]["comfyui"].get("cwd", machine.path_of("engines") / "comfyui")) / "models"
    return machine.path_of("models") / conf.id / comp.key


def build_plan(machine: MachineConfig, conf: Configuration, client: httpx.Client | None = None) -> Plan:
    plan = Plan(conf, blocked=check_policy(machine, conf))
    client = client or httpx.Client(timeout=60, headers=_headers(), follow_redirects=True)
    for comp in conf.components.values():
        src = comp.raw.get("source", {})
        repo, rev = src.get("repo", ""), src.get("revision") or "main"
        specs = _file_specs(comp)
        if not repo or not specs:
            plan.todo.append(f"{comp.key}: 取得するリポジトリとファイルが未設定（catalog の source）")
            continue
        r = client.get(f"{HF}/api/models/{repo}/tree/{rev}", params={"recursive": "true"})
        if r.status_code == 401 or r.status_code == 403:
            plan.todo.append(f"{comp.key}: {repo} は利用条件への同意かログイン（HF_TOKEN）が必要です")
            continue
        if r.status_code == 404:
            plan.todo.append(f"{comp.key}: {repo}（{rev}）が見つかりません。catalog を確認してください")
            continue
        r.raise_for_status()
        listing = [x for x in r.json() if x.get("type") == "file"]
        pinned = src.get("sha256", {})
        base = component_dir(machine, conf, comp)
        for spec in specs:
            matched = [x for x in listing if fnmatch.fnmatch(x["path"], spec["path"])]
            if not matched:
                plan.todo.append(f"{comp.key}: {repo} に {spec['path']} に合うファイルがありません")
            for x in matched:
                lfs = x.get("lfs") or {}
                sha = pinned.get(x["path"]) or lfs.get("oid")
                if pinned.get(x["path"]) and lfs.get("oid") and pinned[x["path"]] != lfs["oid"]:
                    plan.blocked.append(f"{x['path']}: 配布元のハッシュが固定値と違います（更新された可能性）")
                dest = base / spec.get("dest", "") / Path(x["path"]).name
                plan.tasks.append(FileTask(comp.key, repo, rev, x["path"], int(lfs.get("size") or x.get("size", 0)),
                                           sha, dest))
    return plan


def download(task: FileTask, client: httpx.Client, on_progress: Callable[[int, int], None] | None = None) -> None:
    if task.done and (not task.sha256 or _sha256(task.dest) == task.sha256):
        return
    task.dest.parent.mkdir(parents=True, exist_ok=True)
    part = task.dest.with_suffix(task.dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {**_headers(), **({"Range": f"bytes={have}-"} if have else {})}
    url = f"{HF}/{task.repo}/resolve/{task.revision}/{task.path}"
    with client.stream("GET", url, headers=headers, follow_redirects=True) as r:
        if r.status_code == 416:  # 既に全部ある
            pass
        else:
            r.raise_for_status()
            mode = "ab" if have and r.status_code == 206 else "wb"
            if mode == "wb":
                have = 0
            with part.open(mode) as f:
                for chunk in r.iter_bytes(1 << 22):
                    f.write(chunk)
                    have += len(chunk)
                    if on_progress:
                        on_progress(have, task.size)
    if part.stat().st_size != task.size:
        raise IOError(f"{task.path}: サイズが合いません（{part.stat().st_size} != {task.size}）。再実行で続きから取得します")
    if task.sha256 and _sha256(part) != task.sha256:
        part.unlink()
        raise IOError(f"{task.path}: sha256 が一致しません。ファイルを破棄しました")
    part.replace(task.dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def write_lock(plan: Plan, path: Path) -> None:
    """取得した版とハッシュを記録する（再現性）。"""
    data = {t.path: {"repo": t.repo, "revision": t.revision, "sha256": t.sha256, "size": t.size,
                     "component": t.component, "dest": str(t.dest)} for t in plan.tasks}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def disk_free(path: Path) -> int:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free
