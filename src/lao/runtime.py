"""選択中の環境の部品（モデル・エンジン）をメモリ予算内でロード・アンロードする。

ロードは常にこのクラスを通す。予算に入らなければ、同じ構成内の使っていない部品を
古い順に下ろす（構成の外へは切り替えない：R07）。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Component, Configuration, Environment, MachineConfig, check_policy, missing_capabilities
from .lock import GENERATE, InferenceLock, priority_of
from .memory import Decision, MemoryGovernor


class Engine(Protocol):
    @property
    def running(self) -> bool: ...
    async def load(self) -> None: ...
    async def unload(self) -> None: ...


class NotAdmitted(RuntimeError):
    def __init__(self, decision: Decision):
        super().__init__(decision.explain())
        self.decision = decision


@dataclass
class Loaded:
    component: Component
    engine: Engine
    resident_gb: float


class Runtime:
    def __init__(
        self,
        machine: MachineConfig,
        env: Environment,
        conf: Configuration,
        engine_factory,
        governor: MemoryGovernor | None = None,
        lock: InferenceLock | None = None,
        probe=None,
    ):
        problems = missing_capabilities(env, conf) + check_policy(machine, conf)
        self.limited = problems  # 空でなければ「機能限定」。完成扱いにしない（R12）
        self.machine, self.env, self.conf = machine, env, conf
        self.governor = governor or MemoryGovernor(
            machine.budget_gb, machine.safety_factor, float(machine["memory"].get("min_free_system_gb", 8)),
            **({"probe": probe} if probe else {}),
        )
        self.lock = lock or InferenceLock()
        self._factory = engine_factory
        self._engines: dict[str, Engine] = {}
        self._loading = asyncio.Lock()
        self.last_used = time.monotonic()
        h = machine.raw.get("health", {})
        # 熱いときの小休止：生成の区切りの前に GPU の温度を見て、高ければ下がるまで待つ（ノート PC で蓋を閉じて使うため）
        self.cool_above = float(h.get("cooldown_above_c", 85))
        self.cool_to = float(h.get("cooldown_to_c", 75))
        self.cool_max_seconds = float(h.get("cooldown_max_seconds", 600))
        self.temp_probe = None  # () -> 温度（℃）か None。Service が渡す
        self.cooling: float | None = None  # 冷ましている間は温度

    def resident_estimate(self, c: Component) -> float:
        m = self.machine.measured(self.env.id, c.key)
        return float(m["peak_gb"]) if m and "peak_gb" in m else c.resident_gb

    def engine(self, key: str) -> Engine:
        if key not in self._engines:
            self._engines[key] = self._factory(self.env, self.conf, self.conf.components[key])
        return self._engines[key]

    async def ensure(self, keys: list[str], work_gb: float = 0.0) -> Decision:
        """keys の部品を常駐させ、work_gb の作業領域が取れる状態にする。取れなければ NotAdmitted。"""
        async with self._loading:
            load = {k: self.resident_estimate(self.conf.components[k]) for k in keys}
            d = self.governor.plan(load, work_gb, pinned=set(keys))
            if not d.ok:
                raise NotAdmitted(d)
            for k in d.evict:
                await self.unload(k)
            for k in keys:
                eng = self.engine(k)
                if not eng.running:
                    await eng.load()
                    self.governor.mark_loaded(k, load[k])
                self.governor.touch(k)
            return d

    @asynccontextmanager
    async def use(self, keys: list[str], holder: str, work_gb: float = 0.0, job_id: str | None = None):
        """推論ロックを取り、その中で部品を常駐させて使う。

        ロックの外でロードすると、待っている間に別のジョブが追い出す恐れがあるため、
        ロード確認と実行を同じロックの中で行う。
        """
        async with self.lock.hold(holder):
            if priority_of(holder) == GENERATE:
                await self.cool_down()
            self.last_used = time.monotonic()
            await self.ensure(keys, work_gb)
            if job_id and work_gb:
                self.governor.reserve(job_id, work_gb)
            try:
                yield [self.engine(k) for k in keys]
            finally:
                self.last_used = time.monotonic()
                if job_id and work_gb:
                    self.governor.release(job_id)

    async def cool_down(self, interval: float = 15.0) -> float:
        """GPU が cool_above を超えていたら、cool_to まで下がるか cool_max_seconds たつまで待つ。待った秒数を返す。"""
        if not self.temp_probe or self.cool_above <= 0:
            return 0.0
        t = await asyncio.to_thread(self.temp_probe)
        if t is None or t < self.cool_above:
            return 0.0
        start = time.monotonic()
        try:
            while t is not None and t > self.cool_to and time.monotonic() - start < self.cool_max_seconds:
                self.cooling = t
                await asyncio.sleep(interval)
                t = await asyncio.to_thread(self.temp_probe)
        finally:
            self.cooling = None
        return time.monotonic() - start

    async def unload(self, key: str) -> None:
        eng = self._engines.get(key)
        if eng and eng.running:
            await eng.unload()
        self.governor.mark_unloaded(key)

    async def unload_all(self) -> None:
        for k in list(self._engines):
            await self.unload(k)

    def loaded(self) -> list[str]:
        return [k for k, e in self._engines.items() if e.running]

    async def unload_if_idle(self, idle_seconds: float, now: float | None = None) -> list[str]:
        """idle_seconds 使われていなければ、読み込んだ部品をすべて下ろす（次に使うときに読み直す）。

        蓋を閉じて置いたままの PC で、使わないモデルが熱と電気を使い続けないようにする。
        推論ロックが空いているときだけ行う。
        """
        keys = self.loaded()
        if not keys or idle_seconds <= 0 or self.lock.status().get("holder"):
            return []
        if (now if now is not None else time.monotonic()) - self.last_used < idle_seconds:
            return []
        async with self._loading:
            for k in keys:
                await self.unload(k)
        return keys

    async def preload(self) -> list[str]:
        """構成の部品を、予算に入る分だけ先に読み込む（次の依頼の待ちを減らす）。対話の部品から順に。"""
        order = [self.conf.chat_component] + [k for k in self.conf.components if k != self.conf.chat_component]
        done = []
        for k in order:
            try:
                await self.ensure([k])
            except NotAdmitted:
                continue
            done.append(k)
        self.last_used = time.monotonic()
        return done

    def status(self) -> dict:
        return {
            "environment": {"id": self.env.id, "name": self.env.name},
            "configuration": {"id": self.conf.id, "label": self.conf.label, "status": self.conf.status},
            "limited": self.limited,
            "capabilities": sorted(self.conf.capabilities()),
            "loaded": self.loaded(),
            "memory": self.governor.snapshot(),
            "lock": self.lock.status(),
            "cooling": self.cooling,
        }


def default_engine_factory(machine: MachineConfig, log_dir: Path):
    """machine.toml の設定から本物のエンジンを作る。"""
    from .backends.comfyui import ComfyUI
    from .backends.llama import LlamaServer
    from .backends.tts import TTSServer

    watchdog = float(machine.raw.get("gpu", {}).get("watchdog_seconds", 900))
    shared: dict[str, object] = {}
    ports = {"next": int(machine["engines"]["llama"].get("port_base", 8720))}

    class ComfyComponent:
        """ComfyUI は 1 プロセスを共有し、部品のロード＝起動、アンロード＝/free とする。"""

        def __init__(self, comfy: ComfyUI):
            self.comfy = comfy
            self._loaded = False

        @property
        def running(self) -> bool:
            return self._loaded and self.comfy.running

        async def load(self) -> None:
            await self.comfy.start()
            self._loaded = True

        async def unload(self) -> None:
            await self.comfy.free()
            self._loaded = False

    class TTSComponent:
        def __init__(self, tts: TTSServer):
            self.tts = tts

        @property
        def running(self) -> bool:
            return self.tts.running

        async def load(self) -> None:
            await self.tts.start()

        async def unload(self) -> None:
            await self.tts.stop()

    def factory(env: Environment, conf: Configuration, c: Component):
        if c.engine == "llama":
            port = ports["next"]
            ports["next"] += 1
            ctx = int(c.raw.get("context", {}).get("default", 8192))
            return LlamaServer(
                name=f"{env.id}-{c.key}",
                binary=machine["engines"]["llama"]["binary"],
                model_dir=machine.path_of("models") / conf.id / c.key,
                port=port,
                context=ctx,
                log_dir=log_dir,
                args=list(c.raw.get("args", [])) + list(machine["engines"]["llama"].get("extra_args", [])),
                watchdog_seconds=watchdog,
            )
        if c.engine == "comfyui":
            if "comfyui" not in shared:
                e = machine["engines"]["comfyui"]
                shared["comfyui"] = ComfyUI(e["command"], e.get("cwd", ""), e["url"], log_dir, watchdog)
            return ComfyComponent(shared["comfyui"])  # type: ignore[arg-type]
        if c.engine == "tts":
            if "tts" not in shared:
                e = machine["engines"]["tts"]
                shared["tts"] = TTSServer(list(e.get("command", [])), e.get("cwd", ""), e["url"], log_dir, watchdog)
            return TTSComponent(shared["tts"])  # type: ignore[arg-type]
        raise ValueError(f"未対応のエンジン {c.engine}")

    return factory
