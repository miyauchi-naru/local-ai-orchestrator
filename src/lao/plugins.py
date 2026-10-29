"""スラッシュコマンドとプラグイン。

- コマンド：/名前 で呼ぶ決まった依頼文。本文の $ARGUMENTS を、名前の後に書いた文字で置き換える。
- プラグイン：スキルとコマンドをまとめたフォルダ（zip で取り込み・書き出し）。

  plugin.json            {"name", "description", "version"}
  skills/<名前>/SKILL.md
  commands/<名前>.md

プラグインは文章（手順と依頼文）だけを持つ。プログラムは取り込まず、実行もしない（外部送信を防ぐため）。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .skills import NAME_RE, Skill, SkillError, check, parse

COMMAND_RE = re.compile(r"^[\w-]{1,40}$")
MAX_ZIP_FILES = 200
MAX_ZIP_BYTES = 5 * 1024 * 1024


class PluginError(ValueError):
    pass


@dataclass
class Command:
    name: str
    description: str
    body: str
    plugin: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.plugin}:{self.name}" if self.plugin else self.name

    def to_markdown(self) -> str:
        return f"---\ndescription: {self.description}\n---\n\n{self.body.strip()}\n"

    def summary(self) -> dict:
        return {"name": self.full_name, "description": self.description, "plugin": self.plugin, "body": self.body}

    def expand(self, arguments: str) -> str:
        body = self.body.strip()
        if "$ARGUMENTS" in body:
            return body.replace("$ARGUMENTS", arguments.strip())
        return f"{body}\n\n{arguments.strip()}".strip()


def parse_command(name: str, text: str, plugin: str = "") -> Command:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    desc, body = "", text
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip() == "description":
                desc = v.strip().strip('"')
    return Command(name, desc, body.strip(), plugin)


def check_command(c: Command) -> None:
    if not COMMAND_RE.match(c.name):
        raise PluginError("コマンド名は文字・数字・ハイフン・下線で 40 文字までです（空白や / は使えません）")
    if not c.body.strip():
        raise PluginError("コマンドの本文が空です")
    if len(c.body) > 20000 or len(c.description) > 500 or "\n" in c.description:
        raise PluginError("本文は 20000 文字、説明は 1 行 500 文字までです")


class CommandStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not COMMAND_RE.match(name):
            raise KeyError(name)
        return self.root / f"{name}.md"

    def list(self) -> list[Command]:
        return [parse_command(p.stem, p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("*.md"))
                if COMMAND_RE.match(p.stem)]

    def save(self, c: Command, rename_from: str | None = None) -> Command:
        check_command(c)
        if rename_from and rename_from != c.name:
            if self._path(c.name).exists():
                raise PluginError(f"コマンド /{c.name} は既にあります")
            self.delete(rename_from)
        self._path(c.name).write_text(c.to_markdown(), encoding="utf-8")
        return c

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)


@dataclass
class Plugin:
    name: str
    description: str
    version: str
    enabled: bool
    skills: list[Skill]
    commands: list[Command]

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description, "version": self.version, "enabled": self.enabled,
                "skills": [s.summary() for s in self.skills], "commands": [c.summary() for c in self.commands]}


class PluginStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise KeyError(name)
        return self.root / name

    def _load(self, folder: Path) -> Plugin:
        meta = json.loads((folder / "plugin.json").read_text(encoding="utf-8"))
        state = folder / ".disabled"
        skills = []
        for p in sorted(folder.glob("skills/*/SKILL.md")):
            try:
                skills.append(parse(p.read_text(encoding="utf-8")))
            except SkillError:
                continue
        commands = [parse_command(p.stem, p.read_text(encoding="utf-8"), folder.name)
                    for p in sorted(folder.glob("commands/*.md")) if COMMAND_RE.match(p.stem)]
        return Plugin(folder.name, str(meta.get("description", "")), str(meta.get("version", "")),
                      not state.exists(), skills, commands)

    def list(self) -> list[Plugin]:
        out = []
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
            try:
                out.append(self._load(folder))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def get(self, name: str) -> Plugin:
        folder = self._dir(name)
        if not (folder / "plugin.json").exists():
            raise KeyError(name)
        return self._load(folder)

    def set_enabled(self, name: str, enabled: bool) -> Plugin:
        folder = self._dir(name)
        if not folder.exists():
            raise KeyError(name)
        flag = folder / ".disabled"
        if enabled:
            flag.unlink(missing_ok=True)
        else:
            flag.touch()
        return self._load(folder)

    def delete(self, name: str) -> None:
        shutil.rmtree(self._dir(name), ignore_errors=True)

    def install_zip(self, data: bytes, replace: bool = False) -> Plugin:
        """zip を検証して取り込む。文章ファイル（.md / .json）以外や、フォルダの外を指すパスは拒否する。"""
        if len(data) > MAX_ZIP_BYTES:
            raise PluginError("プラグインが大きすぎます（5MB まで）")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise PluginError("zip ファイルではありません") from e
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if len(names) > MAX_ZIP_FILES:
            raise PluginError("ファイルが多すぎます")
        # 先頭に共通フォルダがあれば外す（plugin-name/plugin.json の形にも対応）
        prefix = ""
        if "plugin.json" not in names:
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) == 1 and f"{next(iter(tops))}/plugin.json" in names:
                prefix = next(iter(tops)) + "/"
        files: dict[str, bytes] = {}
        total = 0
        for n in names:
            rel = n[len(prefix):] if n.startswith(prefix) else n
            parts = rel.split("/")
            ok = (rel == "plugin.json"
                  or (len(parts) == 3 and parts[0] == "skills" and NAME_RE.match(parts[1]) and parts[2] == "SKILL.md")
                  or (len(parts) == 2 and parts[0] == "commands" and parts[1].endswith(".md")
                      and COMMAND_RE.match(parts[1][:-3])))
            if not ok:
                raise PluginError(f"取り込めないファイル：{n}（plugin.json、skills/名前/SKILL.md、commands/名前.md だけ）")
            info = zf.getinfo(n)
            total += info.file_size
            if total > MAX_ZIP_BYTES * 4:
                raise PluginError("展開後のサイズが大きすぎます")
            files[rel] = zf.read(n)
        if "plugin.json" not in files:
            raise PluginError("plugin.json がありません")
        try:
            meta = json.loads(files["plugin.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise PluginError("plugin.json が読めません") from e
        name = str(meta.get("name", ""))
        if not NAME_RE.match(name):
            raise PluginError("plugin.json の name は半角の小文字・数字・ハイフンで書いてください")
        for rel, raw in files.items():
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise PluginError(f"{rel} が UTF-8 ではありません") from e
            if rel.startswith("skills/"):
                check(parse(text))
            elif rel.startswith("commands/"):
                check_command(parse_command(rel.split("/")[1][:-3], text, name))
        folder = self._dir(name)
        if folder.exists():
            if not replace:
                raise PluginError(f"プラグイン {name} は既にあります")
            shutil.rmtree(folder)
        for rel, raw in files.items():
            dest = folder / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        return self._load(folder)


def export_zip(name: str, description: str, skills: list[Skill], commands: list[Command]) -> bytes:
    """自分のスキルとコマンドを 1 つのプラグインとして書き出す。"""
    if not NAME_RE.match(name):
        raise PluginError("プラグイン名は半角の小文字・数字・ハイフンで書いてください")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/plugin.json", json.dumps({"name": name, "description": description, "version": "1.0.0"},
                                                      ensure_ascii=False, indent=2))
        for s in skills:
            zf.writestr(f"{name}/skills/{s.name}/SKILL.md", s.to_markdown())
        for c in commands:
            zf.writestr(f"{name}/commands/{c.name}.md", c.to_markdown())
    return buf.getvalue()
