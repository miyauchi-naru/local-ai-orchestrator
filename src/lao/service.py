"""サービス全体の組み立て：設定・カタログ・成果物・ジョブ・選択中の環境。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import AsyncIterator

from .conversations import Conversation, ConversationStore, Turn, turn_messages
from .config import Environment, MachineConfig, load_catalog, load_embedding, missing_capabilities, check_policy
from .jobs import TERMINAL, JobManager
from .library import Library
from .lock import InferenceLock
from .media import Media
from .folders import FolderError, Folders, parse_folders
from .orchestrator import Event, Orchestrator, SkillRef, TurnContext
from .plugins import Command, CommandStore, PluginStore
from .user_memory import MemoryStore
from .projects import ProjectStore
from .sandbox import Sandbox, SandboxConfig
from .search import DocIndex
from .schedule import ScheduleError, Task, TaskStore
from .skills import SkillStore
from .runtime import Runtime, default_engine_factory


STYLES = {
    "standard": "",
    "concise": "要点だけを短く答えてください。",
    "explanatory": "背景や理由も含めて、順を追って丁寧に説明してください。",
    "learning": "答えをすぐに渡さず、考え方を示し、利用者が自分で理解できるよう問いかけながら進めてください。",
    "formal": "丁寧な文体で、仕事の文書に使える形で答えてください。",
}
DEFAULT_SETTINGS = {"about_me": "", "style": "standard", "custom_styles": [], "max_tokens": 2048,
                    "memory_enabled": True, "memory_sensitive": False, "effort": "", "folders": []}
BUILTIN_COMMANDS = [
    Command("スキルを作る", "会話しながらスキル（SKILL.md）の下書きを作る",
            "これから一緒にスキル（手順書）を作ります。作りたいもの：$ARGUMENTS\n\n"
            "足りない情報があれば一つずつ質問し、揃ったら次の形のコードブロック（```markdown）で SKILL.md を出してください。"
            "name は半角の小文字・数字・ハイフン、description は「いつ使うか」を一行で書きます。\n\n"
            "---\nname: 例-meeting-notes\ndescription: 会議の記録を議事録にまとめるときに使う\n---\n\n（手順）"),
    Command("リサーチ", "手元の資料（ライブラリとプロジェクトの資料）だけを調べて、出典つきの報告にまとめる",
            "次の問いについて、手元の資料だけを使って調べ、報告書にまとめてください。問い：$ARGUMENTS\n\n"
            "進め方：\n1. 問いをいくつかの小さな問いに分け、調べる順番を短く示す\n"
            "2. 小さな問いごとに search_documents で探す。言い換えや別の言葉でも探し、足りなければ範囲を変えて探し直す\n"
            "3. 量が多いときは delegate_task で担当に分けて任せ、要点と出典だけを受け取る\n"
            "4. 見つかったことを、主張ごとに出典（[題 id@版]）を付けてまとめる。資料に無いことは推測と分かるように書き、"
            "見つからなかったことは「見つからなかった」と書く\n"
            "5. 最後に、結論・根拠・分からないことの順にまとめる。長くなるときは create_file で docx にもする\n"
            "インターネットは使えません。資料にない事実を作らないでください。"),
    Command("要約", "この会話か、添付した文書を要約する", "ここまでの内容（添付があればその文書）を、要点・決まったこと・残った課題に分けて要約してください。$ARGUMENTS"),
]


class Service:
    def __init__(self, machine: MachineConfig, catalog_dir: Path, engine_factory=None, memory_probe=None,
                 embedder=None):
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
        self.agents = CommandStore(root / "agents")  # サブエージェントの役割（名前・説明・指示）
        self.memory = MemoryStore(root / "memory.json")
        self.settings_path = root / "settings.json"
        self.root = root
        self.tasks = TaskStore(root / "tasks.json")
        self.index = DocIndex(self.library, root / "index", embedder or self._embedder(root / "logs"))
        self.jobs = JobManager(machine.path_of("jobs"))
        ff = machine.raw.get("engines", {}).get("ffmpeg", {})
        self.media = Media(ff.get("binary", "ffmpeg"), ff.get("ffprobe", "ffprobe"))
        self.lock = InferenceLock()
        self.work_dir = root / "work"
        self.sandbox = Sandbox(SandboxConfig.from_machine(machine.raw, root / "sandbox"))
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
                    "chat_capabilities": sorted(c.components[c.chat_component].capabilities),
                    "capabilities": sorted(c.capabilities()),
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
            if k == "style" and v not in STYLES and not str(v).startswith("custom:"):
                raise ValueError(f"style は {list(STYLES)} か custom:名前 です")
            if k == "custom_styles":
                if not isinstance(v, list) or len(v) > 20:
                    raise ValueError("custom_styles は 20 件までの一覧です")
                v = [{"name": str(x.get("name", ""))[:40], "text": str(x.get("text", ""))[:4000]}
                     for x in v if isinstance(x, dict) and str(x.get("name", "")).strip()]
            if k in ("memory_enabled", "memory_sensitive"):
                v = bool(v)
            if k == "effort" and v not in ("", "low", "medium", "high"):
                raise ValueError("effort は low・medium・high か空です")
            if k == "max_tokens":
                v = int(v)
                if not 256 <= v <= 32768:
                    raise ValueError("max_tokens は 256〜32768 です")
            if k == "about_me":
                v = str(v)[:4000]
            if k == "folders":
                if not isinstance(v, list) or len(v) > 20:
                    raise ValueError("folders は 20 件までの一覧です")
                v = [{"path": f.path, "write": f.write} for f in parse_folders(v)]
                for f in v:
                    if not Path(f["path"]).expanduser().is_dir():
                        raise ValueError(f"フォルダ {f['path']} がありません")
            d[k] = v
        self.settings_path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        return d

    def _embedder(self, log_dir: Path):
        """意味検索のモデル。[embedding] enabled のときだけ。最初に使うときに起動する。"""
        emb = self.machine.raw.get("embedding", {})
        conf = load_embedding(self.catalog_dir)
        if not emb.get("enabled") or conf is None:
            return None
        from .backends.llama import LlamaServer
        comp = conf.components["embed"]
        return LlamaServer(name="embedding", binary=self.machine["engines"]["llama"]["binary"],
                           model_dir=self.machine.path_of("models") / conf.id / comp.key, port=int(emb.get("port", 8750)),
                           context=int(comp.raw.get("context", {}).get("default", 8192)), log_dir=log_dir,
                           args=list(comp.raw.get("args", [])))

    def folders(self) -> Folders:
        return Folders(parse_folders(self.settings().get("folders", [])), self.root / "trash")

    def turn_context(self, c: Conversation, effort: str | None = None, voice: bool = False) -> TurnContext:
        settings = self.settings()
        use_memory = settings["memory_enabled"] and not c.incognito
        folders = self.folders()
        return TurnContext(instructions=self.instructions_for(c.project_id), skills=self.active_skills(),
                           agents=self.active_agents(),
                           memories=self.memory.for_prompt(c.project_id) if use_memory else [],
                           allow_remember=use_memory,
                           effort=str(effort or settings.get("effort") or "") or None,
                           folders=folders if folders.folders else None, allow_schedule=not c.incognito,
                           knowledge=self._knowledge(c.project_id), voice=voice)

    async def run_turn(self, c: Conversation, reply: Turn, effort: str | None = None,
                       voice: bool = False) -> AsyncIterator[Event]:
        """会話の続きを作る。reply を更新しながら出来事を返す（画面と定期実行で共通）。"""
        orch = self.orchestrator
        tc = self.turn_context(c, effort, voice)
        max_tokens = int(self.settings()["max_tokens"])
        refs = [r for r in tc.knowledge if self._exists(r)] + [r for t in c.turns for r in t.attachments + t.artifacts]
        refs = list(dict.fromkeys(refs))
        messages = c.model_messages()
        # 長い会話は、前半を要約してから送る（Claude の自動要約に相当）
        if len(c.turns) - c.digest_upto > 4:
            n = await orch.measure(messages, refs, tc)
            if n + max_tokens > orch.context_limit * 0.85:
                upto = len(c.turns) - 3  # 直近の 2 往復と今回の発言は残す
                c.digest = await orch.summarize(turn_messages(c.turns[c.digest_upto:upto]), c.digest)
                c.digest_upto = upto
                self.conversations.save(c)
                messages = c.model_messages()
                msg = "会話が長くなったため、前半を要約して続けます"
                reply.progress.append(msg)
                yield Event("progress", msg, {"summarized": upto})
        async for ev in orch.respond(messages, refs, max_tokens=max_tokens, tc=tc):
            if ev.type == "text":
                reply.content += ev.text
            elif ev.type == "thinking":
                reply.thinking += ev.text
            elif ev.type == "progress":
                reply.progress.append(ev.text)
            elif ev.type == "artifacts":
                reply.artifacts += ev.data["refs"]
            elif ev.type == "error":
                reply.error = ev.text
            elif ev.type == "memory":
                self.memory.add(ev.text, c.project_id, source=c.id)
            elif ev.type == "action":
                reply.actions.append(ev.data)
            yield ev

    def apply_action(self, c: Conversation, aid: str, approve: bool) -> dict:
        """AI が提案した操作を、利用者の判断どおりに行う（または取り下げる）。"""
        for t in c.turns:
            for a in t.actions:
                if a.get("id") != aid:
                    continue
                if a.get("status") != "pending":
                    raise ValueError("この操作は既に済んでいます")
                if not approve:
                    a["status"], a["result"] = "rejected", "やめました"
                else:
                    try:
                        a["result"] = self._do_action(a, c)
                        a["status"] = "done"
                    except (FolderError, ScheduleError, KeyError, ValueError, OSError) as e:
                        a["status"], a["result"] = "failed", str(e)
                self.conversations.save(c)
                return a
        raise KeyError(aid)

    def _do_action(self, a: dict, c: Conversation) -> str:
        args = a["args"]
        if a["kind"] == "schedule":
            t = self.tasks.create(**{**args, "project_id": c.project_id})
            return f"登録しました（次は {time.strftime('%m/%d %H:%M', time.localtime(t.next_run))}）" if t.next_run else "登録しました"
        f = self.folders()  # 承認の時点の許可で、もう一度確かめる
        act = args["action"]
        if act == "write":
            return f"書き込みました：{f.write_text(args['path'], args['content'], bool(args.get('overwrite')))}"
        if act == "save":
            art = self.library.get(args["ref"])
            return f"保存しました：{f.copy_in(self.library.path(art), args['path'], bool(args.get('overwrite')))}"
        if act == "move":
            return f"移しました：{f.move(args['path'], args['to'])}"
        if act == "delete":
            return f"ごみ箱へ移しました：{f.remove(args['path'])}"
        raise ValueError(act)

    async def run_task(self, t: Task) -> str:
        """定期実行の作業を 1 回行い、新しい会話に残す。"""
        rt = self.runtime
        if not rt or (rt.env.id, rt.conf.id) != (t.env_id, t.configuration):
            return f"{t.env_id.upper()} が選ばれていないため実行しませんでした（環境は勝手に切り替えません）"
        c = self.conversations.create(t.env_id, t.configuration, project_id=t.project_id)
        c.title = f"⏰ {t.name}（{time.strftime('%m/%d %H:%M')}）"
        c.turns.append(Turn("user", t.prompt))
        reply = Turn("assistant", "")
        try:
            async for _ in self.run_turn(c, reply):
                pass
        except Exception as e:  # エンジン異常も会話に残す
            reply.error = reply.error or f"止まりました：{e}"
        c.turns.append(reply)
        self.conversations.save(c)
        t.last_conversation = c.id
        return "エラーで止まりました" if reply.error else "実行しました"

    async def scheduler(self, interval: float = 30.0) -> None:
        import asyncio
        while True:
            await asyncio.sleep(interval)
            await self.run_due()

    async def run_due(self, now: float | None = None) -> list[Task]:
        done = []
        for t in self.tasks.due(now or time.time()):
            status = await self.run_task(t)
            t.last_run, t.last_status, t.unread = time.time(), status, True
            if t.kind == "once":
                t.enabled = False
            self.tasks.save(t)
            done.append(t)
        return done

    def _knowledge(self, project_id: str) -> list[str]:
        try:
            return list(self.projects.get(project_id).knowledge) if project_id else []
        except KeyError:
            return []

    def _exists(self, ref: str) -> bool:
        try:
            self.library.get(ref)
            return True
        except (KeyError, ValueError):
            return False

    def active_skills(self) -> list[SkillRef]:
        out = [SkillRef(s.name, s.description, s.body,
                        str(self.skills.files_dir(s.name)) if self.skills.files(s.name) else "")
               for s in self.skills.enabled()]
        for p in self.plugins.list():
            if p.enabled:
                out += [SkillRef(f"{p.name}:{s.name}", s.description, s.body) for s in p.skills if s.enabled]
        return out

    def active_agents(self) -> list[SkillRef]:
        out = [SkillRef(a.name, a.description, a.body) for a in self.agents.list()]
        for p in self.plugins.list():
            if p.enabled:
                out += [SkillRef(f"{p.name}:{a.name}", a.description, a.body) for a in p.agents]
        return out

    def all_commands(self) -> list:
        out = list(BUILTIN_COMMANDS) + list(self.commands.list())
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
        style = st.get("style", "")
        if style.startswith("custom:"):
            for cs in st.get("custom_styles", []):
                if cs["name"] == style[7:] and cs["text"].strip():
                    parts.append("応答スタイル：" + cs["text"].strip())
        elif STYLES.get(style):
            parts.append("応答スタイル：" + STYLES[style])
        if st.get("memory_enabled") and not st.get("memory_sensitive"):
            parts.append("健康・宗教・政治・性・犯罪歴などの機微な話題は覚えないでください。")
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
                                         self.work_dir, self.catalog_dir, self.sandbox, self.index)
        return self.runtime

    async def shutdown(self) -> None:
        if self.runtime:
            await self.runtime.unload_all()
