"""文書の添付（テキスト・PDF）から本文を取り出す。中身はデータとして扱い、命令としては扱わない。"""

from __future__ import annotations

from pathlib import Path

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".xml", ".html",
                 ".htm", ".css", ".js", ".ts", ".py", ".ps1", ".sh", ".bat", ".c", ".cpp", ".h", ".cs", ".java",
                 ".go", ".rs", ".sql", ".log", ".ini", ".srt", ".vtt"}
DOC_SUFFIXES = TEXT_SUFFIXES | {".pdf"}
MAX_CHARS = 100_000


class DocumentError(ValueError):
    pass


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:  # pragma: no cover
            raise DocumentError("PDF を読むには pypdf が必要です（pip install pypdf）") from e
        try:
            reader = PdfReader(str(path))
            pages = [p.extract_text() or "" for p in reader.pages]
        except Exception as e:  # pypdf は壊れたファイルで様々な例外を出す
            raise DocumentError(f"PDF を読めませんでした：{e}") from e
        text = "\n\n".join(f"[{i} ページ]\n{t.strip()}" for i, t in enumerate(pages, 1) if t.strip())
        if not text:
            raise DocumentError("PDF から文字を取り出せませんでした（画像だけの PDF は画像として添付してください）")
        return text
    if suffix in TEXT_SUFFIXES:
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp932"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    raise DocumentError(f"{suffix} は文書として読めません")


def clip(text: str, limit: int = MAX_CHARS) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)
