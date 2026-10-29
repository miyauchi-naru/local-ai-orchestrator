"""実機の設定ファイル（machine.toml）を、コメントを残したまま書き換える。

  lao configure --auto           … ffmpeg・llama-server の場所と固定の API キーを自動で埋める
  lao configure --set a.b=値     … 値を一つずつ書く
  lao configure --apply-proposed … lao p0 propose の測定値（GPU 上限・ピーク・打ち切り時間）を採用する

導入スクリプト（scripts/setup-spark.ps1）から呼ぶ。書き換えた後は TOML として読めるか確かめ、
読めなければ書き込まない。
"""

from __future__ import annotations

import re
import secrets
import shutil
import tomllib
from pathlib import Path
from typing import Any, Callable

_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")


def toml_literal(value: Any) -> str:
    """Python の値を TOML の 1 行の値にする（文字列・数・真偽・リスト・表）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {toml_literal(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"TOML にできない値です：{value!r}")


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else toml_literal(k)


def parse_value(text: str) -> Any:
    """コマンドラインの値。TOML の値として読めればそれを、読めなければ文字列として扱う。"""
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


def set_value(text: str, section: str, key: str, value: Any) -> str:
    """[section] の key の行を置き換える。無ければその表の最後に足し、表も無ければ末尾に作る。

    複数行にまたがる値（[engines.comfyui] の command など）は書き換えない。
    """
    lines = text.splitlines()
    line = f"{_key(key)} = {toml_literal(value)}"
    key_re = re.compile(rf"^\s*{re.escape(_key(key))}\s*=")
    start = end = None
    for i, raw in enumerate(lines):
        m = _HEADER.match(raw)
        if m:
            if start is not None:
                end = i
                break
            if m.group(1).strip() == section:
                start = i
    if start is None:
        lines += ["", f"[{section}]", line]
        return "\n".join(lines) + "\n"
    end = len(lines) if end is None else end
    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            old = lines[i].split("=", 1)[1]
            if old.count("[") > old.count("]"):
                raise ValueError(f"[{section}] {key} は複数行の値なので、手で直してください")
            comment = re.search(r"\s+#[^\"']*$", old)
            lines[i] = line + (comment.group(0) if comment else "")
            return "\n".join(lines) + "\n"
    # 表の最後の空でない行の後ろに足す
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    lines.insert(at, line)
    return "\n".join(lines) + "\n"


def _get(raw: dict, section: str, key: str) -> Any:
    d: Any = raw
    for part in section.split("."):
        d = d.get(part, {}) if isinstance(d, dict) else {}
    return d.get(key) if isinstance(d, dict) else None


def auto_values(raw: dict, which: Callable[[str], str | None] = shutil.which,
                exists: Callable[[str], bool] = lambda p: Path(p).exists()) -> list[tuple[str, str, Any, str]]:
    """実機で決まる値のうち、自動で埋められるもの。（表, 鍵, 値, 理由）の一覧を返す。"""
    out: list[tuple[str, str, Any, str]] = []
    for key, exe in (("binary", "ffmpeg"), ("ffprobe", "ffprobe")):
        cur = _get(raw, "engines.ffmpeg", key) or ""
        if not cur or not exists(cur):
            found = which(exe)
            if found:
                out.append(("engines.ffmpeg", key, Path(found).as_posix(), f"{exe} は {found} にありました"))
    cur = _get(raw, "engines.llama", "binary") or ""
    if not cur or not exists(cur):
        engines = _get(raw, "paths", "engines") or ""
        for cand in (f"{engines}/llama.cpp/llama-server.exe", f"{engines}/llama.cpp/llama-server"):
            if engines and exists(cand):
                out.append(("engines.llama", "binary", cand, "ビルドした llama-server"))
                break
        else:
            found = which("llama-server")
            if found:
                out.append(("engines.llama", "binary", Path(found).as_posix(), f"llama-server は {found} にありました"))
    if not _get(raw, "server", "api_key"):
        # 起動のたびに変わると、メイン PC のブラウザで毎回キーを調べ直すことになるので固定する
        out.append(("server", "api_key", secrets.token_urlsafe(24), "API キーを固定しました（値は machine.toml にあります）"))
    return out


def proposed_values(proposed: dict) -> list[tuple[str, str, Any, str]]:
    """lao p0 propose が作った machine.proposed.toml のうち、測定で決まる値。"""
    out: list[tuple[str, str, Any, str]] = []
    cap = _get(proposed, "memory", "gpu_allocatable_gb")
    if cap:
        out.append(("memory", "gpu_allocatable_gb", cap, "GPU が確保できた量（5% の余裕つき）"))
    wd = _get(proposed, "gpu", "watchdog_seconds")
    if wd:
        out.append(("gpu", "watchdog_seconds", wd, "最も遅い応答から決めた打ち切り時間"))
    for key, m in (proposed.get("measured") or {}).items():
        out.append(("measured", key, m, "実測のピーク・ロード時間・速度"))
    return out


def apply(path: Path, changes: list[tuple[str, str, Any, str]], dry_run: bool = False) -> str:
    text = path.read_text(encoding="utf-8")
    for section, key, value, _ in changes:
        text = set_value(text, section, key, value)
    tomllib.loads(text)  # 壊れていたら書かない
    if not dry_run:
        path.write_text(text, encoding="utf-8")
    return text
