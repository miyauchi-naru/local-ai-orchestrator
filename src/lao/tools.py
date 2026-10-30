"""対話モデルが呼べるツールの定義と引数検証（企画書 8 章、R02・R13）。

対話モデルの文章を直接実行せず、ここで定義したツールだけを、検証済みの引数で実行する。
成果物は id@版 で指定させ、ファイルパスは受け取らない（パス逸脱を防ぐ）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Configuration
from .image_styles import ASPECTS
from .library import Library

REF_RE = re.compile(r"^[0-9a-f]{12}@[1-9][0-9]{0,5}$")


class ToolError(ValueError):
    pass


@dataclass
class Param:
    type: str  # string | integer | number | ref
    description: str
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None
    enum: list[str] | None = None
    ref_kinds: list[str] = field(default_factory=list)  # ref のときに許す成果物の種類

    def schema(self) -> dict:
        t = "string" if self.type == "ref" else self.type
        s: dict[str, Any] = {"type": t, "description": self.description}
        if self.enum:
            s["enum"] = self.enum
        if self.minimum is not None:
            s["minimum"] = self.minimum
        if self.maximum is not None:
            s["maximum"] = self.maximum
        return s


@dataclass
class Tool:
    name: str
    description: str
    needs: str  # 構成に必要な能力
    params: dict[str, Param]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {k: p.schema() for k, p in self.params.items()},
                    "required": [k for k, p in self.params.items() if p.required],
                    "additionalProperties": False,
                },
            },
        }


ALL_TOOLS = [
    Tool(
        "describe_media",
        "添付・保存済みの画像、音声、動画の内容を理解する。動画は区間を指定できる。",
        "perceive",
        {
            "ref": Param("ref", "対象の成果物（id@版）", True, ref_kinds=["image", "audio", "video"]),
            "question": Param("string", "知りたいこと", True, max_length=2000),
            "start_seconds": Param("number", "動画・音声の開始秒", minimum=0),
            "end_seconds": Param("number", "動画・音声の終了秒", minimum=0),
        },
    ),
    Tool(
        "generate_image",
        "文章から画像を生成する。",
        "image_generate",
        {
            "prompt": Param("string", "生成内容（英語推奨）", True, max_length=4000),
            "negative_prompt": Param("string", "避けたい内容", max_length=2000),
            "width": Param("integer", "幅（px）。aspect より優先", minimum=256, maximum=2048),
            "height": Param("integer", "高さ（px）。aspect より優先", minimum=256, maximum=2048),
            "aspect": Param("string", "縦横比（既定 1:1）", enum=list(ASPECTS)),
            "count": Param("integer", "枚数（seed を 1 ずつ変えて作る。既定 1）", minimum=1, maximum=8),
            "style": Param("string", "保存したスタイルの名前（前置き・避けたい内容・縦横比を足す）", max_length=40),
            "seed": Param("integer", "乱数シード", minimum=0, maximum=2**32 - 1),
        },
    ),
    Tool(
        "edit_image",
        "既存の画像を指示に従って修正し、新しい版として保存する。",
        "image_edit",
        {
            "ref": Param("ref", "修正元の画像（id@版）", True, ref_kinds=["image"]),
            "instruction": Param("string", "修正内容", True, max_length=4000),
            "seed": Param("integer", "乱数シード", minimum=0, maximum=2**32 - 1),
        },
    ),
    Tool(
        "generate_speech",
        "日本語などの文章を読み上げた音声を生成する。参考音声で話し方を指定できる。",
        "speech_generate",
        {
            "text": Param("string", "読み上げる文章", True, max_length=10000),
            "reference_ref": Param("ref", "参考音声（id@版）。本人の同意がある声に限る", ref_kinds=["audio"]),
            "style": Param("string", "話し方の指定（例：落ち着いて、明るく）", max_length=500),
            "speed": Param("number", "話す速さ（1.0 が標準）", minimum=0.5, maximum=2.0),
            "seed": Param("integer", "乱数シード", minimum=0, maximum=2**32 - 1),
        },
    ),
    Tool(
        "generate_video",
        "画像を元に短い動画を生成する。",
        "image_to_video",
        {
            "image_ref": Param("ref", "元にする画像（id@版）", True, ref_kinds=["image"]),
            "prompt": Param("string", "動きや場面の説明（英語推奨）", True, max_length=4000),
            "seconds": Param("number", "長さ（秒）", True, minimum=1, maximum=20),
            "with_audio": Param("string", "音声を付けるか", enum=["yes", "no"]),
            "seed": Param("integer", "乱数シード", minimum=0, maximum=2**32 - 1),
        },
    ),
]
TOOLS = {t.name: t for t in ALL_TOOLS}


def tools_for(conf: Configuration) -> list[Tool]:
    caps = conf.capabilities()
    has_perceiver = any(c.role == "perceive" for c in conf.components.values()) or bool(
        {"image", "audio", "video_frames", "video_audio"} & set(conf.chat.capabilities)
    )
    out = []
    for t in ALL_TOOLS:
        if t.needs == "perceive":
            if has_perceiver:
                out.append(t)
        elif t.needs in caps:
            out.append(t)
    return out


def validate(name: str, raw_args: str | dict, conf: Configuration, library: Library) -> dict:
    """ツール名と引数を検証し、正規化した引数を返す。おかしければ ToolError。"""
    allowed = {t.name for t in tools_for(conf)}
    if name not in TOOLS:
        raise ToolError(f"未登録のツール '{name}' は使えません")
    if name not in allowed:
        raise ToolError(f"ツール '{name}' はこの環境では使えません")
    tool = TOOLS[name]
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError as e:
            raise ToolError(f"引数が JSON ではありません：{e}") from e
    else:
        args = dict(raw_args)
    if not isinstance(args, dict):
        raise ToolError("引数はオブジェクトで渡してください")
    unknown = set(args) - set(tool.params)
    if unknown:
        raise ToolError(f"不明な引数：{', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for key, p in tool.params.items():
        if key not in args or args[key] is None:
            if p.required:
                raise ToolError(f"必須の引数 '{key}' がありません")
            continue
        v = args[key]
        if p.type in ("string", "ref"):
            if not isinstance(v, str):
                raise ToolError(f"'{key}' は文字列で指定してください")
            if p.max_length and len(v) > p.max_length:
                raise ToolError(f"'{key}' が長すぎます（最大 {p.max_length} 文字）")
            if p.enum and v not in p.enum:
                raise ToolError(f"'{key}' は {p.enum} のいずれかです")
            if p.type == "ref":
                if not REF_RE.match(v):
                    raise ToolError(f"'{key}' は成果物の id@版 で指定してください")
                try:
                    art = library.get(v)
                except KeyError as e:
                    raise ToolError(str(e)) from e
                if p.ref_kinds and art.kind not in p.ref_kinds:
                    raise ToolError(f"'{key}' には {p.ref_kinds} の成果物を指定してください（{v} は {art.kind}）")
        elif p.type == "integer":
            if isinstance(v, bool) or not isinstance(v, int):
                raise ToolError(f"'{key}' は整数で指定してください")
        elif p.type == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ToolError(f"'{key}' は数値で指定してください")
        if p.type in ("integer", "number"):
            if p.minimum is not None and v < p.minimum:
                raise ToolError(f"'{key}' は {p.minimum} 以上です")
            if p.maximum is not None and v > p.maximum:
                raise ToolError(f"'{key}' は {p.maximum} 以下です")
        out[key] = v
    if "start_seconds" in out and "end_seconds" in out and out["end_seconds"] <= out["start_seconds"]:
        raise ToolError("end_seconds は start_seconds より後にしてください")
    return out
