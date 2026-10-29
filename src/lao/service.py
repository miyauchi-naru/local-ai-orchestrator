"""サービス全体の組み立て：設定・カタログ・成果物・ジョブ・選択中の環境。"""

from __future__ import annotations

from pathlib import Path

from .config import Environment, MachineConfig, load_catalog, missing_capabilities, check_policy
from .jobs import TERMINAL, JobManager
from .library import Library
from .lock import InferenceLock
from .media import Media
from .orchestrator import Orchestrator
from .runtime import Runtime, default_engine_factory


class Service:
    def __init__(self, machine: MachineConfig, catalog_dir: Path, engine_factory=None, memory_probe=None):
        self.machine = machine
        self.catalog_dir = catalog_dir
        self.catalog: dict[str, Environment] = load_catalog(catalog_dir)
        root = machine.path_of("root")
        self.library = Library(machine.path_of("library"))
        self.jobs = JobManager(machine.path_of("jobs"))
        ff = machine.raw.get("engines", {}).get("ffmpeg", {})
        self.media = Media(ff.get("binary", "ffmpeg"), ff.get("ffprobe", "ffprobe"))
        self.lock = InferenceLock()
        self.work_dir = root / "work"
        self.log_dir = root / "logs"
        self._factory = engine_factory or default_engine_factory(machine, self.log_dir)
        self.memory_probe = memory_probe
        self.runtime: Runtime | None = None
        self.orchestrator: Orchestrator | None = None

    def environments(self) -> list[dict]:
        out = []
        for env in self.catalog.values():
            confs = []
            for c in env.configurations.values():
                confs.append({
                    "id": c.id, "label": c.label, "status": c.status,
                    "missing": missing_capabilities(env, c), "policy": check_policy(self.machine, c),
                    "resident_gb": round(sum(x.resident_gb for x in c.components.values()), 1),
                    "download_gb": round(sum(x.size_gb for x in c.components.values()), 1),
                    "models": {k: x.model for k, x in c.components.items()},
                    "licenses": sorted({x.license for x in c.components.values()}),
                })
            out.append({"id": env.id, "name": env.name, "configurations": confs})
        return out

    def busy(self) -> bool:
        return any(j.state not in TERMINAL for j in self.jobs.list())

    async def select(self, env_id: str, conf_id: str | None = None) -> Runtime:
        """環境を切り替える。ユーザーの明示操作でのみ呼ぶ（R07）。"""
        if env_id not in self.catalog:
            raise KeyError(f"環境 {env_id} はありません")
        env = self.catalog[env_id]
        conf_id = conf_id or next(iter(env.configurations))
        if conf_id not in env.configurations:
            raise KeyError(f"構成 {conf_id} はありません")
        if self.busy():
            raise RuntimeError("実行中のジョブがあるため切り替えられません。完了か取消を待ってください")
        if self.runtime:
            await self.runtime.unload_all()
        self.runtime = Runtime(self.machine, env, env.configurations[conf_id], self._factory, lock=self.lock,
                               probe=self.memory_probe)
        self.orchestrator = Orchestrator(self.runtime, self.library, self.jobs, self.media,
                                         self.work_dir, self.catalog_dir)
        return self.runtime

    async def shutdown(self) -> None:
        if self.runtime:
            await self.runtime.unload_all()
