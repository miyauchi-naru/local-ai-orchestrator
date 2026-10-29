"""手元の資料を探す（キーワードと意味）。"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from lao.backends.fake import fake_engine_factory
from lao.config import MachineConfig, load_embedding
from lao.search import DocIndex, FakeEmbedder
from lao.server import create_app
from lao.service import Service


@pytest.fixture
def svc(machine, catalog_dir):
    return Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0),
                   embedder=FakeEmbedder())


def _add(svc, name, text):
    return svc.library.add_text(text, suffix=".md", env_id="upload", configuration="upload", title=name).ref


def test_hybrid_search_and_cache(svc, tmp_path):
    a = _add(svc, "旅費規程", "出張の日当は一日 3000 円です。\n" + "雑記。" * 300)
    _add(svc, "議事録", "来週の会議は火曜に変更。\n" + "メモ。" * 300)
    hits, mode = asyncio.run(svc.index.search("出張の日当はいくら？"))
    assert mode == "hybrid" and hits[0].ref == a and "3000" in hits[0].text
    assert list((svc.index.root / "emb").iterdir())  # 埋め込みはためておく
    # 範囲を絞る
    hits, _ = asyncio.run(svc.index.search("会議", refs=[a]))
    assert all(h.ref == a for h in hits)
    # 埋め込みがなければキーワードだけ
    idx = DocIndex(svc.library, tmp_path / "i2")
    hits, mode = asyncio.run(idx.search("日当"))
    assert mode == "bm25" and hits[0].ref == a
    assert asyncio.run(idx.search("該当しない言葉xyz"))[0] == []


def test_search_tool_and_research_command(svc):
    _add(svc, "旅費規程", "出張の日当は一日 3000 円です。")
    with TestClient(create_app(svc, "k")) as c:
        c.headers["Authorization"] = "Bearer k"
        c.post("/api/environment", json={"env": "e1"})
        cid = c.post("/api/conversations").json()["id"]
        with c.stream("POST", f"/api/conversations/{cid}/messages", json={"text": "資料で日当を調べて"}) as r:
            events = [json.loads(x) for x in r.iter_lines() if x]
        assert any("資料を探しました（キーワードと意味）" in e.get("text", "") for e in events)
        assert "旅費規程" in "".join(e.get("text", "") for e in events if e["type"] == "text")
        assert c.get("/api/search", params={"q": "日当"}).json()["items"][0]["title"] == "旅費規程"
        assert "リサーチ" in [x["name"] for x in c.get("/api/commands").json()["items"]]


def test_embedding_catalog_and_budget(machine, catalog_dir):
    conf = load_embedding(catalog_dir)
    assert conf and conf.components["embed"].quantization == "none"
    raw = {**machine.raw, "embedding": {"enabled": True, "mem_gb": 2}}
    assert MachineConfig(raw).budget_gb == machine.budget_gb - 2
