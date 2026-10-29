"""PC のフォルダを扱う（Claude の Cowork に相当）。

- 使えるのは、設定で許可したフォルダの中だけ。シンボリックリンクなどで外に出るパスは拒否する
- 一覧と読み取りは、許可したフォルダならそのまま行う
- 書き込み・移動・削除は AI が「提案」するだけで、利用者が画面で「実行する」を押したときに行う
- 削除は消さずに、lao のごみ箱フォルダへ移す
"""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

MAX_LIST = 500
MAX_WRITE_CHARS = 2_000_000


class FolderError(ValueError):
    pass


@dataclass
class Folder:
    path: str
    write: bool = False


def parse_folders(raw: list) -> list[Folder]:
    out = []
    for f in raw or []:
        if isinstance(f, dict) and str(f.get("path", "")).strip():
            out.append(Folder(str(f["path"]).strip(), bool(f.get("write", False))))
    return out


class Folders:
    def __init__(self, folders: list[Folder], trash: Path):
        self.folders = folders
        self.trash = trash

    def describe(self) -> list[str]:
        return [f"{f.path}（{'読み書き' if f.write else '読むだけ'}）" for f in self.folders]

    def resolve(self, path: str, *, write: bool = False) -> Path:
        if not self.folders:
            raise FolderError("使えるフォルダがありません（設定 → フォルダ で許可してください）")
        p = Path(str(path)).expanduser()
        for f in self.folders:
            root = Path(f.path).expanduser().resolve()
            cand = (p if p.is_absolute() else root / p).resolve()
            if cand == root or root in cand.parents:
                if write and not f.write:
                    raise FolderError(f"{f.path} は読むだけの許可です")
                return cand
        raise FolderError(f"{path} は許可したフォルダの外です。使えるのは {', '.join(f.path for f in self.folders)}")

    def list(self, path: str, recursive: bool = False) -> list[dict]:
        base = self.resolve(path)
        if not base.is_dir():
            raise FolderError(f"{path} はフォルダではありません")
        it = base.rglob("*") if recursive else base.iterdir()
        out = []
        for p in it:
            if p.name.startswith(".") or p.is_symlink():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"path": str(p), "type": "dir" if p.is_dir() else "file",
                        "size": st.st_size if p.is_file() else None,
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
            if len(out) >= MAX_LIST:
                break
        return sorted(out, key=lambda x: (x["type"] != "dir", x["path"]))

    # ------------------------------------------------------------ 利用者が承認した後に行う操作
    def write_text(self, path: str, content: str, overwrite: bool = False) -> str:
        dest = self.resolve(path, write=True)
        if len(content) > MAX_WRITE_CHARS:
            raise FolderError("内容が大きすぎます")
        if dest.exists() and not overwrite:
            raise FolderError(f"{dest} は既にあります")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return str(dest)

    def copy_in(self, src: Path, path: str, overwrite: bool = False) -> str:
        dest = self.resolve(path, write=True)
        if dest.is_dir():
            dest = dest / src.name
        if dest.exists() and not overwrite:
            raise FolderError(f"{dest} は既にあります")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return str(dest)

    def move(self, src: str, dst: str) -> str:
        a = self.resolve(src, write=True)
        b = self.resolve(dst, write=True)
        if not a.exists():
            raise FolderError(f"{src} がありません")
        if b.is_dir():
            b = b / a.name
        if b.exists():
            raise FolderError(f"{b} は既にあります")
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(a), str(b))
        return str(b)

    def remove(self, path: str) -> str:
        """消さずにごみ箱フォルダへ移す。"""
        a = self.resolve(path, write=True)
        if not a.exists():
            raise FolderError(f"{path} がありません")
        if any(a == Path(f.path).expanduser().resolve() for f in self.folders):
            raise FolderError("許可したフォルダそのものは消せません")
        dest = self.trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}" / a.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(a), str(dest))
        return str(dest)
