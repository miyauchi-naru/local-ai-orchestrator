"""四環境共通の成果物フォルダ（R10・R17）。

各成果物はファイル本体と、由来情報の JSON（<id>.json）を持つ。修正は新しい版として
保存し、既存のファイルは上書きしない。由来情報はデータとして扱い、命令や権限は持たない。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

KINDS = {"text", "image", "audio", "video"}


@dataclass
class Artifact:
    id: str
    version: int
    kind: str
    mime: str
    filename: str
    sha256: str
    created_at: float
    env_id: str
    configuration: str
    title: str = ""
    parent_id: str | None = None  # 修正元の版
    sources: list[str] = field(default_factory=list)  # 元素材の成果物ID
    generation: dict[str, Any] = field(default_factory=dict)  # モデル・設定・seed 等

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def kind_of(mime: str) -> str:
    top = mime.split("/", 1)[0]
    return top if top in KINDS else "text"


class Library:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _meta_path(self, art_id: str, version: int) -> Path:
        return self.root / art_id / f"v{version}.json"

    def add_file(
        self,
        src: Path,
        *,
        env_id: str,
        configuration: str,
        title: str = "",
        parent: str | None = None,
        sources: list[str] | None = None,
        generation: dict[str, Any] | None = None,
        move: bool = False,
    ) -> Artifact:
        """ファイルを取り込む。parent（id@版）を渡すと同じIDの新しい版になる。"""
        mime = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        if parent:
            art_id = parent.split("@", 1)[0]
            version = self.latest_version(art_id) + 1
        else:
            art_id, version = uuid.uuid4().hex[:12], 1
        folder = self.root / art_id
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"v{version}{src.suffix.lower()}"
        if dest.exists():
            raise FileExistsError(dest)
        (shutil.move if move else shutil.copy2)(str(src), str(dest))
        art = Artifact(
            id=art_id,
            version=version,
            kind=kind_of(mime),
            mime=mime,
            filename=dest.name,
            sha256=_sha256(dest),
            created_at=time.time(),
            env_id=env_id,
            configuration=configuration,
            title=title or src.name,
            parent_id=parent,
            sources=list(sources or []),
            generation=dict(generation or {}),
        )
        self._meta_path(art_id, version).write_text(
            json.dumps(asdict(art), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return art

    def add_text(self, text: str, suffix: str = ".md", **kw) -> Artifact:
        tmp = self.root / f".tmp-{uuid.uuid4().hex}{suffix}"
        tmp.write_text(text, encoding="utf-8")
        return self.add_file(tmp, move=True, **kw)

    def latest_version(self, art_id: str) -> int:
        folder = self.root / art_id
        versions = [int(p.stem[1:]) for p in folder.glob("v*.json")]
        return max(versions, default=0)

    def get(self, ref: str) -> Artifact:
        """id@版 で取得する。版を省略しても最新版に置き換えない（参照は版に固定する）。"""
        if "@" not in ref:
            raise ValueError("成果物は id@版 で指定してください")
        art_id, v = ref.split("@", 1)
        meta = self._meta_path(art_id, int(v))
        if not meta.exists():
            raise KeyError(f"{ref} は見つからないか削除済みです")
        return Artifact(**json.loads(meta.read_text(encoding="utf-8")))

    def path(self, art: Artifact) -> Path:
        p = self.root / art.id / art.filename
        if _sha256(p) != art.sha256:
            raise ValueError(f"{art.ref} の内容が記録と一致しません")
        return p

    def list(self, kind: str | None = None, env_id: str | None = None) -> list[Artifact]:
        out = []
        for meta in self.root.glob("*/v*.json"):
            a = Artifact(**json.loads(meta.read_text(encoding="utf-8")))
            if kind and a.kind != kind:
                continue
            if env_id and a.env_id != env_id:
                continue
            out.append(a)
        return sorted(out, key=lambda a: a.created_at, reverse=True)
