"""成果物の表示部品（/vendor）と、成果物からの AI 問い合わせ（/api/complete）。"""

import hashlib
import json
from importlib import resources

import pytest
from fastapi.testclient import TestClient

from lao.backends.fake import fake_engine_factory
from lao.server import create_app
from lao.service import Service


@pytest.fixture
def client(machine, catalog_dir):
    svc = Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0))
    with TestClient(create_app(svc, "k")) as c:
        yield c


def _vendor():
    return resources.files("lao").joinpath("web", "vendor")


def test_vendor_files_match_manifest():
    manifest = json.loads(_vendor().joinpath("manifest.json").read_text(encoding="utf-8"))
    assert {"react.js", "react-dom.js", "babel.js", "recharts.js", "chart.js", "d3.js", "mermaid.js"} <= set(manifest)
    for name, m in manifest.items():
        data = _vendor().joinpath(name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == m["sha256"], name
        assert m["license"] in ("MIT", "ISC"), name
        assert _vendor().joinpath("licenses", f"{name}.txt").is_file(), name


def test_vendor_route_serves_only_listed_files(client):
    r = client.get("/vendor/react.js")  # 部品は鍵なしで読める（成果物の箱の中から読むため）
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.headers["x-content-type-options"] == "nosniff"
    for bad in ("manifest.json", "licenses%2Freact.js.txt", "..%2Findex.html", "nope.js"):
        assert client.get(f"/vendor/{bad}").status_code == 404
    assert r.headers["access-control-allow-origin"] == "*"


def test_complete_from_artifact(client):
    h = {"Authorization": "Bearer k"}
    assert client.post("/api/complete", json={"prompt": "こんにちは"}).status_code == 401
    assert client.post("/api/complete", json={"prompt": "こんにちは"}, headers=h).status_code == 409
    client.post("/api/environment", json={"env": "e1"}, headers=h)
    r = client.post("/api/complete", json={"prompt": "この数字を一言で"}, headers=h)
    assert r.status_code == 200 and "この数字を一言で" in r.json()["text"]
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    assert client.post("/api/complete", json={"messages": msgs}, headers=h).status_code == 200
    for bad in ({"prompt": ""}, {"messages": [{"role": "system", "content": "x"}]}, {"prompt": "x" * 40001},
                {"messages": [{"role": "user", "content": ["x"]}]}):
        assert client.post("/api/complete", json=bad, headers=h).status_code == 400


def test_prompt_mentions_local_libraries():
    from lao.orchestrator import ARTIFACT_GUIDE
    assert "recharts" in ARTIFACT_GUIDE and "window.claude.complete" in ARTIFACT_GUIDE and "mermaid" in ARTIFACT_GUIDE
