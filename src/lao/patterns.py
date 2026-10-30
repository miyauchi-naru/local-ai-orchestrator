"""パターン：生成モデルごとに用意した ComfyUI のワークフローを、会話モデルが名前で選んで組み合わせる。

会話モデルは ComfyUI のグラフを書かない。lao が用意して承認したパターン（拡大、顔と手の補正、
参考画像のポーズで描く、範囲を描き直す、三面図 など）を、最大 MAX_STEPS 手まで並べた「手順」を出し、
前の手の出力を次の手の画像入力へ渡して実行する。

  - パターンは catalog の生成部品の `patterns` フォルダに、ワークフロー JSON と説明の TOML を置く。
  - 使えるのは承認したものだけ。承認（lao pattern approve）のときに一度だけ、許可ノード・パス・
    ネットへの接続・モデルのファイル名を検証し、ワークフローのハッシュを TOML に記録する。
    ワークフローが後で変わったら、承認し直すまで使えない。
  - 会話モデルに見せる候補は、依頼の文と添付の有無で機械的に絞る（既定 3 個まで）。
  - メモリは、パターンごとの作業量（work_gb、P0 で実測）を生成部品の常駐に足して、実行前に受付判定する。
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Component, Configuration

MAX_STEPS = 6
MAX_NODES = 80
MAX_SHOWN = 3
OUTPUTS = ("image", "video")
PARAM_TYPES = ("string", "integer", "number")
# 生成部品のワークフローに共通で差し込める値（パターン側で bindings に書いたものだけ使う）
COMMON = {"prompt", "negative", "seed", "width", "height"}
URL_RE = re.compile(r"https?://|ftp://|file:", re.I)
MODEL_INPUTS = {"ckpt_name", "unet_name", "vae_name", "clip_name", "clip_name1", "clip_name2", "clip_name3",
                "lora_name", "control_net_name", "model_name", "upscale_model", "style_model_name",
                "clip_vision_name", "ipadapter_file"}


class PatternError(ValueError):
    pass


@dataclass
class Param:
    type: str
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 2000
    enum: list[str] | None = None
    default: Any = None

    def schema(self) -> dict:
        s: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            s["enum"] = self.enum
        if self.minimum is not None:
            s["minimum"] = self.minimum
        if self.maximum is not None:
            s["maximum"] = self.maximum
        return s


@dataclass
class Pattern:
    id: str
    label: str
    does: str
    needs: str
    good_for: list[str]
    avoid: str
    output: str
    images: list[str]  # 画像入力の差し込み名。最初のものに前の手の出力（か指定の画像）が入る
    params: dict[str, Param]
    bindings: dict[str, str]
    models: list[str]
    work_gb: float
    workflow_path: Path
    toml_path: Path
    approved: str = ""
    component: str = ""  # 構成の中の生成部品の名前
    problems: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.approved) and self.approved == workflow_hash(self.workflow_path) and not self.problems

    def describe(self) -> str:
        parts = [f"{self.id}：{self.does}", f"要るもの：{self.needs}"]
        if self.good_for:
            parts.append("向いている依頼：" + "／".join(self.good_for[:3]))
        if self.avoid:
            parts.append(f"使わない：{self.avoid}")
        return "。".join(parts)


def workflow_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _param(name: str, d: dict, where: str) -> Param:
    t = d.get("type", "string")
    if t not in PARAM_TYPES:
        raise PatternError(f"{where}: params.{name} の type は {PARAM_TYPES} のどれかです")
    return Param(t, d.get("description", ""), d.get("min"), d.get("max"), int(d.get("max_length", 2000)),
                 d.get("enum"), d.get("default"))


def load_pattern(path: Path, component: str = "") -> Pattern:
    try:
        d = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise PatternError(f"{path.name}: 読めません：{e}") from e
    where = path.name
    for k in ("id", "does", "workflow", "bindings"):
        if k not in d:
            raise PatternError(f"{where}: '{k}' がありません")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", d["id"]):
        raise PatternError(f"{where}: id は半角の小文字・数字・ハイフンで 40 字までです")
    output = d.get("output", "image")
    if output not in OUTPUTS:
        raise PatternError(f"{where}: output は image か video です")
    params = {k: _param(k, v, where) for k, v in d.get("params", {}).items()}
    clash = (set(params) & (COMMON | set(d.get("images", []))))
    if clash:
        raise PatternError(f"{where}: params に共通の名前は使えません：{', '.join(sorted(clash))}")
    p = Pattern(
        id=d["id"], label=d.get("label", d["id"]), does=d["does"], needs=d.get("needs", "なし"),
        good_for=list(d.get("good_for", [])), avoid=d.get("avoid", ""), output=output,
        images=list(d.get("images", [])), params=params, bindings=dict(d["bindings"]),
        models=list(d.get("models", [])), work_gb=float(d.get("work_gb", 0)),
        workflow_path=path.parent / d["workflow"], toml_path=path, approved=d.get("approved", ""),
        component=component,
    )
    missing = [k for k in [*p.images, *p.params] if k not in p.bindings]
    if missing:
        raise PatternError(f"{where}: bindings に差し込み先がありません：{', '.join(missing)}")
    return p


def patterns_for(catalog_dir: Path, conf: Configuration) -> list[Pattern]:
    """構成の生成部品（patterns フォルダを持つもの）のパターン。承認していないものも返す（usable で見分ける）。"""
    out: list[Pattern] = []
    for c in conf.components.values():
        rel = c.raw.get("patterns")
        if not rel or c.engine != "comfyui":
            continue
        folder = catalog_dir / rel
        for f in sorted(folder.glob("*.toml")):
            try:
                out.append(load_pattern(f, c.key))
            except PatternError as e:
                bad = Pattern(f.stem, f.stem, "", "", [], "", "image", [], {}, {}, [], 0, f, f, component=c.key)
                bad.problems.append(str(e))
                out.append(bad)
    seen: set[str] = set()
    for p in out:  # 同じ id が二つあれば両方使わない
        if p.id in seen:
            for q in out:
                if q.id == p.id:
                    q.problems.append(f"id {p.id} が重なっています")
        seen.add(p.id)
    return out


# ------------------------------------------------------------------ 取り込み時の検証（承認の前に一度だけ）
def load_allowlist(catalog_dir: Path) -> set[str]:
    path = catalog_dir / "shared" / "comfy-nodes.toml"
    d = tomllib.loads(path.read_text(encoding="utf-8"))
    return set(d.get("allowed", []))


def check(p: Pattern, allowed_nodes: set[str]) -> list[str]:
    """ワークフローを検証し、問題の一覧を返す（空なら承認できる）。"""
    problems: list[str] = []
    try:
        wf = json.loads(p.workflow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"ワークフロー {p.workflow_path.name} が読めません：{e}"]
    if not isinstance(wf, dict) or not all(isinstance(n, dict) and "class_type" in n for n in wf.values()):
        return ["API 形式（Export (API)）のワークフローではありません"]
    if len(wf) > MAX_NODES:
        problems.append(f"ノードが多すぎます（{len(wf)} > {MAX_NODES}）")
    bound = {}
    for key, target in p.bindings.items():
        try:
            node_id, section, name = target.split(".", 2)
        except ValueError:
            problems.append(f"bindings.{key} は 'ノード番号.inputs.入力名' の形です")
            continue
        if node_id not in wf:
            problems.append(f"bindings.{key}：ノード {node_id} がありません")
        bound[(node_id, name)] = key
    for node_id, node in wf.items():
        ct = node["class_type"]
        if ct not in allowed_nodes:
            problems.append(f"ノード {node_id}（{ct}）は許可されていません（catalog/shared/comfy-nodes.toml）")
        for name, value in (node.get("inputs") or {}).items():
            if isinstance(value, list):  # 他のノードへのつなぎ
                continue
            if ct in ("LoadImage", "LoadImageMask", "LoadVideo") and name in ("image", "video") \
                    and (node_id, name) not in bound:
                problems.append(f"ノード {node_id}（{ct}）の {name} は bindings で lao が差し込む形にしてください")
            if not isinstance(value, str):
                continue
            if URL_RE.search(value):
                problems.append(f"ノード {node_id} の {name} に URL があります")
            if name in MODEL_INPUTS:
                if value not in p.models:
                    problems.append(f"ノード {node_id} のモデル {value} が models の一覧にありません")
                if ".." in value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
                    problems.append(f"ノード {node_id} のモデル名 {value} にパスが含まれています")
            elif name in ("filename_prefix",):
                if "/" in value or "\\" in value or ".." in value:
                    problems.append(f"ノード {node_id} の保存先の名前 {value} にフォルダが含まれています")
            elif re.search(r"^(/|[A-Za-z]:[\\/])|\.\.[\\/]", value) and (node_id, name) not in bound:
                problems.append(f"ノード {node_id} の {name} にパスのような値があります：{value[:60]}")
    if not any(n["class_type"].startswith(("Save", "VHS_VideoCombine")) for n in wf.values()):
        problems.append("出力を保存するノード（SaveImage・SaveVideo など）がありません")
    if not p.does.strip() or not p.good_for:
        problems.append("does（何をするか）と good_for（向いている依頼の例）を書いてください。候補を絞るのに使います")
    if p.work_gb <= 0:
        problems.append("work_gb（このパターンの作業メモリ。P0 で実測）を書いてください")
    return problems


def approve(p: Pattern, allowed_nodes: set[str]) -> list[str]:
    """検証に通れば、ワークフローのハッシュを TOML の approved に書く。問題があれば書かずに返す。"""
    problems = check(p, allowed_nodes)
    if problems:
        return problems
    text = p.toml_path.read_text(encoding="utf-8")
    line = f'approved = "{workflow_hash(p.workflow_path)}"'
    if re.search(r"^approved\s*=.*$", text, re.M):
        text = re.sub(r"^approved\s*=.*$", line, text, count=1, flags=re.M)
    else:
        text = line + "\n" + text
    p.toml_path.write_text(text, encoding="utf-8")
    return []


def scaffold(workflow: Path, folder: Path, pid: str) -> Path:
    """書き出したワークフローから、説明の TOML の下書きを作る（差し込み先は見つけた分だけ埋める）。"""
    wf = json.loads(workflow.read_text(encoding="utf-8"))
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{pid}.json"
    if dest.resolve() != workflow.resolve():
        dest.write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    bindings, images, models, params = {}, [], [], {}
    texts = [k for k, n in wf.items() if n["class_type"] == "CLIPTextEncode"]
    if texts:
        bindings["prompt"] = f"{texts[0]}.inputs.text"
    if len(texts) > 1:
        bindings["negative"] = f"{texts[1]}.inputs.text"
    for k, n in wf.items():
        ins = n.get("inputs") or {}
        if n["class_type"] in ("LoadImage",):
            name = "image" if not images else f"image{len(images) + 1}"
            images.append(name)
            bindings[name] = f"{k}.inputs.image"
        if n["class_type"].startswith("KSampler"):
            if "seed" in ins or "noise_seed" in ins:
                bindings.setdefault("seed", f"{k}.inputs.{'seed' if 'seed' in ins else 'noise_seed'}")
            for name in ("steps", "cfg", "denoise"):
                if name in ins and not isinstance(ins[name], list):
                    bindings.setdefault(name, f"{k}.inputs.{name}")
                    params.setdefault(name, ins[name])
        for name, v in ins.items():
            if name in MODEL_INPUTS and isinstance(v, str):
                models.append(v)
    lines = [
        f'id = "{pid}"', 'label = ""', 'does = ""                # 何をするか（一文）',
        'needs = ""               # 要るもの（例：画像 1 枚）', 'good_for = []            # 向いている依頼の例（日本語で 2〜3 個）',
        'avoid = ""               # 使ってはいけない時', 'output = "image"', f'workflow = "{dest.name}"',
        f"images = {json.dumps(images, ensure_ascii=False)}", f"models = {json.dumps(sorted(set(models)), ensure_ascii=False)}",
        "work_gb = 0              # P0 で実測して書く", 'approved = ""            # lao pattern approve が書く', "",
        "[bindings]", *[f'{k} = "{v}"' for k, v in bindings.items()], "",
    ]
    limits = {"steps": (1, 80), "cfg": (0, 20), "denoise": (0.05, 1.0)}
    for name, default in params.items():
        lo, hi = limits[name]
        t = "integer" if name == "steps" else "number"
        lines += [f"[params.{name}]", f'type = "{t}"', f"min = {lo}", f"max = {hi}", f"default = {default}",
                  f'description = "{name}"', ""]
    out = folder / f"{pid}.toml"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ------------------------------------------------------------------ 会話モデルへの見せ方
def _grams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def shortlist(patterns: list[Pattern], text: str, has_image: bool, limit: int = MAX_SHOWN) -> list[Pattern]:
    """依頼の文と画像の有無で、見せる候補を機械的に絞る。

    画像が要るパターンは、添付か会話の中に画像が無ければ外す。残りを、依頼の文と
    パターンの説明（何をするか・向いている依頼）の文字の重なりで並べ、上から limit 個。
    """
    ok = [p for p in patterns if p.usable and (has_image or not p.images)]
    want = _grams(text)

    def score(p: Pattern) -> tuple[int, str]:
        return (-len(want & _grams(" ".join([p.label, p.does, *p.good_for]))), p.id)

    return sorted(ok, key=score)[:limit]


def recipe_schema(shown: list[Pattern]) -> dict:
    ids = [p.id for p in shown]
    params: dict[str, Any] = {}
    for p in shown:
        for k, v in p.params.items():
            params.setdefault(k, v.schema())
    lines = "\n".join(f"- {p.describe()}" for p in shown)
    return {"type": "function", "function": {
        "name": "run_recipe",
        "description": ("用意したパターン（ComfyUI のワークフロー）を、最大 6 手まで順に使う。前の手の出力が次の手の画像入力になる。"
                        "1 手目に画像が要るパターンを使うときは image_ref に元の画像（id@版）を書く。"
                        "1 回の生成で済むなら generate_image・edit_image を使う。使えるパターン：\n" + lines),
        "parameters": {"type": "object", "properties": {
            "steps": {"type": "array", "maxItems": MAX_STEPS, "items": {"type": "object", "properties": {
                "pattern": {"type": "string", "enum": ids},
                "prompt": {"type": "string", "description": "この手で描く内容（英語推奨。要らないパターンもある）"},
                "negative_prompt": {"type": "string"},
                "aspect": {"type": "string", "description": "縦横比（新しく描くパターンだけ）"},
                "image_ref": {"type": "string", "description": "この手の画像入力（id@版）。2 手目以降は省くと前の手の出力"},
                "seed": {"type": "integer", "minimum": 0, "maximum": 2**32 - 1},
                **params}, "required": ["pattern"], "additionalProperties": False}},
        }, "required": ["steps"], "additionalProperties": False}}}


def validate_recipe(raw: Any, patterns: list[Pattern], library, max_pixels: int) -> list[dict]:
    """手順を検証し、正規化した手の一覧を返す。おかしければ PatternError。"""
    from .image_styles import ASPECTS
    from .tools import REF_RE

    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError as e:
            raise PatternError(f"引数が JSON ではありません：{e}") from e
    steps = raw.get("steps") if isinstance(raw, dict) else None
    if not isinstance(steps, list) or not steps:
        raise PatternError("steps に 1 手以上を書いてください")
    if len(steps) > MAX_STEPS:
        raise PatternError(f"手順は {MAX_STEPS} 手までです")
    by_id = {p.id: p for p in patterns if p.usable}
    out = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            raise PatternError(f"{i} 手目がオブジェクトではありません")
        p = by_id.get(str(s.get("pattern")))
        if not p:
            raise PatternError(f"{i} 手目：パターン '{s.get('pattern')}' は使えません（使えるもの：{', '.join(by_id) or 'なし'}）")
        known = {"pattern", "prompt", "negative_prompt", "aspect", "image_ref", "seed", *p.params}
        unknown = set(s) - known
        if unknown:
            raise PatternError(f"{i} 手目（{p.id}）：このパターンに無い値です：{', '.join(sorted(unknown))}")
        step: dict[str, Any] = {"pattern": p.id}
        for k in ("prompt", "negative_prompt"):
            if k in s:
                if not isinstance(s[k], str) or len(s[k]) > 4000:
                    raise PatternError(f"{i} 手目：{k} は 4000 字までの文字列です")
                step[k] = s[k]
        if "aspect" in s:
            if s["aspect"] not in ASPECTS:
                raise PatternError(f"{i} 手目：aspect は {list(ASPECTS)} のどれかです")
            w, h = ASPECTS[s["aspect"]]
            if w * h > max_pixels:
                raise PatternError(f"{i} 手目：大きすぎます（{w}×{h}）")
            step["aspect"] = s["aspect"]
        if "seed" in s:
            if isinstance(s["seed"], bool) or not isinstance(s["seed"], int) or not 0 <= s["seed"] < 2**32:
                raise PatternError(f"{i} 手目：seed は 0〜2^32-1 の整数です")
            step["seed"] = s["seed"]
        if "image_ref" in s:
            ref = s["image_ref"]
            if not isinstance(ref, str) or not REF_RE.match(ref):
                raise PatternError(f"{i} 手目：image_ref は成果物の id@版 で書いてください")
            try:
                art = library.get(ref)
            except KeyError as e:
                raise PatternError(str(e)) from e
            if art.kind != "image":
                raise PatternError(f"{i} 手目：image_ref は画像にしてください（{ref} は {art.kind}）")
            step["image_ref"] = ref
        if p.images and i == 1 and "image_ref" not in step:
            raise PatternError(f"1 手目（{p.id}）は画像が要ります。image_ref に元の画像を書いてください")
        if i > 1 and p.images and out[-1]["output"] != "image" and "image_ref" not in step:
            raise PatternError(f"{i} 手目（{p.id}）：前の手の出力は動画なので、画像入力に使えません")
        for k, prm in p.params.items():
            v = s.get(k, prm.default)
            if v is None:
                continue
            if prm.type == "string":
                if not isinstance(v, str) or len(v) > prm.max_length:
                    raise PatternError(f"{i} 手目（{p.id}）：{k} は {prm.max_length} 字までの文字列です")
                if prm.enum and v not in prm.enum:
                    raise PatternError(f"{i} 手目（{p.id}）：{k} は {prm.enum} のどれかです")
            else:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or (prm.type == "integer" and not isinstance(v, int)):
                    raise PatternError(f"{i} 手目（{p.id}）：{k} は{'整数' if prm.type == 'integer' else '数値'}です")
                if prm.minimum is not None and v < prm.minimum or prm.maximum is not None and v > prm.maximum:
                    raise PatternError(f"{i} 手目（{p.id}）：{k} は {prm.minimum}〜{prm.maximum} です")
            step[k] = v
        step["output"] = p.output
        out.append(step)
    return out


def component_for(conf: Configuration, p: Pattern) -> Component:
    return conf.components[p.component]
