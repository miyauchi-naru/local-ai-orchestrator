"""サービス全体の組み立て：設定・カタログ・成果物・ジョブ・選択中の環境。"""

from __future__ import annotations

import json
from pathlib import Path

from .conversations import ConversationStore
from .config import Environment, MachineConfig, load_catalog, missing_capabilities, check_policy
from .jobs import TERMINAL, JobManager
from .library import Library
from .lock import InferenceLock
from .media import Media
from .orchestrator import Orchestrator, SkillRef
from .plugins import CommandStore, PluginStore
from .projects import ProjectStore
from .skills import SkillStore
from .runtime import Runtime, default_engine_factory


STYLES = {
    "standard": "",
    "concise": "要点だけを短く答えてください。",
    "detailed": "背景や理由も含めて詳しく、順を追って説明してください。",
    "formal": "丁寧な文体で、仕事の文書に使える形で答えてください。",
}
DEFAULT_SETTINGS = {"about_me": "", "style": "standard", "max_tokens": 2048}


class Service:
    def __init__(self, machine: MachineConfig, catalog_dir: Path, engine_factory=None, memory_probe=None):
        self.machine = machine
        self.catalog_dir = catalog_dir
        self.catalog: dict[str, Environment] = load_catalog(catalog_dir)
        root = machine.path_of("root")
        self.library = Library(machine.path_of("library"))
        self.conversations = ConversationStore(root / "conversations")
        self.projects = ProjectStore(root / "projects")
        self.skills = SkillStore(root / "skills")
        self.commands = CommandStore(root / "commands")
        self.plugins = PluginStore(root / "plugins")
        self.settings_path = root / "settings.json"
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

    # ------------------------------------------------------------ 設定・スキル・コマンド
    def settings(self) -> dict:
        d = dict(DEFAULT_SETTINGS)
        try:
            d.update(json.loads(self.settings_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
        return d

    def save_settings(self, patch: dict) -> dict:
        d = self.settings()
        for k, v in patch.items():
            if k not in DEFAULT_SETTINGS:
                continue
            if k == "style" and v not in STYLES:
                raise ValueError(f"style は {list(STYLES)} のいずれかです")
            if k == "max_tokens":
                v = int(v)
                if not 256 <= v <= 32768:
                    raise ValueError("max_tokens は 256〜32768 です")
            if k == "about_me":
                v = str(v)[:4000]
            d[k] = v
        self.settings_path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        return d

    def active_skills(self) -> list[SkillRef]:
        out = [SkillRef(s.name, s.description, s.body) for s in self.skills.enabled()]
        for p in self.plugins.list():
            if p.enabled:
                out += [SkillRef(f"{p.name}:{s.name}", s.description, s.body) for s in p.skills if s.enabled]
        return out

    def all_commands(self) -> list:
        out = list(self.commands.list())
        for p in self.plugins.list():
            if p.enabled:
                out += p.commands
        return out

    def expand_command(self, text: str) -> tuple[str, str | None]:
        """先頭が /コマンド なら依頼文に置き換える。返り値は（本文、使ったコマンド名）。"""
        if not text.startswith("/"):
            return text, None
        head, _, rest = text[1:].partition(" ")
        head, _, rest2 = head.partition("\n")
        rest = (rest2 + " " + rest).strip() if rest2 else rest
        for c in self.all_commands():
            if c.full_name == head or (c.name == head and not c.plugin):
                return c.expand(rest), c.full_name
        for c in self.all_commands():
            if c.name == head:
                return c.expand(rest), c.full_name
        return text, None

    def instructions_for(self, project_id: str = "") -> str:
        st = self.settings()
        parts = []
        if st.get("about_me", "").strip():
            parts.append("利用者について：" + st["about_me"].strip())
        if STYLES.get(st.get("style", "")):
            parts.append("応答スタイル：" + STYLES[st["style"]])
        if project_id:
            try:
                p = self.projects.get(project_id)
            except KeyError:
                p = None
            if p:
                parts.append(f"プロジェクト「{p.name}」の指示：" + (p.instructions.strip() or "（なし）"))
        return "\n".join(parts)

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
