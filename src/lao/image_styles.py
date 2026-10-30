"""画像の「見た目の型」（スタイル）：プロンプトの前置き・避けたい内容・縦横比を名前で保存して呼ぶ。

画面・対話・別の PC（lao mcp）のどこからでも同じ型を使えるよう、root/image_styles.json に置く。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

# 縦横比ごとの大きさ（どれも 64 の倍数、約 100 万画素）
ASPECTS = {
    "1:1": (1024, 1024), "16:9": (1344, 768), "9:16": (768, 1344), "4:3": (1152, 896),
    "3:4": (896, 1152), "3:2": (1216, 832), "2:3": (832, 1216),
}
MAX_STYLES = 50


class StyleError(ValueError):
    pass


@dataclass
class ImageStyle:
    name: str
    prompt: str = ""      # 前置き。{prompt} を含めばそこに依頼文を入れ、無ければ依頼文の後ろに足す
    negative: str = ""
    aspect: str = ""
    note: str = ""

    def apply(self, prompt: str) -> str:
        if not self.prompt.strip():
            return prompt
        if "{prompt}" in self.prompt:
            return self.prompt.replace("{prompt}", prompt)
        return f"{prompt}, {self.prompt.strip()}"


class StyleStore:
    def __init__(self, path: Path):
        self.path = path

    def list(self) -> list[ImageStyle]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [ImageStyle(**{k: str(v) for k, v in x.items() if k in ImageStyle.__dataclass_fields__})
                for x in raw if isinstance(x, dict) and x.get("name")]

    def get(self, name: str) -> ImageStyle:
        for s in self.list():
            if s.name == name:
                return s
        names = "、".join(s.name for s in self.list()) or "（まだありません）"
        raise StyleError(f"スタイル「{name}」はありません。あるもの：{names}")

    def save(self, name: str, prompt: str = "", negative: str = "", aspect: str = "", note: str = "") -> ImageStyle:
        name = name.strip()[:40]
        if not name:
            raise StyleError("スタイルの名前を付けてください")
        if aspect and aspect not in ASPECTS:
            raise StyleError(f"aspect は {list(ASPECTS)} のいずれかです")
        s = ImageStyle(name, prompt[:2000], negative[:1000], aspect, note[:200])
        items = [x for x in self.list() if x.name != name] + [s]
        if len(items) > MAX_STYLES:
            raise StyleError(f"スタイルは {MAX_STYLES} 件までです")
        self._write(items)
        return s

    def delete(self, name: str) -> None:
        items = self.list()
        if not any(x.name == name for x in items):
            raise StyleError(f"スタイル「{name}」はありません")
        self._write([x for x in items if x.name != name])

    def _write(self, items: list[ImageStyle]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(x) for x in items], ensure_ascii=False, indent=1), encoding="utf-8")


def image_size(args: dict, style: ImageStyle | None) -> tuple[int, int]:
    """幅・高さの指定 → 縦横比の指定 → スタイルの縦横比 → 正方形 の順に決める。"""
    aspect = args.get("aspect") or (style.aspect if style else "") or "1:1"
    w, h = ASPECTS.get(aspect, ASPECTS["1:1"])
    return int(args.get("width") or w), int(args.get("height") or h)
