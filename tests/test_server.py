import base64

import pytest
from fastapi.testclient import TestClient

from lao.backends.fake import fake_engine_factory
from lao.server import create_app
from lao.service import Service


@pytest.fixture
def client(machine, catalog_dir):
    svc = Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0))
    app = create_app(svc, "k")
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer k"
        yield c


def test_requires_key(machine, catalog_dir):
    app = create_app(Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0)), "k")
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 401


def test_flow_select_chat_generate(client, sample_image):
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 409
    envs = client.get("/api/environments").json()["environments"]
    assert {e["id"] for e in envs} == {"e1", "e2", "e3", "e4"}
    r = client.post("/api/environment", json={"env": "e2"})
    assert r.status_code == 200 and r.json()["environment"]["id"] == "e2"
    r = client.post("/v1/chat/completions", json={"model": "lao-e2", "messages": [{"role": "user", "content": "猫の画像を作って"}]})
    text = r.json()["choices"][0]["message"]["content"]
    assert "/files/" in text
    lib = client.get("/api/library").json()["items"]
    assert lib and lib[0]["kind"] == "image"
    assert client.get(lib[0]["url"]).status_code == 200


def test_no_silent_environment_switch(client):
    client.post("/api/environment", json={"env": "e1"})
    r = client.post("/v1/chat/completions", json={"model": "lao-e4", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 409


def test_data_uri_attachment_ingested_and_streaming(client, sample_image):
    client.post("/api/environment", json={"env": "e1"})
    b64 = base64.b64encode(sample_image.read_bytes()).decode()
    msg = {"role": "user", "content": [{"type": "text", "text": "これは何？"},
                                       {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}
    with client.stream("POST", "/v1/chat/completions", json={"messages": [msg], "stream": True}) as r:
        body = "".join(r.iter_text())
    assert "describe_media" in body and "[DONE]" in body


def test_upload_rejects_corrupt_video(client, tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_bytes(b"junk")
    r = client.post("/api/upload", files={"file": ("x.mp4", bad.read_bytes())})
    assert r.status_code == 400


def test_ui_served(client):
    assert "ローカルAI" in client.get("/").text


def test_pwa_assets_served(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    assert "/api" in client.get("/sw.js").text  # API は保存しない
    assert client.get("/icon.svg").headers["content-type"].startswith("image/svg")


def _send(client, cid, text, attachments=()):
    with client.stream("POST", f"/api/conversations/{cid}/messages",
                       json={"text": text, "attachments": list(attachments)}) as r:
        assert r.status_code == 200
        return [__import__("json").loads(line) for line in r.iter_lines() if line]


def test_conversation_flow(client):
    assert client.post("/api/conversations").status_code == 409  # 環境が未選択
    client.post("/api/environment", json={"env": "e2"})
    cid = client.post("/api/conversations").json()["id"]
    events = _send(client, cid, "猫の画像を作って\n詳しく")
    assert events[-1] == {"type": "done", "title": "猫の画像を作って"}
    refs = [r for e in events if e["type"] == "artifacts" for r in e["data"]["refs"]]
    assert refs
    c = client.get(f"/api/conversations/{cid}").json()
    assert [t["role"] for t in c["turns"]] == ["user", "assistant"]
    assert c["turns"][1]["artifacts"] == refs and c["turns"][1]["progress"]
    # 作った画像の版の一覧
    art_id = refs[0].split("@")[0]
    assert client.get(f"/api/library/{art_id}/versions").status_code == 200
    # 名前の変更・一覧・削除
    assert client.patch(f"/api/conversations/{cid}", json={"title": "猫"}).json()["title"] == "猫"
    assert [x["id"] for x in client.get("/api/conversations").json()["items"]] == [cid]
    client.delete(f"/api/conversations/{cid}")
    assert client.get(f"/api/conversations/{cid}").status_code == 404


def test_conversation_rejects_other_environment_and_bad_input(client):
    client.post("/api/environment", json={"env": "e1"})
    cid = client.post("/api/conversations").json()["id"]
    assert client.post(f"/api/conversations/{cid}/messages", json={"text": ""}).status_code == 400
    r = client.post(f"/api/conversations/{cid}/messages", json={"text": "x", "attachments": ["000000000000@1"]})
    assert r.status_code == 400
    client.post("/api/environment", json={"env": "e2"})
    assert client.post(f"/api/conversations/{cid}/messages", json={"text": "x"}).status_code == 409
    assert client.get("/api/conversations/../x").status_code == 404
