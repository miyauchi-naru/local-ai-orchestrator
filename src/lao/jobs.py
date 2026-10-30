"""ジョブ管理（R09・R18）。

ジョブの状態は JSON でディスクに残す。サービスが落ちて再起動したとき、終わっていない
ジョブは「中断」にする（盲目的に再実行しない）。進捗はエンジンが返した値だけを記録する。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

STATES = ("queued", "loading", "processing", "saving", "done", "cancelled", "failed", "interrupted")
TERMINAL = {"done", "cancelled", "failed", "interrupted"}

STATE_LABELS = {
    "queued": "待機",
    "loading": "ロード中",
    "processing": "処理中",
    "saving": "保存中",
    "done": "完了",
    "cancelled": "取消",
    "failed": "失敗",
    "interrupted": "中断",
}


class Cancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    kind: str  # tool 名（generate_image 等）
    env_id: str
    configuration: str
    args: dict[str, Any]
    state: str = "queued"
    progress: float | None = None  # エンジンが返したときだけ 0〜1
    message: str = ""
    result: list[str] = field(default_factory=list)  # 成果物 ref（id@版）
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    segments_done: list[str] = field(default_factory=list)  # 区切り実行の保存済み部分

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]


class JobContext:
    """ジョブ本体に渡す操作窓口。"""

    def __init__(self, manager: "JobManager", job: Job):
        self._m, self.job = manager, job

    def set_state(self, state: str, message: str = "") -> None:
        self.check_cancel()
        self._m._update(self.job, state=state, message=message)

    def set_progress(self, value: float | None, message: str = "") -> None:
        self._m._update(self.job, progress=value, message=message or self.job.message)

    def add_segment(self, ref: str) -> None:
        self.job.segments_done.append(ref)
        self._m._save(self.job)

    def check_cancel(self) -> None:
        if self.job.cancel_requested:
            raise Cancelled()


Runner = Callable[[JobContext], Awaitable[list[str]]]


class JobManager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_hooks: dict[str, Callable[[], Awaitable[None]]] = {}
        self._listeners: list[Callable[[Job], None]] = []
        self._recover()

    # --- 永続化 ---
    def _save(self, job: Job) -> None:
        tmp = self.root / f"{job.id}.json.tmp"
        tmp.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.root / f"{job.id}.json")

    def _recover(self) -> None:
        for p in self.root.glob("*.json"):
            job = Job(**json.loads(p.read_text(encoding="utf-8")))
            if job.state not in TERMINAL:
                job.state = "interrupted"
                job.message = "サービスが停止したため中断しました。保存済みの部分だけが残っています"
                self._save(job)
            self.jobs[job.id] = job

    def _update(self, job: Job, **changes) -> None:
        for k, v in changes.items():
            setattr(job, k, v)
        job.updated_at = time.time()
        self._save(job)
        for fn in self._listeners:
            fn(job)

    def on_change(self, fn: Callable[[Job], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[Job], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    # --- 操作 ---
    def submit(self, kind: str, env_id: str, configuration: str, args: dict, runner: Runner) -> Job:
        job = Job(uuid.uuid4().hex[:12], kind, env_id, configuration, args)
        self.jobs[job.id] = job
        self._save(job)
        self._tasks[job.id] = asyncio.create_task(self._run(job, runner))
        return job

    async def _run(self, job: Job, runner: Runner) -> None:
        ctx = JobContext(self, job)
        try:
            refs = await runner(ctx)
            self._update(job, state="done", result=refs, progress=None)
        except (Cancelled, asyncio.CancelledError):
            self._update(job, state="cancelled", message="取り消しました")
        except Exception as e:  # noqa: BLE001 失敗は記録して隔離する。自動再試行はしない
            self._update(job, state="failed", error=f"{type(e).__name__}: {e}")
        finally:
            self._cancel_hooks.pop(job.id, None)

    def set_cancel_hook(self, job_id: str, hook: Callable[[], Awaitable[None]]) -> None:
        self._cancel_hooks[job_id] = hook

    async def cancel(self, job_id: str) -> Job:
        job = self.jobs[job_id]
        if job.state in TERMINAL:
            return job
        self._update(job, cancel_requested=True)
        hook = self._cancel_hooks.get(job_id)
        if hook:
            await hook()
        return job

    async def wait(self, job_id: str) -> Job:
        task = self._tasks.get(job_id)
        if task:
            await asyncio.shield(task)
        return self.jobs[job_id]

    def get(self, job_id: str) -> Job:
        return self.jobs[job_id]

    def forget(self, job_id: str) -> None:
        """終わったジョブの記録を消す（成果物は消さない）。"""
        job = self.jobs.get(job_id)
        if job is None or job.state not in TERMINAL:
            return
        del self.jobs[job_id]
        (self.root / f"{job_id}.json").unlink(missing_ok=True)

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
