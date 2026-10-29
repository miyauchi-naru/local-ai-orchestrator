"""ローカル API サーバー。

- /v1/chat/completions ・ /v1/models：OpenAI 互換。Open WebUI 等の既存 UI から接続できる。
- /api/*：環境の選択、添付のアップロード、成果物、ジョブの状態と取消。
- /：最小限の対話画面（既存 UI を使わない場合の主経路）。

ループバックでのみ待ち受け、API キーで保護する（企画書 10 章）。
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import tempfile
import time
import uuid
from urllib.parse import quote
from dataclasses import asdict
from importlib import resources
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from .documents import DOC_SUFFIXES, DocumentError, extract_text
from .library import Artifact
from .orchestrator import TurnContext
from .service import Service

MAX_UPLOAD_BYTES = 4 * 1024**3
DATA_URI_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "audio/wav": ".wav",
                 "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "video/mp4": ".mp4"}
MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
TEXT_EXTS = {".md", ".txt", ".html", ".svg", ".py", ".js", ".ts", ".css", ".json", ".csv", ".ps1", ".sh", ".sql",
             ".yaml", ".toml", ".xml"}


def create_app(service: Service, api_key: str | None = None) -> FastAPI:
    key = api_key or service.machine["server"].get("api_key") or secrets.token_urlsafe(24)
    @asynccontextmanager
    async def lifespan(_app):
        yield
        await service.shutdown()

    app = FastAPI(title="local-ai-orchestrator", lifespan=lifespan)
    app.state.service = service
    app.state.api_key = key

    def auth(request: Request) -> None:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        given = given or request.query_params.get("key", "")
        if not secrets.compare_digest(given, key):
            raise HTTPException(401, "API キーが違います")

    def runtime_or_409():
        if not service.orchestrator:
            raise HTTPException(409, "環境が選ばれていません。先に環境を選んでください")
        return service.orchestrator

    def art_json(a: Artifact) -> dict:
        return {"ref": a.ref, "kind": a.kind, "mime": a.mime, "title": a.title, "env": a.env_id,
                "created_at": a.created_at, "sources": a.sources, "parent": a.parent_id,
                "generation": a.generation, "url": f"/files/{a.ref}"}

    # ------------------------------------------------------------ 画面
    def asset(name: str) -> str:
        return resources.files("lao").joinpath("web", name).read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return asset("index.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return Response(asset("manifest.webmanifest"), media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        return Response(asset("sw.js"), media_type="text/javascript")

    @app.get("/icon.svg")
    def icon():
        return Response(asset("icon.svg"), media_type="image/svg+xml")

    # ------------------------------------------------------------ 環境
    @app.get("/api/environments", dependencies=[Depends(auth)])
    def environments():
        return {"environments": service.environments(),
                "active": service.runtime.status() if service.runtime else None}

    @app.post("/api/environment", dependencies=[Depends(auth)])
    async def select(body: dict):
        try:
            rt = await service.select(body["env"], body.get("configuration"))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return rt.status()

    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        return service.runtime.status() if service.runtime else {"environment": None}

    # ------------------------------------------------------------ 成果物
    @app.post("/api/upload", dependencies=[Depends(auth)])
    async def upload(file: UploadFile = File(...)):
        env = service.runtime.env.id if service.runtime else "upload"
        suffix = Path(file.filename or "file").suffix.lower()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / f"upload{suffix}"
            size = 0
            with tmp.open("wb") as f:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "ファイルが大きすぎます")
                    f.write(chunk)
            if suffix not in MEDIA_SUFFIXES | IMAGE_SUFFIXES | DOC_SUFFIXES:
                raise HTTPException(400, f"{suffix or '拡張子なし'} は添付できません（画像・音声・動画・文書・PDF）")
            title = Path(file.filename or "file").name
            if suffix in MEDIA_SUFFIXES:
                try:
                    info = service.media.probe(tmp)
                except ValueError as e:
                    raise HTTPException(400, str(e)) from e
                if suffix in (".webm", ".ogg", ".opus") and not info.has_video:
                    # 録音（音声だけの webm 等）は wav にして音声として扱う
                    tmp = service.media.to_wav(tmp, Path(d) / "voice.wav")
                    title = Path(title).with_suffix(".wav").name
            elif suffix in DOC_SUFFIXES:
                try:
                    extract_text(tmp)
                except DocumentError as e:
                    raise HTTPException(400, str(e)) from e
            art = service.library.add_file(tmp, env_id=env, configuration="upload", title=title, move=True)
        return art_json(art)

    @app.get("/api/library", dependencies=[Depends(auth)])
    def library(kind: str | None = None, env: str | None = None):
        return {"items": [art_json(a) for a in service.library.list(kind, env)]}

    @app.get("/api/library/{art_id}/versions", dependencies=[Depends(auth)])
    def versions(art_id: str):
        items = []
        for v in range(1, service.library.latest_version(art_id) + 1):
            try:
                items.append(art_json(service.library.get(f"{art_id}@{v}")))
            except (KeyError, ValueError):
                continue
        return {"items": items}

    # ------------------------------------------------------------ 会話
    @app.get("/api/conversations", dependencies=[Depends(auth)])
    def conversations(q: str = "", project: str | None = None):
        items = []
        for c in service.conversations.list():
            if project is not None and c.project_id != project:
                continue
            if q:
                hit = c.search(q)
                if hit is None:
                    continue
                items.append({**c.summary(), "snippet": hit})
            else:
                items.append(c.summary())
        return {"items": items}

    @app.post("/api/conversations", dependencies=[Depends(auth)])
    def new_conversation(body: dict | None = None):
        if not service.runtime:
            raise HTTPException(409, "環境が選ばれていません。先に環境を選んでください")
        pid = str((body or {}).get("project_id") or "")
        if pid:
            project_or_404(pid)
        c = service.conversations.create(service.runtime.env.id, service.runtime.conf.id, project_id=pid,
                                         incognito=bool((body or {}).get("incognito")))
        return c.summary()

    def conv_or_404(cid: str):
        try:
            return service.conversations.get(cid)
        except KeyError as e:
            raise HTTPException(404, "会話がありません") from e

    @app.get("/api/conversations/{cid}", dependencies=[Depends(auth)])
    def get_conversation(cid: str):
        c = conv_or_404(cid)
        return {**c.summary(), "turns": [t.__dict__ for t in c.turns]}

    @app.patch("/api/conversations/{cid}", dependencies=[Depends(auth)])
    def update_conversation(cid: str, body: dict):
        c = conv_or_404(cid)
        if "title" in body:
            c.title = str(body["title"])[:100] or c.title
        if "pinned" in body:
            c.pinned = bool(body["pinned"])
        if "project_id" in body:
            pid = str(body["project_id"] or "")
            if pid:
                project_or_404(pid)
            c.project_id = pid
        service.conversations.save(c)
        return c.summary()

    @app.delete("/api/conversations/{cid}", dependencies=[Depends(auth)])
    def delete_conversation(cid: str):
        # 会話を消しても、成果物フォルダの画像・音声・動画は残す
        service.conversations.delete(cid)
        return {"deleted": cid}

    @app.get("/api/conversations/{cid}/export", dependencies=[Depends(auth)])
    def export_conversation(cid: str):
        c = conv_or_404(cid)
        name = "".join(ch for ch in c.title if ch.isalnum())[:40] or c.id
        return Response(c.to_markdown(), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.md"})

    @app.post("/api/conversations/{cid}/messages", dependencies=[Depends(auth)])
    async def send_message(cid: str, body: dict):
        """送信。regenerate=true で最後の返答を作り直し、edit_turn=n で n 番目の発言から書き直す。"""
        orch = runtime_or_409()
        c = conv_or_404(cid)
        rt = service.runtime
        if (c.env_id, c.configuration) != (rt.env.id, rt.conf.id):
            # 勝手に環境を切り替えない（R07）。画面側で利用者に切り替えを確かめる
            raise HTTPException(409, f"この会話は {c.env_id.upper()}（{c.configuration}）の会話です。続けるには環境を切り替えてください")
        from .conversations import Turn, turn_messages

        if body.get("regenerate"):
            while c.turns and c.turns[-1].role == "assistant":
                c.turns.pop()
            if not c.turns:
                raise HTTPException(400, "作り直す返答がありません")
        else:
            text = str(body.get("text", "")).strip()
            attachments = [r for r in body.get("attachments", []) if isinstance(r, str)]
            for r in attachments:
                try:
                    service.library.get(r)
                except (KeyError, ValueError) as e:
                    raise HTTPException(400, f"添付 {r} がありません") from e
            if not text:
                raise HTTPException(400, "メッセージが空です")
            if body.get("edit_turn") is not None:
                try:
                    idx = int(body["edit_turn"])
                except (TypeError, ValueError) as e:
                    raise HTTPException(400, "edit_turn は番号です") from e
                if not (0 <= idx < len(c.turns)) or c.turns[idx].role != "user":
                    raise HTTPException(400, "書き直せるのは自分の発言だけです")
                c.turns = c.turns[:idx]
            expanded, command = service.expand_command(text)
            c.turns.append(Turn("user", expanded, attachments=attachments, command=command or ""))
            if c.title == "新しい会話":
                c.title = text.splitlines()[0][:40]
        service.conversations.save(c)
        reply = Turn("assistant", "")
        settings = service.settings()
        use_memory = settings["memory_enabled"] and not c.incognito
        effort = str(body.get("effort") or settings.get("effort") or "") or None
        tc = TurnContext(instructions=service.instructions_for(c.project_id), skills=service.active_skills(),
                         agents=service.active_agents(),
                         memories=service.memory.for_prompt(c.project_id) if use_memory else [],
                         allow_remember=use_memory, effort=effort)
        knowledge = []
        if c.project_id:
            try:
                knowledge = service.projects.get(c.project_id).knowledge
            except KeyError:
                pass
        max_tokens = int(settings["max_tokens"])

        async def stream():
            finished = False
            try:
                refs = [r for r in knowledge if _exists(r)] + [r for t in c.turns for r in t.attachments + t.artifacts]
                refs = list(dict.fromkeys(refs))
                messages = c.model_messages()
                # 長い会話は、前半を要約してから送る（Claude の自動要約に相当）
                if len(c.turns) - c.digest_upto > 4:
                    n = await orch.measure(messages, refs, tc)
                    if n + max_tokens > orch.context_limit * 0.85:
                        upto = len(c.turns) - 3  # 直近の 2 往復と今回の発言は残す
                        old = turn_messages(c.turns[c.digest_upto:upto])
                        c.digest = await orch.summarize(old, c.digest)
                        c.digest_upto = upto
                        service.conversations.save(c)
                        messages = c.model_messages()
                        msg = "会話が長くなったため、前半を要約して続けます"
                        reply.progress.append(msg)
                        yield json.dumps({"type": "progress", "text": msg, "data": {"summarized": upto}},
                                         ensure_ascii=False) + "\n"
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
                        service.memory.add(ev.text, c.project_id, source=c.id)
                    yield json.dumps({"type": ev.type, "text": ev.text, "data": ev.data}, ensure_ascii=False) + "\n"
                finished = True
            except Exception as e:  # エンジン異常も会話に残す
                reply.error = reply.error or f"止まりました：{e}"
                finished = True
                yield json.dumps({"type": "error", "text": reply.error, "data": {}}, ensure_ascii=False) + "\n"
            finally:
                if not finished:  # 画面の停止ボタンで接続が切られた
                    reply.error = reply.error or "途中で止めました"
                c.turns.append(reply)
                service.conversations.save(c)
            yield json.dumps({"type": "done", "title": c.title}, ensure_ascii=False) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.post("/api/conversations/{cid}/branch", dependencies=[Depends(auth)])
    def branch_conversation(cid: str, body: dict):
        c = conv_or_404(cid)
        try:
            n = service.conversations.branch(c, int(body.get("upto", len(c.turns) - 1)))
        except (IndexError, ValueError, TypeError) as e:
            raise HTTPException(400, "分岐する位置が正しくありません") from e
        return n.summary()

    def _exists(ref: str) -> bool:
        try:
            service.library.get(ref)
            return True
        except (KeyError, ValueError):
            return False

    # ------------------------------------------------------------ プロジェクト
    def project_or_404(pid: str):
        try:
            return service.projects.get(pid)
        except KeyError as e:
            raise HTTPException(404, "プロジェクトがありません") from e

    def project_fields(p, body: dict) -> None:
        if "name" in body:
            name = str(body["name"]).strip()[:80]
            if not name:
                raise HTTPException(400, "名前を入れてください")
            p.name = name
        if "description" in body:
            p.description = str(body["description"])[:1000]
        if "instructions" in body:
            p.instructions = str(body["instructions"])[:20000]
        if "knowledge" in body:
            refs = [r for r in body["knowledge"] if isinstance(r, str)]
            for r in refs:
                if not _exists(r):
                    raise HTTPException(400, f"資料 {r} がありません")
            p.knowledge = list(dict.fromkeys(refs))

    @app.get("/api/projects", dependencies=[Depends(auth)])
    def projects():
        convs = service.conversations.list()
        return {"items": [{**asdict(p), "conversations": sum(c.project_id == p.id for c in convs)}
                          for p in service.projects.list()]}

    @app.post("/api/projects", dependencies=[Depends(auth)])
    def new_project(body: dict):
        from .projects import Project
        p = Project("", str(body.get("name", "")).strip()[:80] or "新しいプロジェクト")
        project_fields(p, {k: v for k, v in body.items() if k != "name"})  # 誤りがあれば作らない
        p = service.projects.create(p.name, description=p.description, instructions=p.instructions,
                                    knowledge=p.knowledge)
        return asdict(p)

    @app.get("/api/projects/{pid}", dependencies=[Depends(auth)])
    def get_project(pid: str):
        return asdict(project_or_404(pid))

    @app.patch("/api/projects/{pid}", dependencies=[Depends(auth)])
    def update_project(pid: str, body: dict):
        p = project_or_404(pid)
        project_fields(p, body)
        service.projects.save(p)
        return asdict(p)

    @app.delete("/api/projects/{pid}", dependencies=[Depends(auth)])
    def delete_project(pid: str):
        project_or_404(pid)
        # 会話は消さずにプロジェクトから外す
        for c in service.conversations.list():
            if c.project_id == pid:
                c.project_id = ""
                service.conversations.save(c)
        service.projects.delete(pid)
        return {"deleted": pid}

    # ------------------------------------------------------------ 設定
    @app.get("/api/settings", dependencies=[Depends(auth)])
    def get_settings():
        from .service import STYLES
        return {**service.settings(), "styles": list(STYLES)}

    @app.put("/api/settings", dependencies=[Depends(auth)])
    def put_settings(body: dict):
        try:
            return service.save_settings(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ------------------------------------------------------------ スキル・コマンド・プラグイン
    @app.get("/api/skills", dependencies=[Depends(auth)])
    def skills():
        return {"items": [{**s.summary(), "body": s.body, "plugin": ""} for s in service.skills.list()]
                + [{**s.summary(), "name": f"{p.name}:{s.name}", "body": s.body, "plugin": p.name,
                    "enabled": s.enabled and p.enabled}
                   for p in service.plugins.list() for s in p.skills]}

    @app.put("/api/skills/{name}", dependencies=[Depends(auth)])
    def put_skill(name: str, body: dict):
        from .skills import Skill, SkillError
        sk = Skill(str(body.get("name", name)), str(body.get("description", "")), str(body.get("body", "")),
                   bool(body.get("enabled", True)))
        try:
            return service.skills.save(sk, rename_from=name).summary()
        except (SkillError, KeyError) as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/skills/{name}", dependencies=[Depends(auth)])
    def delete_skill(name: str):
        try:
            service.skills.delete(name)
        except KeyError as e:
            raise HTTPException(404, "スキルがありません") from e
        return {"deleted": name}

    # ------------------------------------------------------------ メモリ
    @app.get("/api/memory", dependencies=[Depends(auth)])
    def memory_list():
        return {"items": [asdict(m) for m in service.memory.list()], "enabled": service.settings()["memory_enabled"]}

    @app.post("/api/memory", dependencies=[Depends(auth)])
    def memory_add(body: dict):
        try:
            if "import" in body:
                return {"imported": service.memory.import_text(str(body["import"]), str(body.get("project_id", "")))}
            return asdict(service.memory.add(str(body.get("text", "")), str(body.get("project_id", ""))))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/memory/export", dependencies=[Depends(auth)])
    def memory_export():
        return Response(service.memory.export_text(), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="memory.txt"'})

    @app.patch("/api/memory/{mid}", dependencies=[Depends(auth)])
    def memory_update(mid: str, body: dict):
        try:
            return asdict(service.memory.update(mid, str(body.get("text", ""))))
        except KeyError as e:
            raise HTTPException(404, "項目がありません") from e

    @app.delete("/api/memory/{mid}", dependencies=[Depends(auth)])
    def memory_delete(mid: str):
        if mid == "all":
            service.memory.clear()
        else:
            service.memory.delete(mid)
        return {"deleted": mid}

    # ------------------------------------------------------------ サブエージェントの役割
    @app.get("/api/agents", dependencies=[Depends(auth)])
    def agents():
        return {"items": [{**a.summary(), "builtin": False} for a in service.agents.list()]
                + [{**a.summary()} for p in service.plugins.list() for a in p.agents]}

    @app.put("/api/agents/{name}", dependencies=[Depends(auth)])
    def put_agent(name: str, body: dict):
        from .plugins import Command, PluginError
        a = Command(str(body.get("name", name)), str(body.get("description", "")), str(body.get("body", "")))
        try:
            return service.agents.save(a, rename_from=name).summary()
        except (PluginError, KeyError) as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/agents/{name}", dependencies=[Depends(auth)])
    def delete_agent(name: str):
        try:
            service.agents.delete(name)
        except KeyError as e:
            raise HTTPException(404, "担当がありません") from e
        return {"deleted": name}

    # ------------------------------------------------------------ 読み上げ（E3）
    @app.post("/api/speak", dependencies=[Depends(auth)])
    async def speak(body: dict):
        from .tools import ToolError, validate
        orch = runtime_or_409()
        rt = service.runtime
        text = str(body.get("text", ""))[:10000]
        try:
            args = validate("generate_speech", {"text": text}, rt.conf, service.library)
        except ToolError as e:
            raise HTTPException(400, f"この環境では読み上げできません：{e}") from e
        job = service.jobs.submit("generate_speech", rt.env.id, rt.conf.id, args, orch._runner("generate_speech", args))
        return {"job": job.id}

    @app.get("/api/commands", dependencies=[Depends(auth)])
    def commands():
        from .service import BUILTIN_COMMANDS
        builtin = {c.name for c in BUILTIN_COMMANDS}
        return {"items": [{**c.summary(), "builtin": c.name in builtin and not c.plugin}
                          for c in service.all_commands()]}

    @app.put("/api/commands/{name}", dependencies=[Depends(auth)])
    def put_command(name: str, body: dict):
        from .plugins import Command, PluginError
        c = Command(str(body.get("name", name)), str(body.get("description", "")), str(body.get("body", "")))
        try:
            return service.commands.save(c, rename_from=name).summary()
        except (PluginError, KeyError) as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/commands/{name}", dependencies=[Depends(auth)])
    def delete_command(name: str):
        try:
            service.commands.delete(name)
        except KeyError as e:
            raise HTTPException(404, "コマンドがありません") from e
        return {"deleted": name}

    @app.get("/api/plugins", dependencies=[Depends(auth)])
    def plugins():
        return {"items": [p.summary() for p in service.plugins.list()]}

    @app.post("/api/plugins", dependencies=[Depends(auth)])
    async def install_plugin(file: UploadFile = File(...), replace: bool = False):
        from .plugins import PluginError
        from .skills import SkillError
        data = await file.read(6 * 1024 * 1024)
        try:
            return service.plugins.install_zip(data, replace=replace).summary()
        except (PluginError, SkillError) as e:
            raise HTTPException(400, str(e)) from e

    @app.patch("/api/plugins/{name}", dependencies=[Depends(auth)])
    def update_plugin(name: str, body: dict):
        try:
            return service.plugins.set_enabled(name, bool(body.get("enabled", True))).summary()
        except KeyError as e:
            raise HTTPException(404, "プラグインがありません") from e

    @app.delete("/api/plugins/{name}", dependencies=[Depends(auth)])
    def delete_plugin(name: str):
        try:
            service.plugins.delete(name)
        except KeyError as e:
            raise HTTPException(404, "プラグインがありません") from e
        return {"deleted": name}

    @app.post("/api/plugins/export", dependencies=[Depends(auth)])
    def export_plugin(body: dict):
        from .plugins import PluginError, export_zip
        name = str(body.get("name", "my-plugin"))
        want_s = set(body.get("skills") or [s.name for s in service.skills.list()])
        want_c = set(body.get("commands") or [c.name for c in service.commands.list()])
        try:
            data = export_zip(name, str(body.get("description", "")),
                              [s for s in service.skills.list() if s.name in want_s],
                              [c for c in service.commands.list() if c.name in want_c],
                              service.agents.list() if body.get("agents", True) else [])
        except PluginError as e:
            raise HTTPException(400, str(e)) from e
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

    # ------------------------------------------------------------ 成果物（文章）
    @app.post("/api/library/text", dependencies=[Depends(auth)])
    def save_text(body: dict):
        """画面のコードブロック・文書を成果物として保存する。parent を渡すと新しい版になる。"""
        text = str(body.get("text", ""))
        if not text.strip():
            raise HTTPException(400, "内容が空です")
        if len(text) > 2_000_000:
            raise HTTPException(413, "大きすぎます")
        ext = str(body.get("ext", ".md")).lower()
        if ext not in TEXT_EXTS:
            raise HTTPException(400, f"拡張子は {sorted(TEXT_EXTS)} のいずれかです")
        parent = body.get("parent") or None
        if parent and not _exists(parent):
            raise HTTPException(400, f"{parent} がありません")
        env = service.runtime.env.id if service.runtime else "upload"
        conf = service.runtime.conf.id if service.runtime else "upload"
        a = service.library.add_text(text, suffix=ext, env_id=env, configuration=conf,
                                     title=str(body.get("title", ""))[:100] or f"文書{ext}", parent=parent,
                                     generation={"source": "conversation"})
        return art_json(a)

    @app.get("/files/{ref}", dependencies=[Depends(auth)])
    def file(ref: str):
        try:
            a = service.library.get(ref)
            return FileResponse(service.library.path(a), media_type=a.mime)
        except (KeyError, ValueError) as e:
            raise HTTPException(404, str(e)) from e

    # ------------------------------------------------------------ ジョブ
    @app.get("/api/jobs", dependencies=[Depends(auth)])
    def jobs():
        return {"jobs": [{**j.__dict__, "label": j.label} for j in service.jobs.list()[:100]]}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
    def job(job_id: str):
        try:
            j = service.jobs.get(job_id)
        except KeyError as e:
            raise HTTPException(404, "ジョブがありません") from e
        return {**j.__dict__, "label": j.label}

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(job_id: str):
        try:
            j = await service.jobs.cancel(job_id)
        except KeyError as e:
            raise HTTPException(404, "ジョブがありません") from e
        return {"id": j.id, "state": j.state, "cancel_requested": j.cancel_requested}

    # ------------------------------------------------------------ OpenAI 互換
    @app.get("/v1/models", dependencies=[Depends(auth)])
    def models():
        data = []
        if service.runtime:
            data.append({"id": f"lao-{service.runtime.env.id}", "object": "model", "owned_by": "local"})
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions", dependencies=[Depends(auth)])
    async def chat(body: dict):
        orch = runtime_or_409()
        want = body.get("model", "")
        active = f"lao-{service.runtime.env.id}"
        if want and want != active:
            # 勝手に環境を切り替えない（R07）
            raise HTTPException(409, f"選択中の環境は {active} です。{want} を使うには画面で環境を切り替えてください")
        messages, attachments = ingest_messages(service, body.get("messages", []))
        attachments += [r for r in body.get("attachments", []) if isinstance(r, str)]
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 2048)
        stream = bool(body.get("stream"))
        cid = f"chatcmpl-{uuid.uuid4().hex[:16]}"

        async def events():
            async for ev in orch.respond(messages, attachments, max_tokens=max_tokens):
                yield ev

        if not stream:
            text_parts, refs, error = [], [], None
            async for ev in events():
                if ev.type == "text":
                    text_parts.append(ev.text)
                elif ev.type == "artifacts":
                    refs += ev.data["refs"]
                elif ev.type == "error":
                    error = ev.text
            content = error or "".join(text_parts)
            content += artifact_markdown(service, refs, key)
            return JSONResponse(completion(cid, active, content))

        async def sse():
            async for ev in events():
                if ev.type == "progress":
                    piece = f"> {ev.text}\n\n"
                elif ev.type == "artifacts":
                    piece = artifact_markdown(service, ev.data["refs"], key) + "\n\n"
                elif ev.type == "error":
                    piece = f"**{ev.text}**"
                else:
                    piece = ev.text
                yield f"data: {json.dumps(chunk(cid, active, piece), ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(chunk(cid, active, '', finish=True))}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    return app


def ingest_messages(service: Service, messages: list[dict]) -> tuple[list[dict], list[str]]:
    """data URI の画像・音声を成果物として取り込み、文中では id@版 で参照させる。"""
    out, refs = [], []
    env = service.runtime.env.id if service.runtime else "upload"
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            out.append({"role": m.get("role", "user"), "content": c or ""})
            continue
        texts = []
        for part in c:
            t = part.get("type")
            if t == "text":
                texts.append(part.get("text", ""))
                continue
            data_uri, mime = None, None
            if t == "image_url":
                data_uri = part.get("image_url", {}).get("url", "")
            elif t == "input_audio":
                a = part.get("input_audio", {})
                mime = f"audio/{a.get('format', 'wav')}"
                data_uri = f"data:{mime};base64,{a.get('data', '')}"
            if not data_uri or not data_uri.startswith("data:"):
                texts.append("[外部URLの添付は読み込みません]")
                continue
            head, _, b64 = data_uri.partition(",")
            mime = head[5:].split(";")[0]
            suffix = DATA_URI_MIME.get(mime)
            if not suffix:
                texts.append(f"[非対応の形式 {mime}]")
                continue
            try:
                raw = base64.b64decode(b64, validate=True)
            except binascii.Error:
                texts.append("[壊れた添付]")
                continue
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / f"attach{suffix}"
                p.write_bytes(raw)
                art = service.library.add_file(p, env_id=env, configuration="upload", title=f"添付{suffix}", move=True)
            refs.append(art.ref)
            texts.append(f"[添付 {art.ref}]")
        out.append({"role": m.get("role", "user"), "content": "\n".join(texts)})
    return out, refs


def artifact_markdown(service: Service, refs: list[str], key: str) -> str:
    lines = []
    for r in refs:
        a = service.library.get(r)
        url = f"/files/{a.ref}?key={key}"
        if a.kind == "image":
            lines.append(f"\n![{a.title}]({url})")
        else:
            lines.append(f"\n[{a.kind}：{a.title}（{a.ref}）]({url})")
    return "".join(lines)


def completion(cid: str, model: str, content: str) -> dict:
    return {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


def chunk(cid: str, model: str, piece: str, finish: bool = False) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": {} if finish else {"content": piece},
                         "finish_reason": "stop" if finish else None}]}
