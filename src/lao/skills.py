"""スキル：名前と「いつ使うか」の説明が付いた手順書（SKILL.md）。

対話モデルには普段は説明だけを見せ、必要なときに use_skill で本文を読み込ませる。
本文は利用者が書いた手順として扱うが、システムの決まり（外部送信しない等）より優先しない。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY = 20000


class SkillError(ValueError):
    pass


@dataclass
class Skill:
    name: str
    description: str
    body: str
    enabled: bool = True

    def to_markdown(self) -> str:
        return (f"---\nname: {self.name}\ndescription: {self.description}\n"
                f"enabled: {'true' if self.enabled else 'false'}\n---\n\n{self.body.strip()}\n")

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description, "enabled": self.enabled}


def parse(text: str) -> Skill:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        raise SkillError("先頭に --- で囲んだ name と description が必要です")
    meta = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep:
            meta[k.strip()] = v.strip().strip('"')
    return Skill(meta.get("name", ""), meta.get("description", ""), m.group(2).strip(),
                 meta.get("enabled", "true").lower() != "false")


def check(skill: Skill) -> None:
    if not NAME_RE.match(skill.name):
        raise SkillError("名前は半角の小文字・数字・ハイフンで 64 文字までです（例：meeting-notes）")
    if not skill.description.strip() or len(skill.description) > 500 or "\n" in skill.description:
        raise SkillError("説明（いつ使うか）を 1 行・500 文字以内で書いてください")
    if not skill.body.strip():
        raise SkillError("本文（手順）が空です")
    if len(skill.body) > MAX_BODY:
        raise SkillError(f"本文が長すぎます（最大 {MAX_BODY} 文字）")


FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")
FILE_SUFFIXES = {".py", ".txt", ".md", ".csv", ".tsv", ".json"}
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 30


class SkillStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise KeyError(name)
        return self.root / name / "SKILL.md"

    def list(self) -> list[Skill]:
        out = []
        for p in sorted(self.root.glob("*/SKILL.md")):
            try:
                out.append(parse(p.read_text(encoding="utf-8")))
            except (SkillError, OSError):
                continue
        return out

    def enabled(self) -> list[Skill]:
        return [s for s in self.list() if s.enabled]

    def get(self, name: str) -> Skill:
        p = self._path(name)
        if not p.exists():
            raise KeyError(name)
        return parse(p.read_text(encoding="utf-8"))

    def save(self, skill: Skill, rename_from: str | None = None) -> Skill:
        check(skill)
        if rename_from and rename_from != skill.name:
            if self._path(skill.name).exists():
                raise SkillError(f"スキル {skill.name} は既にあります")
            old = self._path(rename_from).parent
            if old.exists():
                old.rename(self._path(skill.name).parent)  # 付けたファイルごと移す
        p = self._path(skill.name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(skill.to_markdown(), encoding="utf-8")
        return skill

    def delete(self, name: str) -> None:
        shutil.rmtree(self._path(name).parent, ignore_errors=True)

    # ---------------------------------------------------------- 付けたファイル（コード実行の箱で使う）
    def files_dir(self, name: str) -> Path:
        return self._path(name).parent / "files"

    def files(self, name: str) -> list[dict]:
        d = self.files_dir(name)
        return [{"name": p.name, "size": p.stat().st_size} for p in sorted(d.iterdir()) if p.is_file()] if d.is_dir() else []

    def save_file(self, name: str, filename: str, data: bytes) -> None:
        if not self._path(name).exists():
            raise KeyError(name)
        if not FILE_RE.match(filename) or Path(filename).suffix.lower() not in FILE_SUFFIXES:
            raise SkillError(f"付けられるのは {', '.join(sorted(FILE_SUFFIXES))} のファイルです（名前は英数字・._-）")
        if len(data) > MAX_FILE_BYTES:
            raise SkillError("ファイルは 1MB までです")
        d = self.files_dir(name)
        if not (d / filename).exists() and len(self.files(name)) >= MAX_FILES:
            raise SkillError(f"ファイルは {MAX_FILES} 個までです")
        d.mkdir(exist_ok=True)
        (d / filename).write_bytes(data)

    def delete_file(self, name: str, filename: str) -> None:
        if not FILE_RE.match(filename):
            raise KeyError(filename)
        (self.files_dir(name) / filename).unlink(missing_ok=True)

