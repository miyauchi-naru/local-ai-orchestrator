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
from importlib import resources
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from .library import Artifact
from .service import Service

MAX_UPLOAD_BYTES = 4 * 1024**3
DATA_URI_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "audio/wav": ".wav",
                 "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "video/mp4": ".mp4"}


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
    @app.get("/", response_class=HTMLResponse)
    def index():
        return resources.files("lao").joinpath("ui.html").read_text(encoding="utf-8")

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
            if suffix in (".mp4", ".mov", ".mkv", ".webm", ".wav", ".mp3", ".m4a", ".flac"):
                try:
                    service.media.probe(tmp)
                except ValueError as e:
                    raise HTTPException(400, str(e)) from e
            art = service.library.add_file(tmp, env_id=env, configuration="upload",
                                           title=Path(file.filename or "file").name, move=True)
        return art_json(art)

    @app.get("/api/library", dependencies=[Depends(auth)])
    def library(kind: str | None = None, env: str | None = None):
        return {"items": [art_json(a) for a in service.library.list(kind, env)]}

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
