"""会話の保存。会話は一つの環境・構成に結び付け、別の環境では続けない（R07）。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Turn:
    role: str  # user | assistant
    content: str
    attachments: list[str] = field(default_factory=list)  # 成果物 ref
    artifacts: list[str] = field(default_factory=list)  # この返答で作られた成果物 ref
    progress: list[str] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class Conversation:
    id: str
    title: str
    env_id: str
    configuration: str
    created_at: float
    updated_at: float
    turns: list[Turn] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "env_id": self.env_id, "configuration": self.configuration,
                "created_at": self.created_at, "updated_at": self.updated_at, "turns": len(self.turns)}

    def model_messages(self) -> list[dict]:
        """対話モデルへ渡す履歴。添付は id@版 の文字で示す。"""
        out = []
        for t in self.turns:
            if t.role == "assistant" and not t.content:
                continue
            text = t.content
            if t.attachments:
                text += "\n[添付 " + ", ".join(t.attachments) + "]"
            if t.artifacts:
                text += "\n[作成 " + ", ".join(t.artifacts) + "]"
            out.append({"role": t.role, "content": text})
        return out


class ConversationStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, cid: str) -> Path:
        if not cid.isalnum():
            raise KeyError(cid)
        return self.root / f"{cid}.json"

    def create(self, env_id: str, configuration: str, title: str = "") -> Conversation:
        now = time.time()
        c = Conversation(uuid.uuid4().hex[:12], title or "新しい会話", env_id, configuration, now, now)
        self.save(c)
        return c

    def save(self, c: Conversation) -> None:
        c.updated_at = time.time()
        tmp = self._path(c.id).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(c), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path(c.id))

    def get(self, cid: str) -> Conversation:
        p = self._path(cid)
        if not p.exists():
            raise KeyError(cid)
        d = json.loads(p.read_text(encoding="utf-8"))
        d["turns"] = [Turn(**t) for t in d.get("turns", [])]
        return Conversation(**d)

    def list(self) -> list[Conversation]:
        out = []
        for p in self.root.glob("*.json"):
            try:
                out.append(self.get(p.stem))
            except (KeyError, json.JSONDecodeError, TypeError):
                continue
        return sorted(out, key=lambda c: c.updated_at, reverse=True)

    def delete(self, cid: str) -> None:
        self._path(cid).unlink(missing_ok=True)
