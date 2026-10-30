"""組み合わせ評価（lao p0 pairs）：会話モデル＋生成モデルの組み合わせを、狙いどおりに作れるかとメモリで比べる。

会話モデルが lao のツール（generate_image・edit_image・generate_video）を通して生成モデルを使い、
依頼の意図をどれだけ再現できるかを測る。見るのは次の 5 つ。

  初回の再現度  … 日本語の依頼から 1 回で、確認項目をいくつ満たした絵になったか
  引数の選び方  … 壊れていない呼び出し、縦横比、避けたい内容、修正と新規の使い分け
  見て直す力    … 出来た画像を見せ、依頼と違う所に気づいて直せたか（足りなかった項目をいくつ取り戻したか）
  修正指示      … 「ここを変えて」に edit_image で応え、変えた所と残すべき所が守られたか
  動画          … 画像を作って動画にする依頼で、動きの指定が守られたか（動画の部品がある組み合わせだけ）

あわせて、組み合わせごとにピークメモリ・読み直しの回数と時間・1 課題の時間を測り、
予算（と、他のために空けておきたい量）に収まる組み合わせの中から推奨を選ぶ。
判定は生成とは別の段階で、判定用の理解モデル（既定は課題ファイルの judge）が「はい／いいえ」で答える。
メイン PC の Claude などに判定させたいときは、judge-sheet.json の答えを answers.json に書いて採点し直せる。

測定はユーザーの「開始」指示の後、実機で行う。ここでは何もインストールしない。
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import random
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import Component, Configuration, Environment, MachineConfig, load_catalog
from ..image_styles import image_size
from ..jobs import JobManager
from ..library import Library
from ..media import Media, image_part
from ..orchestrator import Orchestrator
from ..runtime import Runtime, default_engine_factory
from .probe import PeakSampler

KEYS = {"chat": "chat", "image": "imagegen", "video": "videogen"}
FIX_PROMPT = ("{ref} は最初の依頼で作った画像です（添付）。依頼と見比べて、違う所や足りない所があれば "
              "generate_image か edit_image で直してください。依頼どおりなら、ツールを使わずに「完成」とだけ答えてください。")
JUDGE_PROMPT = ("次の画像について答えてください。{note}質問：{check}？\n"
                "画像から確かにそう言えるときだけ「はい」、それ以外は「いいえ」と、一語だけで答えてください。")
VIDEO_NOTE = "この画像は動画から等間隔に取った 4 コマを、左から時刻順に並べたものです。"
DEFAULT_WEIGHTS = {"first": 0.35, "args": 0.15, "fix": 0.2, "edit": 0.2, "video": 0.1}


class PairsError(ValueError):
    pass


# ------------------------------------------------------------------ 課題ファイル
@dataclass
class Task:
    id: str
    kind: str  # image | edit | video
    request: str
    checks: list[str]
    expect: dict[str, Any] = field(default_factory=dict)
    followup: str = ""
    followup_checks: list[str] = field(default_factory=list)
    video_checks: list[str] = field(default_factory=list)


@dataclass
class Suite:
    tasks: list[Task]
    chat: list[str]
    image: list[str]
    video: list[str]
    judge: str
    fix_rounds: int = 2
    repeats: int = 1
    seed: int = 1234
    tie_points: float = 3.0
    reserve_gb: float = 0.0
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))


def load_suite(path: Path) -> Suite:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    s, c = data.get("settings", {}), data.get("candidates", {})
    tasks = []
    for t in data.get("tasks", []):
        kind = t.get("kind", "image")
        if kind not in ("image", "edit", "video"):
            raise PairsError(f"{path.name}: 課題 {t.get('id')} の kind は image・edit・video のどれかです")
        if not t.get("checks"):
            raise PairsError(f"{path.name}: 課題 {t.get('id')} に checks がありません")
        if kind == "edit" and not (t.get("followup") and t.get("followup_checks")):
            raise PairsError(f"{path.name}: 修正の課題 {t.get('id')} には followup と followup_checks が要ります")
        if kind == "video" and not t.get("video_checks"):
            raise PairsError(f"{path.name}: 動画の課題 {t.get('id')} には video_checks が要ります")
        tasks.append(Task(t["id"], kind, t["request"], list(t["checks"]), dict(t.get("expect", {})),
                          t.get("followup", ""), list(t.get("followup_checks", [])), list(t.get("video_checks", []))))
    if len({t.id for t in tasks}) != len(tasks):
        raise PairsError(f"{path.name}: 課題の id が重なっています")
    if not c.get("chat") or not c.get("image"):
        raise PairsError(f"{path.name}: [candidates] に chat と image を書いてください")
    weights = dict(DEFAULT_WEIGHTS)
    weights.update({k: float(v) for k, v in s.get("weights", {}).items() if k in DEFAULT_WEIGHTS})
    return Suite(tasks, list(c["chat"]), list(c["image"]), list(c.get("video", [])), c.get("judge", ""),
                 int(s.get("fix_rounds", 2)), int(s.get("repeats", 1)), int(s.get("seed", 1234)),
                 float(s.get("tie_points", 3)), float(s.get("reserve_gb", 0)), weights)


# ------------------------------------------------------------------ 組み合わせ
@dataclass
class Pair:
    chat: str
    image: str
    video: str = ""

    @property
    def id(self) -> str:
        parts = [self.chat, self.image] + ([self.video] if self.video else [])
        return "+".join(p.split(".", 1)[1].replace(".", "-") for p in parts)


def resolve(catalog: dict[str, Environment], ref: str) -> tuple[Environment, Configuration, Component]:
    """「環境.構成.部品」（例：e2.e2-standard.chat）をカタログから引く。"""
    try:
        env_id, conf_id, key = ref.split(".")
        env = catalog[env_id]
        conf = env.configurations[conf_id]
        return env, conf, conf.components[key]
    except (ValueError, KeyError) as e:
        raise PairsError(f"カタログに {ref} がありません（環境.構成.部品 の形で書きます）") from e


def pairs_of(suite: Suite, catalog: dict[str, Environment], only: str = "") -> list[Pair]:
    out = []
    for c in suite.chat:
        for i in suite.image:
            for v in suite.video or [""]:
                p = Pair(c, i, v)
                for ref, role in ((c, "chat"), (i, "image_gen"), (v, "video_gen")):
                    if ref and resolve(catalog, ref)[2].role != role:
                        raise PairsError(f"{ref} は {role} の部品ではありません")
                if not only or only in p.id:
                    out.append(p)
    return out


def build(machine: MachineConfig, catalog: dict[str, Environment], pair: Pair):
    """組み合わせ用の構成を作る。部品名は chat・imagegen・videogen にそろえ、元の場所を覚えておく。"""
    comps: dict[str, Component] = {}
    origin: dict[str, tuple[Environment, Configuration, Component]] = {}
    for role, ref in (("chat", pair.chat), ("image", pair.image), ("video", pair.video)):
        if not ref:
            continue
        env, conf, c = resolve(catalog, ref)
        m = machine.measured(env.id, c.key)
        copy = dataclasses.replace(c, key=KEYS[role])
        if m and "peak_gb" in m:  # 実測があれば見積もりより優先する
            copy = dataclasses.replace(copy, weights_gb=float(m["peak_gb"]), overhead_gb=0.0)
        comps[KEYS[role]] = copy
        origin[KEYS[role]] = (env, conf, c)
    outputs = ["text", "image"] + (["video"] if pair.video else [])
    env = Environment("bench", "画像・動画の生成（組み合わせ評価）", ["text"], outputs)
    conf = Configuration("bench", pair.id, pair.id, "candidate", "chat", comps)
    env.configurations[conf.id] = conf
    return env, conf, origin


def missing_models(machine: MachineConfig, origin: dict) -> list[str]:
    """llama の部品で、モデルを取得していないもの（ComfyUI の部品は ComfyUI 側で確かめる）。"""
    out = []
    for env, conf, c in origin.values():
        if c.engine == "llama" and not (machine.path_of("models") / conf.id / c.key).exists():
            out.append(f"{env.id}.{conf.id}.{c.key}")
    return out


class CountingFactory:
    """元の構成のエンジンを作り、ロードの回数と時間を数える（読み直しが多い＝同居できていない）。"""

    def __init__(self, base, origin: dict):
        self.base, self.origin = base, origin
        self.loads: dict[str, int] = {}
        self.load_seconds = 0.0

    def __call__(self, env, conf, c):
        oenv, oconf, oc = self.origin[c.key]
        eng = self.base(oenv, oconf, oc)
        counter = self

        class Counted:
            @property
            def running(self):
                return eng.running

            async def load(self):
                t0 = time.monotonic()
                await eng.load()
                counter.load_seconds += time.monotonic() - t0
                counter.loads[c.key] = counter.loads.get(c.key, 0) + 1

            async def unload(self):
                await eng.unload()

            def __getattr__(self, name):
                return getattr(eng, name)

        return Counted()

    @property
    def reloads(self) -> int:
        return sum(max(0, n - 1) for n in self.loads.values())


# ------------------------------------------------------------------ 生成
async def _turn(orch: Orchestrator, jobs: JobManager, messages: list[dict]) -> dict:
    before = {j.id for j in jobs.list()}
    text, invalid, errors = "", 0, []
    async for ev in orch.respond(messages, max_tokens=2048):
        if ev.type == "text":
            text += ev.text
        elif ev.type == "progress" and "受け付けませんでした" in ev.text:
            invalid += 1
        elif ev.type == "error":
            errors.append(ev.text)
    new = sorted((j for j in jobs.list() if j.id not in before), key=lambda j: j.created_at)
    calls = [{"tool": j.kind, "args": j.args, "ok": j.state == "done", "refs": list(j.result), "error": j.error}
             for j in new]
    return {"text": text.strip()[:2000], "invalid": invalid, "errors": errors, "calls": calls}


def _last(turn: dict, tools: tuple[str, ...]) -> str:
    for call in reversed(turn["calls"]):
        if call["tool"] in tools and call["ok"] and call["refs"]:
            return call["refs"][-1]
    return ""


def _summary(turn: dict, ref: str) -> str:
    return turn["text"] or f"作りました：{ref}"


async def run_task(orch: Orchestrator, jobs: JobManager, lib: Library, task: Task, suite: Suite,
                   can_see: bool, seed: int) -> dict:
    random.seed(seed)  # 会話モデルが seed を決めないときも、組み合わせ間で同じ乱数にする
    t0 = time.monotonic()
    rec: dict[str, Any] = {"task": task.id, "kind": task.kind, "seed": seed, "turns": [], "images": [],
                           "said_done": []}
    msgs = [{"role": "user", "content": task.request}]
    first = await _turn(orch, jobs, msgs)
    rec["turns"].append(first)
    ref = _last(first, ("generate_image", "edit_image"))
    if ref:
        rec["images"].append(ref)
    if task.kind == "video":
        rec["video"] = _last(first, ("generate_video",))
    elif ref and task.kind == "image" and can_see:
        for _ in range(suite.fix_rounds):
            path = lib.path(lib.get(ref))
            fix = msgs + [{"role": "assistant", "content": _summary(rec["turns"][-1], ref)},
                          {"role": "user", "content": [{"type": "text", "text": FIX_PROMPT.format(ref=ref)},
                                                       image_part(path)]}]
            t = await _turn(orch, jobs, fix)
            rec["turns"].append(t)
            new = _last(t, ("generate_image", "edit_image"))
            if not new:
                rec["said_done"].append(len(rec["images"]) - 1)
                break
            ref = new
            rec["images"].append(ref)
    elif ref and task.kind == "edit":
        t = await _turn(orch, jobs, msgs + [{"role": "assistant", "content": _summary(first, ref)},
                                            {"role": "user", "content": task.followup}])
        rec["turns"].append(t)
        rec["edited"] = _last(t, ("generate_image", "edit_image"))
    rec["seconds"] = round(time.monotonic() - t0, 1)
    return rec


async def run_pair(machine: MachineConfig, catalog_dir: Path, catalog: dict[str, Environment], pair: Pair,
                   suite: Suite, out_dir: Path, engine_factory=None, memory_probe=None) -> dict:
    env, conf, origin = build(machine, catalog, pair)
    factory = CountingFactory(engine_factory or default_engine_factory(machine, out_dir / "logs"), origin)
    rt = Runtime(machine, env, conf, factory, **({"probe": memory_probe} if memory_probe else {}))
    work = out_dir / pair.id
    lib, jobs = Library(work / "library"), JobManager(work / "jobs")
    ff = machine.raw.get("engines", {}).get("ffmpeg", {})
    orch = Orchestrator(rt, lib, jobs, Media(ff.get("binary", "ffmpeg"), ff.get("ffprobe", "ffprobe")),
                        work / "tmp", catalog_dir)
    can_see = "image" in conf.chat.capabilities
    est = sum(c.resident_gb for c in conf.components.values()) * machine.safety_factor
    res: dict[str, Any] = {"pair": dataclasses.asdict(pair), "id": pair.id, "can_see": can_see,
                           "models": {k: c.model for k, c in conf.components.items()},
                           "estimate_gb": round(est, 1), "records": []}
    t0 = time.monotonic()
    try:
        with PeakSampler() as s:
            for rep in range(suite.repeats):
                for i, task in enumerate(suite.tasks):
                    if task.kind == "video" and not pair.video:
                        continue
                    print(f"  {task.id}（{rep + 1}/{suite.repeats}）", flush=True)
                    rec = await run_task(orch, jobs, lib, task, suite, can_see, suite.seed + i * 101 + rep)
                    rec["repeat"] = rep
                    res["records"].append(rec)
        res["peak_gb"] = s.delta_gb
    except Exception as e:  # noqa: BLE001 失敗も結果として残す
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        await rt.unload_all()
    res["seconds"] = round(time.monotonic() - t0, 1)
    res["loads"] = dict(factory.loads)
    res["reloads"] = factory.reloads
    res["load_seconds"] = round(factory.load_seconds, 1)
    res["not_admitted"] = sum(1 for r in res["records"] for t in r["turns"]
                              for e in t["errors"] if "実行前に止めました" in e)
    return res


# ------------------------------------------------------------------ 判定
def sheet(results: list[dict], lib_path: Callable[[str, str], Path]) -> list[dict]:
    """判定する項目の一覧（画像・質問・答えを入れる鍵）。"""
    items = []
    for res in results:
        for rec in res["records"]:
            task = rec["task"]
            targets = [(f"r{i}", ref, "checks") for i, ref in enumerate(rec["images"])]
            if rec.get("edited"):
                targets.append(("edited", rec["edited"], "followup_checks"))
            if rec.get("video"):
                targets.append(("video", rec["video"], "video_checks"))
            for slot, ref, field_ in targets:
                items.append({"key": f"{res['id']}|{task}|{rec['repeat']}|{slot}", "pair": res["id"], "task": task,
                              "slot": slot, "ref": ref, "path": str(lib_path(res["id"], ref)), "field": field_,
                              "video": slot == "video"})
    return items


class LlamaJudge:
    """判定用の理解モデル。生成の組み合わせとは別に、一つだけ読み込んで使う。"""

    def __init__(self, machine: MachineConfig, catalog: dict[str, Environment], ref: str, log_dir: Path,
                 engine_factory=None, memory_probe=None):
        env, conf, c = resolve(catalog, ref)
        if "image" not in c.capabilities:
            raise PairsError(f"判定役 {ref} は画像を見られません")
        self.ref, self.model = ref, c.model
        jconf = Configuration(env.id, conf.id, conf.label, conf.status, c.key, {c.key: c})
        self.rt = Runtime(machine, env, jconf, engine_factory or default_engine_factory(machine, log_dir),
                          **({"probe": memory_probe} if memory_probe else {}))
        self.key = c.key

    async def ask(self, image: Path, check: str, video: bool) -> bool | None:
        text = JUDGE_PROMPT.format(note=VIDEO_NOTE if video else "", check=check)
        msg = [{"role": "user", "content": [{"type": "text", "text": text}, image_part(image)]}]
        async with self.rt.use([self.key], f"perceive:{self.key}") as (eng,):
            r = await eng.chat(msg, max_tokens=16)
        return parse_answer(r["choices"][0]["message"].get("content") or "")

    async def close(self) -> None:
        await self.rt.unload_all()


def parse_answer(text: str) -> bool | None:
    t = re.sub(r"[\s「」『』。、.!！*]", "", text).lower()
    if t.startswith(("はい", "yes")):
        return True
    if t.startswith(("いいえ", "no")):
        return False
    return None


async def judge_all(items: list[dict], tasks: dict[str, Task], ask, media: Media, tmp: Path) -> dict[str, list]:
    """項目ごとに確認項目を判定する。answers[key] = [True/False/None, ...]（確認項目の順）。"""
    answers: dict[str, list] = {}
    tmp.mkdir(parents=True, exist_ok=True)
    for n, it in enumerate(items):
        image = Path(it["path"])
        if it["video"]:
            image = media.thumbnail(image, tmp / f"v{n}.jpg", size=512, tiles=4)
        answers[it["key"]] = [await ask(image, q, it["video"]) for q in getattr(tasks[it["task"]], it["field"])]
    return answers


# ------------------------------------------------------------------ 採点
def _ratio(ans: list | None) -> float | None:
    return None if ans is None or not ans else sum(1 for a in ans if a is True) / len(ans)


def _args_checks(rec: dict, task: Task) -> list[bool]:
    """引数の選び方の確認：壊れた呼び出しが無い、画像ができた、縦横比、避けたい内容、修正の道具、動画の元画像。"""
    first = rec["turns"][0]
    out = [first["invalid"] == 0 and not first["errors"], bool(rec["images"])]
    gen = next((c for c in first["calls"] if c["tool"] == "generate_image"), None)
    want = task.expect.get("orientation")
    if want:
        if gen:
            w, h = image_size(gen["args"], None)
            got = "square" if abs(w - h) <= 64 else ("landscape" if w > h else "portrait")
            out.append(got == want)
        else:
            out.append(False)
    if task.expect.get("negative"):
        out.append(bool(gen and str(gen["args"].get("negative_prompt", "")).strip()))
    if "count" in task.expect:
        out.append(bool(gen and int(gen["args"].get("count", 1)) == int(task.expect["count"])))
    if task.kind == "edit" and len(rec["turns"]) > 1:
        follow = rec["turns"][1]
        out.append(any(c["tool"] == "edit_image" and c["ok"] for c in follow["calls"]))
        out.append(follow["invalid"] == 0 and not follow["errors"])
    if task.kind == "video":
        vid = next((c for c in first["calls"] if c["tool"] == "generate_video"), None)
        out.append(bool(vid and vid["ok"] and rec["images"] and vid["args"].get("image_ref") in rec["images"]))
    return out


def score_pair(res: dict, tasks: dict[str, Task], answers: dict[str, list], weights: dict[str, float]) -> dict:
    first, final, edit, video, args = [], [], [], [], []
    gain = possible = 0.0
    needless = 0
    for rec in res["records"]:
        task = tasks[rec["task"]]
        base = f"{res['id']}|{rec['task']}|{rec['repeat']}|"
        a = _args_checks(rec, task)
        args.append(sum(a) / len(a))
        n = len(task.checks)
        r0: float | None = _ratio(answers.get(base + "r0")) if rec["images"] else 0.0
        if task.kind == "video":
            video.append((_ratio(answers.get(base + "video")) or 0.0) if rec.get("video") else 0.0)
        r0 = r0 or 0.0
        first.append(r0)
        if task.kind == "edit":
            edit.append((_ratio(answers.get(base + "edited")) or 0.0) if rec.get("edited") else 0.0)
        if task.kind == "image" and res["can_see"] and rec["images"]:
            rl = _ratio(answers.get(base + f"r{len(rec['images']) - 1}")) or 0.0
            final.append(rl)
            gain += (rl - r0) * n
            possible += (1 - r0) * n
            if r0 == 1.0 and len(rec["images"]) > 1:
                needless += 1  # 依頼どおりだったのに作り直した
    parts: dict[str, float | None] = {
        "first": _mean(first),
        "args": _mean(args),
        "fix": (max(0.0, gain / possible) if possible else (1.0 if final else None)) if res["can_see"] else 0.0,
        "edit": _mean(edit),
        "video": _mean(video),
    }
    used = {k: w for k, w in weights.items() if parts.get(k) is not None and w > 0}
    quality = sum(parts[k] * w for k, w in used.items()) / sum(used.values()) * 100 if used else 0.0
    n_tasks = len(res["records"]) or 1
    return {
        "quality": round(quality, 1),
        "parts": {k: (round(v * 100, 1) if v is not None else None) for k, v in parts.items()},
        "final": round((_mean(final) or 0) * 100, 1) if final else None,
        "needless_redo": needless,
        "seconds_per_task": round(res["seconds"] / n_tasks, 1),
    }


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def rank(results: list[dict], budget_gb: float, reserve_gb: float, tie_points: float) -> dict:
    """予算から reserve を引いた量に収まる組み合わせの中で、品質が最上位から tie_points 以内なら軽い方を推す。"""
    limit = budget_gb - reserve_gb
    rows = []
    for r in results:
        sc = r.get("score", {})
        fits = not r.get("error") and not r.get("not_admitted") and r.get("peak_gb", 0) <= limit
        why = ("失敗：" + r["error"]) if r.get("error") else (
            "予算で止められた" if r.get("not_admitted") else ("" if fits else f"ピーク {r.get('peak_gb')}GB > {limit:.0f}GB"))
        rows.append({"id": r["id"], "quality": sc.get("quality", 0.0), "peak_gb": r.get("peak_gb"),
                     "estimate_gb": r.get("estimate_gb"), "reloads": r.get("reloads", 0),
                     "seconds_per_task": sc.get("seconds_per_task"), "fits": fits, "why": why})
    ok = [x for x in rows if x["fits"]]
    for x in rows:  # 品質もメモリも上回る組み合わせが無い（パレート最適）
        x["pareto"] = x["fits"] and not any(
            y is not x and y["quality"] >= x["quality"] and (y["peak_gb"] or 0) <= (x["peak_gb"] or 0)
            and (y["quality"], -(y["peak_gb"] or 0)) != (x["quality"], -(x["peak_gb"] or 0)) for y in ok)
    rec = None
    if ok:
        best = max(x["quality"] for x in ok)
        near = [x for x in ok if x["quality"] >= best - tie_points]
        rec = min(near, key=lambda x: ((x["peak_gb"] or 0), x["reloads"], x["seconds_per_task"] or 0))["id"]
    rows.sort(key=lambda x: (not x["fits"], -x["quality"]))
    return {"limit_gb": limit, "rows": rows, "recommended": rec}


def report_text(out: dict) -> str:
    r = out["ranking"]
    lines = [f"# 組み合わせ評価（{out['timestamp']}）", "",
             f"判定役：{out.get('judge') or '外部の答え（answers.json）'}　課題 {len(out['tasks'])} 件　"
             f"使える上限 {r['limit_gb']:.0f}GB（予算 {out['budget_gb']:.0f}GB − 空けておく {out['reserve_gb']:.0f}GB）", ""]
    if out.get("judge_is_candidate"):
        lines += ["注意：判定役が候補の会話モデルと同じモデルです。自分の絵に甘くなる恐れがあります。", ""]
    lines += [f"推奨：**{r['recommended']}**" if r["recommended"] else "推奨：上限に収まる組み合わせがありません", "",
              "| 組み合わせ | 品質 | 初回 | 引数 | 見て直す | 修正 | 動画 | ピーク GB | 読み直し | 秒/課題 | 判定 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    by_id = {x["id"]: x for x in out["results"]}
    for row in r["rows"]:
        p = by_id[row["id"]].get("score", {}).get("parts", {})
        cell = lambda k: "—" if p.get(k) is None else f"{p[k]:.0f}"  # noqa: E731
        mark = ("★" if row["id"] == r["recommended"] else "") + ("◆" if row.get("pareto") else "")
        lines.append(f"| {row['id']} {mark} | {row['quality']:.1f} | {cell('first')} | {cell('args')} | {cell('fix')} | "
                     f"{cell('edit')} | {cell('video')} | {row['peak_gb']} | {row['reloads']} | {row['seconds_per_task']} | "
                     f"{'収まる' if row['fits'] else row['why']} |")
    lines += ["", "★ 推奨　◆ 品質とメモリの両方で上回る組み合わせが無い",
              "品質は 初回・引数・見て直す・修正・動画 の重み付き平均（課題ファイルの weights）。"
              "見て直すは、初回に足りなかった確認項目のうち直して取り戻せた割合（画像を見られない会話モデルは 0）。",
              "読み直しが 0 なら、会話モデルと生成モデルが同時に載ったまま動いた。"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 全体
def _lib_path(out_dir: Path):
    def f(pair_id: str, ref: str) -> Path:
        lib = Library(out_dir / pair_id / "library")
        return lib.path(lib.get(ref))
    return f


def finish(out_dir: Path, answers: dict[str, list], judge_label: str = "") -> dict:
    """生成の記録と判定の答えから採点し、result.json と report.md を書く。"""
    data = json.loads((out_dir / "runs.json").read_text(encoding="utf-8"))
    suite_tasks = {t["id"]: Task(**t) for t in data["tasks"]}
    for res in data["results"]:
        res["score"] = score_pair(res, suite_tasks, answers, data["weights"])
    data["ranking"] = rank(data["results"], data["budget_gb"], data["reserve_gb"], data["tie_points"])
    data["judge"] = judge_label or data.get("judge", "")
    (out_dir / "result.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "report.md").write_text(report_text(data), encoding="utf-8")
    return data


def run(machine: MachineConfig, catalog_dir: Path, suite_path: Path, results_dir: Path, *, only: str = "",
        judge_ref: str | None = None, no_judge: bool = False, engine_factory=None, memory_probe=None,
        judge_ask: Callable[[Path, str, bool], Awaitable[bool | None]] | None = None) -> Path:
    catalog = load_catalog(catalog_dir)
    suite = load_suite(suite_path)
    pairs = pairs_of(suite, catalog, only)
    if not pairs:
        raise PairsError("測る組み合わせがありません")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = results_dir / f"pairs-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for p in pairs:
        env, conf, origin = build(machine, catalog, p)
        missing = missing_models(machine, origin) if engine_factory is None else []
        if missing:
            print(f"== {p.id}：モデル未取得のため省略（{', '.join(missing)}）")
            continue
        print(f"== {p.id} ==", flush=True)
        results.append(asyncio.run(run_pair(machine, catalog_dir, catalog, p, suite, out_dir, engine_factory,
                                            memory_probe)))
    judge_ref = judge_ref if judge_ref is not None else suite.judge
    data = {"timestamp": stamp, "tasks": [dataclasses.asdict(t) for t in suite.tasks], "budget_gb": machine.budget_gb,
            "reserve_gb": suite.reserve_gb, "tie_points": suite.tie_points, "weights": suite.weights,
            "judge": judge_ref, "judge_is_candidate": False, "results": results}
    (out_dir / "runs.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    items = sheet(results, _lib_path(out_dir))
    tasks = {t.id: t for t in suite.tasks}
    (out_dir / "judge-sheet.json").write_text(json.dumps(
        [{**it, "questions": getattr(tasks[it["task"]], it["field"])} for it in items], ensure_ascii=False, indent=1),
        encoding="utf-8")
    if no_judge:
        print(f"\n生成だけ終えました：{out_dir}\njudge-sheet.json の答えを answers.json に書き、"
              "lao p0 pairs --score で採点します")
        return out_dir
    media = Media(*(machine.raw.get("engines", {}).get("ffmpeg", {}).get(k, d)
                    for k, d in (("binary", "ffmpeg"), ("ffprobe", "ffprobe"))))
    judge = None
    if judge_ask is None:
        if not judge_ref:
            raise PairsError("判定役（[candidates] judge か --judge）がありません。--no-judge で生成だけ行えます")
        judge = LlamaJudge(machine, catalog, judge_ref, out_dir / "logs", memory_probe=memory_probe)
        judge_ask = judge.ask
        chat_models = {resolve(catalog, c)[2].model for c in suite.chat}
        if judge.model in chat_models:
            data["judge_is_candidate"] = True
            (out_dir / "runs.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("== 判定 ==", flush=True)

    async def _judge():
        try:
            return await judge_all(items, tasks, judge_ask, media, out_dir / "judge-tmp")
        finally:
            if judge:
                await judge.close()

    answers = asyncio.run(_judge())
    (out_dir / "answers.json").write_text(json.dumps(answers, ensure_ascii=False, indent=1), encoding="utf-8")
    finish(out_dir, answers, judge_ref)
    print(f"\n結果：{out_dir / 'report.md'}")
    return out_dir


def latest(results_dir: Path) -> dict | None:
    """最新の組み合わせ評価の結果（lao p0 propose が推奨を知らせるのに使う）。"""
    dirs = sorted(d for d in results_dir.glob("pairs-*") if (d / "result.json").exists())
    return json.loads((dirs[-1] / "result.json").read_text(encoding="utf-8")) if dirs else None
