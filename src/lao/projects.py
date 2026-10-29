"""プロジェクト：会話をまとめ、共通の指示と資料（成果物 id@版）を効かせる。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Project:
    id: str
    name: str
    description: str = ""
    instructions: str = ""
    knowledge: list[str] = field(default_factory=list)  # 資料（成果物 id@版）
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class ProjectStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, pid: str) -> Path:
        if not pid.isalnum():
            raise KeyError(pid)
        return self.root / f"{pid}.json"

    def create(self, name: str, **kw) -> Project:
        p = Project(uuid.uuid4().hex[:12], name, **kw)
        self.save(p)
        return p

    def save(self, p: Project) -> None:
        p.updated_at = time.time()
        tmp = self._path(p.id).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(p), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path(p.id))

    def get(self, pid: str) -> Project:
        path = self._path(pid)
        if not path.exists():
            raise KeyError(pid)
        return Project(**json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[Project]:
        out = []
        for path in self.root.glob("*.json"):
            try:
                out.append(self.get(path.stem))
            except (KeyError, json.JSONDecodeError, TypeError):
                continue
        return sorted(out, key=lambda p: p.updated_at, reverse=True)

    def delete(self, pid: str) -> None:
        self._path(pid).unlink(missing_ok=True)
