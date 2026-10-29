"""手元の資料を探す（Research と意味検索）。

対象は成果物ライブラリの文書（テキスト・PDF・Office）。インターネットは使わない。
- キーワード検索（BM25）はいつでも使える
- 意味検索は、埋め込みモデル（machine.toml の [embedding]）が有効なときだけ足す。二つの順位を RRF でまとめる
文書の文字と埋め込みは index フォルダにためておき、同じ版をもう一度読まない。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .documents import DOC_SUFFIXES, DocumentError, _terms, chunks, extract_text
from .library import Library

QUERY_PREFIX = "Instruct: Given a question, retrieve passages that answer it\nQuery: "
MAX_DOCS = 2000


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class Hit:
    ref: str
    title: str
    text: str
    score: float

    def summary(self) -> dict:
        return {"ref": self.ref, "title": self.title, "text": self.text, "score": round(self.score, 4)}


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class DocIndex:
    def __init__(self, library: Library, root: Path, embedder: Embedder | None = None):
        self.library = library
        self.root = root
        self.embedder = embedder
        (root / "text").mkdir(parents=True, exist_ok=True)
        (root / "emb").mkdir(parents=True, exist_ok=True)

    def documents(self, refs: list[str] | None = None) -> list[tuple[str, str]]:
        """(ref, 題) の一覧。refs を渡すとその中だけ。"""
        if refs is not None:
            out = []
            for r in refs:
                try:
                    a = self.library.get(r)
                except (KeyError, ValueError):
                    continue
                if Path(a.filename).suffix.lower() in DOC_SUFFIXES:
                    out.append((a.ref, a.title))
            return out
        latest: dict[str, tuple[int, str, str]] = {}
        for a in self.library.list():
            if Path(a.filename).suffix.lower() not in DOC_SUFFIXES:
                continue
            if a.id not in latest or a.version > latest[a.id][0]:
                latest[a.id] = (a.version, a.ref, a.title)
        return [(ref, title) for _, ref, title in list(latest.values())[:MAX_DOCS]]

    def text(self, ref: str) -> str:
        cache = self.root / "text" / f"{ref.replace('@', '_')}.txt"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        try:
            text = extract_text(self.library.path(self.library.get(ref)))
        except (DocumentError, OSError, KeyError, ValueError):
            text = ""
        cache.write_text(text, encoding="utf-8")
        return text

    async def _vectors(self, ref: str, pieces: list[str]) -> list[list[float]]:
        cache = self.root / "emb" / f"{ref.replace('@', '_')}.json"
        key = hashlib.sha256("\n\0".join(pieces).encode()).hexdigest()
        if cache.exists():
            d = json.loads(cache.read_text(encoding="utf-8"))
            if d.get("key") == key:
                return d["vectors"]
        vecs: list[list[float]] = []
        for i in range(0, len(pieces), 16):
            vecs += await self.embedder.embed(pieces[i:i + 16])  # type: ignore[union-attr]
        cache.write_text(json.dumps({"key": key, "vectors": vecs}), encoding="utf-8")
        return vecs

    async def search(self, query: str, refs: list[str] | None = None, limit: int = 8) -> tuple[list[Hit], str]:
        docs = self.documents(refs)
        pieces: list[tuple[str, str, str]] = []  # (ref, title, chunk)
        for ref, title in docs:
            pieces += [(ref, title, c) for c in chunks(self.text(ref))]
        if not pieces or not query.strip():
            return [], "bm25"
        # キーワード（BM25）
        from collections import Counter
        tfs = [Counter(_terms(c)) for _, _, c in pieces]
        df = Counter(t for tf in tfs for t in tf)
        avg = sum(sum(tf.values()) for tf in tfs) / len(tfs) or 1
        q = set(_terms(query))
        n = len(pieces)

        def bm25(tf) -> float:
            dl = sum(tf.values()) or 1
            return sum(math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) * tf[t] * 2.2
                       / (tf[t] + 1.2 * (0.25 + 0.75 * dl / avg)) for t in q if t in tf)

        kw = [bm25(tf) for tf in tfs]
        ranks = [sorted(range(n), key=lambda i: kw[i], reverse=True)]
        mode = "bm25"
        # 意味（埋め込み）
        if self.embedder is not None:
            try:
                qv = (await self.embedder.embed([QUERY_PREFIX + query]))[0]
                sem = []
                by_ref: dict[str, list[int]] = {}
                for i, (ref, _, _) in enumerate(pieces):
                    by_ref.setdefault(ref, []).append(i)
                sem = [0.0] * n
                for ref, idx in by_ref.items():
                    vecs = await self._vectors(ref, [pieces[i][2] for i in idx])
                    for i, v in zip(idx, vecs):
                        sem[i] = _cos(qv, v)
                ranks.append(sorted(range(n), key=lambda i: sem[i], reverse=True))
                mode = "hybrid"
            except Exception:  # 埋め込みが使えなくてもキーワードで続ける
                mode = "bm25"
        # 順位をまとめる（Reciprocal Rank Fusion）
        fused = [0.0] * n
        for order in ranks:
            for r, i in enumerate(order):
                fused[i] += 1.0 / (60 + r + 1)
        if mode == "bm25":
            best = [i for i in sorted(range(n), key=lambda i: kw[i], reverse=True) if kw[i] > 0]
        else:
            best = sorted(range(n), key=lambda i: fused[i], reverse=True)
        return [Hit(pieces[i][0], pieces[i][1], pieces[i][2], fused[i]) for i in best[:limit]], mode


class FakeEmbedder:
    """テスト用：文字の 2-gram を数える小さなベクトル。"""

    dims = 256

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            t = t.removeprefix(QUERY_PREFIX)
            v = [0.0] * self.dims
            for g in _terms(t):
                v[int(hashlib.md5(g.encode()).hexdigest(), 16) % self.dims] += 1
            out.append(v)
        return out
