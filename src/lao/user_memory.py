"""会話をまたいで覚えておくこと（Claude のメモリに相当）。

項目は一文ずつ。全体の項目と、プロジェクトごとの項目に分ける。利用者は一覧で確認・編集・削除できる。
シークレット会話では読みも書きもしない。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_ITEMS = 200
MAX_IN_PROMPT = 40


@dataclass
class MemoryItem:
    id: str
    text: str
    project_id: str = ""
    source: str = ""  # 覚えた会話の id（手で足したものは空）
    created_at: float = field(default_factory=time.time)


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list(self, project_id: str | None = None) -> list[MemoryItem]:
        try:
            items = [MemoryItem(**d) for d in json.loads(self.path.read_text(encoding="utf-8"))]
        except (OSError, json.JSONDecodeError, TypeError):
            items = []
        if project_id is not None:
            items = [m for m in items if m.project_id in ("", project_id)]
        return items

    def _write(self, items: list[MemoryItem]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(m) for m in items], ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, text: str, project_id: str = "", source: str = "") -> MemoryItem:
        text = " ".join(text.split())[:500]
        if not text:
            raise ValueError("内容が空です")
        items = self.list()
        for m in items:
            if m.text == text and m.project_id == project_id:
                return m
        item = MemoryItem(uuid.uuid4().hex[:12], text, project_id, source)
        items.append(item)
        self._write(items[-MAX_ITEMS:])
        return item

    def update(self, mid: str, text: str) -> MemoryItem:
        items = self.list()
        for m in items:
            if m.id == mid:
                m.text = " ".join(text.split())[:500]
                self._write(items)
                return m
        raise KeyError(mid)

    def delete(self, mid: str) -> None:
        self._write([m for m in self.list() if m.id != mid])

    def clear(self) -> None:
        self._write([])

    def for_prompt(self, project_id: str = "") -> list[str]:
        return [m.text for m in self.list(project_id)][-MAX_IN_PROMPT:]

    def import_text(self, text: str, project_id: str = "") -> int:
        """一行一項目で取り込む（他の AI から書き出した内容の貼り付けを想定）。"""
        n = 0
        for line in text.splitlines():
            line = line.strip().lstrip("-*・").strip()
            if line:
                self.add(line, project_id)
                n += 1
        return n

    def export_text(self) -> str:
        return "\n".join(f"- {m.text}" for m in self.list())
