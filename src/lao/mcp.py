"""別の PC の Claude Code から lao を使うための MCP サーバー（lao mcp）。

標準入出力で MCP（JSON-RPC）を話し、同じ PC で動いている lao serve に 127.0.0.1 で中継する。
別の PC からは SSH 越しに `ssh spark lao mcp` として起動するので、LAN に新しい口は開けない。
GPU・メモリ・推論ロックは lao serve が一手に持つので、画面で作業中の依頼とも順番に動く。

呼ぶ側（Claude）のトークンを節約するため、返すのは成果物の場所と短い要約だけにする。
画像を見たいときは view で縮小版を、中身の確認は describe_media でこの PC の AI に任せる。
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO

import httpx

from .tools import TOOLS
from .usage import IMAGE_TOKENS, VIDEO_FRAMES, category, est_tokens, thumb_tokens

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_WAIT = 240  # 生成ツールで結果を待つ既定の秒数。超えたらジョブ番号を返す
MAX_WAIT = 600

INSTRUCTIONS = (
    "lao は LAN 内の別の PC で動くローカル AI です。画像・動画・音声の生成と、"
    "下請けの文章作業をこの PC で行い、Claude のトークンを節約します。\n"
    "- 生成の結果は成果物の ref（id@版）と保存場所だけを返します。仕上がりを見る必要があるときだけ view で縮小版を見てください。\n"
    "- 画像・動画が指示どおりかの確認は、まず describe_media（この PC の AI が見て文章で答える）を使ってください。\n"
    "- 長い文書の要約・下書き・翻訳・画像用の英語プロンプト作りは ask_local に任せられます。"
    "メイン PC のファイルは自分で読まずに Spark の受け取り箱へ送り（scp）、ask_local の inbox に名前を渡してください。\n"
    "- 画像の案出しは generate_image の count で何枚か作り、view の refs で並べた 1 枚を見てください。"
    "決まった絵柄は save_style で保存し、style で呼べます。\n"
    "- 環境（E1 推論、E2 画像、E3 音声、E4 動画）は、道具に合わせて自動で切り替わります。"
    "切り替えにはモデルの入れ替えで時間がかかり、その分も生成時間に含まれます。\n"
    "- ask_local は今の環境の AI が答えます（答えたモデル名を末尾に付けます）。"
    "大事な文章や難しい作業は best=true で E1 の大きいモデルに頼んでください。"
    "生成の合間の短いプロンプト作りは best を付けないほうが、モデルの入れ替えが起きず速いです。\n"
    "- 時間がかかると job=… が返ります。job で待ってください（何度も短く問い合わせないでください）。"
)

_WAIT = {"type": "integer", "minimum": 0, "maximum": MAX_WAIT,
         "description": f"結果を待つ最長秒数（既定 {DEFAULT_WAIT}）。0 ならすぐジョブ番号を返す"}


def _from_lao(name: str, description: str) -> dict:
    params = json.loads(json.dumps(TOOLS[name].schema()["function"]["parameters"]))
    params["properties"]["wait_seconds"] = _WAIT
    return {"name": name, "description": description, "inputSchema": params}


TOOL_DEFS = [
    {"name": "status", "description": "選ばれている環境、使える道具、実行中のジョブを短く返す。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "switch_environment",
     "description": "環境と構成を選ぶ（e1 推論・e2 画像・e3 音声・e4 動画）。通常は道具に合わせて自動で切り替わるので、構成を指定したいときだけ使う。",
     "inputSchema": {"type": "object", "properties": {
         "env": {"type": "string", "enum": ["e1", "e2", "e3", "e4"]},
         "configuration": {"type": "string", "description": "構成の id（省略時は先頭）"},
         "preload": {"type": "boolean", "description": "true ならモデルを先に読み込んでおく（次の依頼の待ちを減らす）"}},
         "required": ["env"], "additionalProperties": False}},
    _from_lao("generate_image", "文章から画像を作る（E2）。返すのは ref と保存場所。"),
    _from_lao("edit_image", "画像を指示どおりに直し、新しい版にする（E2）。"),
    _from_lao("generate_video", "画像を元に短い動画を作る（E4）。数分かかることがある。"),
    _from_lao("generate_speech", "文章を読み上げた音声を作る（E3）。"),
    _from_lao("describe_media",
              "画像・音声・動画をこの PC の AI に見せ、問いへの答えを文章で返す。仕上がりの確認に使う。"),
    {"name": "patterns", "description": "run_recipe で使えるパターン（生成モデルごとに用意した ComfyUI の処理：拡大、顔と手の補正、"
                                       "参考画像のポーズで描く など）の一覧と、それぞれの値。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "run_recipe",
     "description": "パターンを最大 6 手まで順に使う（E2）。前の手の出力が次の手の画像入力になり、途中の画像も残る。"
                    "使えるパターンと値は patterns で確かめる。1 回で済むなら generate_image・edit_image を使う。",
     "inputSchema": {"type": "object", "properties": {
         "steps": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
             "pattern": {"type": "string"}, "prompt": {"type": "string"}, "negative_prompt": {"type": "string"},
             "aspect": {"type": "string"}, "image_ref": {"type": "string", "description": "この手の画像入力（id@版）"},
             "seed": {"type": "integer"}}, "required": ["pattern"],
             "additionalProperties": True}, "description": "手順。パターン固有の値（steps・denoise など）も各手に書ける"},
         "wait_seconds": _WAIT}, "required": ["steps"], "additionalProperties": False}},
    {"name": "job", "description": "ジョブの状態を返す。wait_seconds まで終わるのを待つ。cancel で取り消す。",
     "inputSchema": {"type": "object", "properties": {
         "job_id": {"type": "string"}, "wait_seconds": _WAIT,
         "cancel": {"type": "boolean", "description": "true で取り消す"}},
         "required": ["job_id"], "additionalProperties": False}},
    {"name": "view", "description": "画像・動画の縮小版を見る（動画は 4 コマを並べた 1 枚、refs なら画像を並べた 1 枚）。必要なときだけ使う。",
     "inputSchema": {"type": "object", "properties": {
         "ref": {"type": "string", "description": "成果物（id@版）"},
         "refs": {"type": "array", "items": {"type": "string"}, "maxItems": 16,
                  "description": "並べて見る画像（id@版）。ref の代わりに使う"},
         "size": {"type": "integer", "minimum": 64, "maximum": 1024, "description": "長辺の px（既定 256。並べるときは 1 枚あたり）"}},
         "additionalProperties": False}},
    {"name": "ask_local",
     "description": "この PC の AI に下請けを頼み、結果の文章だけを受け取る（要約・下書き・翻訳・プロンプト作り）。",
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string", "description": "頼むこと（出力の形と長さも書く）"},
         "text": {"type": "string", "description": "素材の文章"},
         "refs": {"type": "array", "items": {"type": "string"}, "description": "素材にする成果物（id@版、文書・テキスト）"},
         "inbox": {"type": "array", "items": {"type": "string"},
                   "description": "素材にする受け取り箱のファイル名・フォルダ名（取り込んでから読む。PDF・Word・Excel・PowerPoint も可）"},
         "max_tokens": {"type": "integer", "minimum": 64, "maximum": 8192},
         "best": {"type": "boolean",
                  "description": "true なら E1 の大きい推論モデルで答える（切り替えでモデルを入れ替えるので遅い）。"
                                 "false なら今の環境の AI で答える（生成の合間は小さいモデルのことがある）"}},
         "required": ["task"], "additionalProperties": False}},
    {"name": "import_file",
     "description": "受け取り箱（lao の root/inbox）に送ったファイルかフォルダを成果物に取り込み、ref を返す。",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "ファイル名かフォルダ名"}},
                     "required": ["name"], "additionalProperties": False}},
    {"name": "inbox", "description": "受け取り箱にあるもの（まだ取り込んでいないファイル・フォルダ）と、箱の場所を返す。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "make_clip",
     "description": "画像から動画（E4）を作り、読み上げ音声（E3）を作って重ね、1 本の mp4 にする。"
                    "動画が待ち時間内に終わらなければ job を返すので、終わったら video_ref を付けてもう一度呼ぶ。",
     "inputSchema": {"type": "object", "properties": {
         "image_ref": {"type": "string", "description": "元にする画像（id@版）"},
         "prompt": {"type": "string", "description": "動きや場面の説明（英語推奨）"},
         "seconds": {"type": "number", "minimum": 1, "maximum": 20},
         "narration": {"type": "string", "description": "読み上げる文章（省略すると音声を重ねない）"},
         "voice_ref": {"type": "string", "description": "読み上げの参考音声（id@版、本人の同意がある声に限る）"},
         "mix": {"type": "boolean", "description": "true なら動画の元の音と混ぜる（既定は置き換え）"},
         "video_ref": {"type": "string", "description": "作り終えた動画（id@版）。指定すると動画の生成を飛ばす"},
         "wait_seconds": _WAIT},
         "additionalProperties": False}},
    {"name": "combine", "description": "動画をつなぐ、または動画に音声を重ねる（GPU を使わない）。",
     "inputSchema": {"type": "object", "properties": {
         "refs": {"type": "array", "items": {"type": "string"}, "description": "つなぐ動画（順に）と、重ねる音声 0〜1 本"},
         "mix": {"type": "boolean", "description": "true なら元の音と混ぜる"},
         "title": {"type": "string"}},
         "required": ["refs"], "additionalProperties": False}},
    {"name": "styles", "description": "保存した画像のスタイル（名前・前置き・避けたい内容・縦横比）の一覧。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "save_style",
     "description": "画像のスタイルを名前で保存する（同じ名前なら上書き）。generate_image の style で呼べる。delete で消す。",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "prompt": {"type": "string", "description": "前置き（英語推奨）。{prompt} を含めばそこに依頼文が入る"},
         "negative": {"type": "string", "description": "避けたい内容"},
         "aspect": {"type": "string", "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]},
         "note": {"type": "string", "description": "用途のメモ"},
         "delete": {"type": "boolean"}},
         "required": ["name"], "additionalProperties": False}},
    {"name": "health",
     "description": "Spark の健康状態：C・D の空き、GPU の温度、読み込み中のモデル、直近の失敗。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "cleanup",
     "description": "作業の残り・古いジョブ記録・古いログ・古いバックアップを片づける。既定は一覧だけ（execute で消す）。成果物は消さない。",
     "inputSchema": {"type": "object", "properties": {
         "execute": {"type": "boolean"}, "days": {"type": "integer", "minimum": 1, "maximum": 3650,
                                                  "description": "これより古い記録を対象にする（既定 30 日）"}},
         "additionalProperties": False}},
    {"name": "backup", "description": "会話・プロジェクト・記憶・設定・スキル・スタイルを zip にまとめ、保存場所を返す。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "unload", "description": "読み込んだモデルを今すぐ下ろす（熱と電気を抑える。次の依頼で読み直す）。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "usage",
     "description": "Claude のトークンの記録：lao に任せた回数と節約の見積もり、メイン PC の Claude Code が実際に使った量（月ごと）。",
     "inputSchema": {"type": "object", "properties": {
         "month": {"type": "string", "description": "YYYY-MM（省略すると今月）"}}, "additionalProperties": False}},
    {"name": "update",
     "description": "Spark の lao を最新にする（git pull → 必要なら依存を入れる → lao serve を再起動 → 動かなければ元の版に戻す）。"
                    "既定は確認だけ。execute で行う。",
     "inputSchema": {"type": "object", "properties": {"execute": {"type": "boolean"}}, "additionalProperties": False}},
    {"name": "list_artifacts", "description": "最近の成果物を新しい順に返す。",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["image", "video", "audio", "text"]},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
         "additionalProperties": False}},
]
GENERATORS = {"generate_image", "edit_image", "generate_video", "generate_speech", "describe_media", "run_recipe"}
# 道具が今の環境に無いときに切り替える先
HOME_ENV = {"generate_image": "e2", "edit_image": "e2", "run_recipe": "e2", "generate_video": "e4", "generate_speech": "e3",
            "describe_media": "e1"}
TERMINAL_STATES = ("done", "cancelled", "failed", "interrupted")


class LaoError(Exception):
    pass


class Bridge:
    """MCP の道具呼び出しを lao serve の API に置き換える。"""

    _offloaded = 0  # 今の呼び出しで Spark が代わりに読んだトークン（見積もり）

    def __init__(self, client: httpx.Client, machine=None):
        self.http = client
        self.machine = machine

    def _req(self, method: str, url: str, timeout: float = 60, **kw) -> httpx.Response:
        try:
            r = self.http.request(method, url, timeout=timeout, **kw)
        except httpx.ConnectError as e:
            raise LaoError("lao serve に接続できません。この PC で lao serve を起動してください") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise LaoError(str(detail)[:1000])
        return r

    def call(self, name: str, args: dict) -> list[dict]:
        t0 = time.monotonic()
        self._offloaded = 0
        ok, out = False, []
        try:
            fn: Callable[[dict], Any] | None = getattr(self, f"t_{name}", None)
            if name in GENERATORS:
                out = _text(self.run(name, args))
            elif fn is None:
                raise LaoError(f"道具 {name} はありません")
            else:
                res = fn(args)
                out = res if isinstance(res, list) else _text(res)
            ok = True
            return out
        finally:
            self._record(name, args, out, ok, time.monotonic() - t0)

    def _record(self, name: str, args: dict, out: list[dict], ok: bool, seconds: float) -> None:
        """呼び出しごとに、Claude に返した量と、Spark が代わりに読んだ量の見積もりを残す（失敗しても道具は止めない）。"""
        if name == "usage":
            return
        returned = sum(est_tokens(c.get("text", "")) for c in out if c.get("type") == "text")
        returned += sum(_image_tokens(c) for c in out if c.get("type") == "image")
        kind = ""
        offloaded = self._offloaded
        if name == "describe_media" and args.get("ref"):
            try:
                kind = self._req("GET", f"/api/artifact/{args['ref']}").json()["kind"]
            except LaoError:
                kind = ""
            offloaded = {"image": IMAGE_TOKENS, "video": IMAGE_TOKENS * VIDEO_FRAMES}.get(kind, 0)
        cat = category(name, kind)
        rec = {"tool": name, "category": cat, "ok": ok, "seconds": seconds,
               "sent_tokens": est_tokens(json.dumps(args, ensure_ascii=False)), "returned_tokens": returned,
               "offloaded_tokens": offloaded,
               "saved_tokens": max(0, offloaded - returned) if ok and cat == "saved" else 0}
        try:
            self._req("POST", "/api/usage", json=rec, timeout=10)
        except (LaoError, httpx.HTTPError):
            pass

    # ------------------------------------------------------------ 道具
    def t_status(self, _args: dict) -> str:
        st = self._req("GET", "/api/status").json()
        jobs = self._req("GET", "/api/jobs").json()["jobs"]
        running = [j for j in jobs if j["state"] not in TERMINAL_STATES]
        if not st.get("environment"):
            head = "環境：未選択（道具を使うと自動で選ばれます）"
        else:
            env = st["environment"]
            env_id = env.get("id", env) if isinstance(env, dict) else env
            conf = st.get("configuration", {})
            conf_id = conf.get("id", conf) if isinstance(conf, dict) else conf
            tools = ", ".join(st.get("tools", [])) or "なし"
            head = f"環境：{env_id}（{conf_id}）　使える道具：{tools}"
        lines = [head, f"実行中のジョブ：{len(running)} 件"]
        lines += [f"- job={j['id']} {j['kind']} {j['label']}" for j in running[:5]]
        try:
            lines.append(self._req("GET", "/api/health").json()["summary"])
        except LaoError:
            pass
        return "\n".join(lines)

    def t_switch_environment(self, args: dict) -> str:
        body = {"env": args["env"], "preload": bool(args.get("preload"))}
        if args.get("configuration"):
            body["configuration"] = args["configuration"]
        self._req("POST", "/api/environment", json=body, timeout=900)
        return f"{args['env']} に切り替えました。\n" + self.t_status({})

    def run(self, name: str, args: dict) -> str:
        args = dict(args)
        wait = _clamp_wait(args.pop("wait_seconds", DEFAULT_WAIT))
        t0 = time.monotonic()
        switched = self._ensure_env(name)
        j = self._req("POST", f"/api/run/{name}", json=args).json()
        if wait:
            j = self._job(j["id"], wait)
        out = format_job(j)
        if switched:
            out += f"\n（{switched} に切り替えて実行。所要 {time.monotonic() - t0:.0f} 秒、切り替えを含む）"
        return out

    def _ensure_env(self, name: str | None) -> str:
        """道具が今の環境に無ければ、その道具の環境へ切り替える。切り替えた環境を返す。

        別の PC からの依頼では、切り替えの待ち時間を生成時間の一部として扱う（利用者の指示）。
        実行中のジョブがあれば、終わるのを待ってから切り替える。
        """
        st = self._req("GET", "/api/status").json()
        if st.get("environment") and (name is None or name in st.get("tools", [])):
            return ""
        return self._switch_to(HOME_ENV.get(name or "", "e1"))

    def _switch_to(self, env: str) -> str:
        """env へ切り替える（同じ環境なら何もしない）。実行中のジョブがあれば終わるのを待つ。切り替えた環境を返す。"""
        if (self._req("GET", "/api/status").json().get("environment") or {}).get("id") == env:
            return ""
        for j in self._req("GET", "/api/jobs").json()["jobs"]:
            if j["state"] not in TERMINAL_STATES:
                self._job(j["id"], MAX_WAIT)
        self._req("POST", "/api/environment", json={"env": env}, timeout=900)
        return env

    def t_job(self, args: dict) -> str:
        if args.get("cancel"):
            self._req("POST", f"/api/jobs/{args['job_id']}/cancel")
        return format_job(self._job(args["job_id"], _clamp_wait(args.get("wait_seconds", DEFAULT_WAIT))))

    def _job(self, job_id: str, wait: float) -> dict:
        return self._req("GET", f"/api/jobs/{job_id}", params={"wait": wait}, timeout=wait + 30).json()

    def t_view(self, args: dict) -> list[dict]:
        if args.get("refs"):
            r = self._req("GET", "/api/sheet", params={"refs": ",".join(args["refs"]),
                                                       "size": int(args.get("size") or 256)})
        elif args.get("ref"):
            r = self._req("GET", f"/api/thumb/{args['ref']}", params={"size": int(args.get("size") or 256)})
        else:
            raise LaoError("ref か refs を指定してください")
        return [{"type": "image", "data": base64.b64encode(r.content).decode(), "mimeType": "image/jpeg"}]

    def ask_local_best(self) -> bool:
        return bool(self.machine and self.machine.raw.get("server", {}).get("ask_local_best", False))

    def t_ask_local(self, args: dict) -> str:
        args = dict(args)
        best = args.pop("best", None)
        if best if best is not None else self.ask_local_best():
            self._switch_to("e1")  # 大きい推論モデルで答える
        else:
            self._ensure_env(None)  # どの環境の AI でもよい。未選択のときだけ E1 を選ぶ
        r = self._req("POST", "/api/delegate", json=args, timeout=3600).json()
        # 下請けの素材のうち、頼む側が書いて送った text は Claude がもう読んでいるので、節約には数えない
        self._offloaded = max(0, int(r.get("material_tokens", 0)) - est_tokens(args.get("text", "")))
        notes = []
        if r.get("refs"):
            notes.append("取り込んだ資料：" + " ".join(r["refs"]))
        if r.get("pieces", 1) > 1:
            notes.append(f"（長いので {r['pieces']} 区切りに分けて読みました）")
        if r.get("model"):
            notes.append(f"（答えた AI：{r['model']}、{r.get('environment', '')}）")
        return r["text"] + ("\n\n" + "\n".join(notes) if notes else "")

    def t_import_file(self, args: dict) -> str:
        a = self._req("POST", "/api/import", json={"name": args["name"]}).json()
        items = a.get("items") or [a]
        if len(items) == 1:
            return f"取り込みました：{a['ref']} {a['kind']} {a['title']}"
        return f"{len(items)} 個取り込みました：\n" + "\n".join(f"- {x['ref']} {x['kind']} {x['title']}" for x in items)

    def t_inbox(self, _args: dict) -> str:
        r = self._req("GET", "/api/inbox").json()
        if not r["items"]:
            return f"受け取り箱（{r['path']}）は空です"
        return f"受け取り箱（{r['path']}）：\n" + "\n".join(
            f"- {x['name']}{'/' if x['folder'] else ''} {x['bytes'] / 1024**2:.1f}MB" for x in r["items"])

    def t_list_artifacts(self, args: dict) -> str:
        params = {"kind": args["kind"]} if args.get("kind") else {}
        items = self._req("GET", "/api/library", params=params).json()["items"]
        items = items[: int(args.get("limit") or 10)]
        if not items:
            return "成果物はありません"
        return "\n".join(f"- {a['ref']} {a['kind']} {a['title'][:50]}" for a in items)

    def t_make_clip(self, args: dict) -> str:
        wait = _clamp_wait(args.get("wait_seconds", DEFAULT_WAIT))
        video = args.get("video_ref")
        if not video:
            if not args.get("image_ref") or not args.get("prompt") or not args.get("seconds"):
                raise LaoError("image_ref・prompt・seconds を指定してください（作り終えた動画があれば video_ref）")
            out = self.run("generate_video", {"image_ref": args["image_ref"], "prompt": args["prompt"],
                                              "seconds": args["seconds"], "wait_seconds": wait})
            refs = REF_IN_TEXT.findall(out)
            if "job=" in out and not refs:
                return out + "\n動画が終わったら、job の結果の ref を video_ref に入れて make_clip をもう一度呼んでください"
            if not refs:
                return out
            video = refs[0]
        if not args.get("narration"):
            return f"動画：{video}（読み上げは指定がないので重ねていません）"
        speech_args = {"text": args["narration"], "wait_seconds": MAX_WAIT}
        if args.get("voice_ref"):
            speech_args["reference_ref"] = args["voice_ref"]
        out = self.run("generate_speech", speech_args)
        voice = REF_IN_TEXT.findall(out)
        if not voice:
            return f"動画：{video}\n読み上げ：{out}"
        return self.t_combine({"refs": [video, voice[0]], "mix": bool(args.get("mix"))})

    def t_combine(self, args: dict) -> str:
        a = self._req("POST", "/api/combine", json=args, timeout=1800).json()
        return f"できました：{a['ref']} video {a['path']} seconds={a['seconds']} has_audio={a['has_audio']}"

    def t_patterns(self, _args: dict) -> str:
        self._ensure_env("run_recipe")
        items = self._req("GET", "/api/patterns").json()["patterns"]
        if not items:
            return "使えるパターンはありません（Spark で lao pattern new／approve で用意します）"
        out = []
        for p in items:
            params = "、".join(f"{k}（{v.get('minimum', '')}〜{v.get('maximum', '')}）" if v["type"] != "string" else k
                              for k, v in p["params"].items())
            out.append(f"- {p['id']}：{p['does']}（要るもの：{p['needs']}。出力：{p['output']}）"
                       + (f" 値：{params}" if params else "") + (f" 使わない：{p['avoid']}" if p["avoid"] else ""))
        return "\n".join(out)

    def t_styles(self, _args: dict) -> str:
        items = self._req("GET", "/api/image-styles").json()["styles"]
        if not items:
            return "保存したスタイルはありません（save_style で保存できます）"
        return "\n".join(f"- {x['name']}：{x['prompt'][:80]}" + (f"（{x['aspect']}）" if x["aspect"] else "")
                         + (f" ※{x['note']}" if x["note"] else "") for x in items)

    def t_save_style(self, args: dict) -> str:
        if args.get("delete"):
            self._req("DELETE", f"/api/image-styles/{args['name']}")
            return f"スタイル「{args['name']}」を消しました"
        st = self._req("POST", "/api/image-styles", json={k: v for k, v in args.items() if k != "delete"}).json()
        return f"スタイル「{st['name']}」を保存しました（generate_image の style=\"{st['name']}\" で使えます）"

    def t_health(self, _args: dict) -> str:
        h = self._req("GET", "/api/health").json()
        lines = [h["summary"]]
        if h.get("gpu") is None:
            lines.append("GPU：nvidia-smi が見つからないため読めません")
        lines += [f"失敗：job={f['id']} {f['kind']} {f['error'][:120]}" for f in h.get("failed_recent", [])]
        lines.append(f"使わないモデルは {h['idle_unload_minutes']:.0f} 分で下ろします" if h["idle_unload_minutes"]
                     else "使わないモデルを自動で下ろす設定は切ってあります")
        return "\n".join(lines)

    def t_cleanup(self, args: dict) -> str:
        r = self._req("POST", "/api/cleanup", json={"execute": bool(args.get("execute")),
                                                    "days": args.get("days") or 30}).json()
        if not r["by_kind"]:
            return "片づけるものはありません"
        mb = lambda b: f"{b / 1024**2:.0f}MB"  # noqa: E731
        lines = [f"- {k}：{v['count']} 個 {mb(v['bytes'])}" for k, v in r["by_kind"].items()]
        head = (f"片づけました（{mb(r['removed_bytes'])}）" if r["executed"]
                else f"片づけられるもの（合計 {mb(r['total_bytes'])}。execute=true で消します。成果物は対象外）")
        return head + "\n" + "\n".join(lines)

    def t_backup(self, _args: dict) -> str:
        r = self._req("POST", "/api/backup", timeout=600).json()
        return f"バックアップ：{r['path']}（{r['bytes'] / 1024**2:.1f}MB）"

    def t_unload(self, _args: dict) -> str:
        r = self._req("POST", "/api/unload", timeout=300).json()
        return "下ろしました：" + (", ".join(r["unloaded"]) or "（読み込んでいるモデルはありませんでした）")

    def t_usage(self, args: dict) -> str:
        return self._req("GET", "/api/usage", params={"month": args.get("month", "")}).json()["summary"]

    def t_update(self, args: dict) -> str:
        from . import updater
        if not args.get("execute"):
            return updater.plan_text()
        log = updater.start_detached(self.machine)
        return (f"更新を始めました。記録：{log}\n"
                "1〜2 分後に status で確かめてください。新しい道具は Claude Code で lao をつなぎ直すと使えます（/mcp）")


REF_IN_TEXT = re.compile(r"\b[0-9a-f]{12}@\d+\b")


def _image_tokens(part: dict) -> int:
    """返した JPEG の大きさから、Claude がそれを見るトークンを見積もる。"""
    try:
        data = base64.b64decode(part.get("data", ""))
        i = 2
        while i < len(data) - 9:  # JPEG の SOF から幅と高さを読む
            if data[i] != 0xFF:
                break
            marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xC3:
                h, w = int.from_bytes(data[i + 5:i + 7], "big"), int.from_bytes(data[i + 7:i + 9], "big")
                return thumb_tokens(w, h)
            i += 2 + length
    except (ValueError, IndexError):
        pass
    return IMAGE_TOKENS


def _clamp_wait(v: Any) -> int:
    try:
        return max(0, min(int(v), MAX_WAIT))
    except (TypeError, ValueError):
        return DEFAULT_WAIT


def _text(s: str) -> list[dict]:
    return [{"type": "text", "text": s}]


def format_job(j: dict) -> str:
    """ジョブを数行にまとめる。成果物は ref・種類・保存場所と、生成条件の要点だけ。"""
    state = j["state"]
    head = f"{j['label']} job={j['id']} {j['kind']}"
    if state not in TERMINAL_STATES:
        pct = f" {j['progress'] * 100:.0f}%" if j.get("progress") is not None else ""
        return f"{head}{pct}（終わっていません。job ツールで待てます）"
    lines = [head]
    if j.get("error"):
        lines.append(f"エラー：{j['error'][:500]}")
    for a in j.get("items", []):
        g = a.get("generation") or {}
        bits = [f"{k}={g[k]}" for k in ("seed", "width", "height", "actual_seconds", "has_audio") if k in g]
        lines.append(f"- {a['ref']} {a['kind']} {a.get('path', '')} {' '.join(bits)}".rstrip())
    if j.get("message") and state == "done":
        lines.append(j["message"])
    return "\n".join(lines)


# ------------------------------------------------------------------ JSON-RPC（標準入出力）
def handle(bridge: Bridge, msg: dict) -> dict | None:
    """1 通を処理して返事を返す。通知（id なし）には返事をしない。"""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    try:
        if method == "initialize":
            want = (msg.get("params") or {}).get("protocolVersion")
            result: dict = {"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "lao", "version": "0.1.0"},
                            "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOL_DEFS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                result = {"content": bridge.call(str(p.get("name")), dict(p.get("arguments") or {})),
                          "isError": False}
            except (LaoError, KeyError) as e:
                result = {"content": _text(f"できませんでした：{e}"), "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"{method} はありません"}}
    except Exception as e:  # noqa: BLE001 — 予期しない失敗も返事にして、接続は切らない
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)[:500]}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve_stdio(bridge: Bridge, stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON ではありません"}}
        else:
            reply = handle(bridge, msg) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def server_key(machine) -> str:
    """lao serve の API キー。設定に無ければ、起動時に root/server.key へ書いたものを使う。"""
    key = machine["server"].get("api_key") or ""
    if key:
        return key
    p = Path(machine.path_of("root")) / "server.key"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise LaoError(f"API キーが見つかりません（{p}）。先に lao serve を起動してください") from e


def run(machine, url: str | None = None, check: bool = False) -> int:
    import io

    url = url or f"http://127.0.0.1:{int(machine['server']['port'])}"
    try:
        key = server_key(machine)
    except LaoError as e:
        print(e, file=sys.stderr)
        return 1
    client = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {key}"}, trust_env=False)
    bridge = Bridge(client, machine)
    if check:
        try:
            print(bridge.t_status({}))
        except LaoError as e:
            print(f"NG：{e}", file=sys.stderr)
            return 1
        print("OK：lao serve に接続できました")
        return 0
    # Windows でも文字化けしないよう、入出力は UTF-8 で扱う
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    serve_stdio(bridge, stdin, stdout)
    return 0
