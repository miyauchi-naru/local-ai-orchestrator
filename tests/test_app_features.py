"""Claude アプリ相当の機能（プロジェクト・設定・スキル・コマンド・プラグイン・メモリ・会話の操作）。"""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from lao.backends.fake import fake_engine_factory
from lao.documents import best_chunks, extract_text
from lao.plugins import PluginError, PluginStore
from lao.server import create_app
from lao.service import Service


@pytest.fixture
def svc(machine, catalog_dir):
    return Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0))


@pytest.fixture
def client(svc):
    with TestClient(create_app(svc, "k")) as c:
        c.headers["Authorization"] = "Bearer k"
        yield c


def _send(client, cid, **body):
    with client.stream("POST", f"/api/conversations/{cid}/messages", json=body) as r:
        assert r.status_code == 200, r.read()
        return [json.loads(line) for line in r.iter_lines() if line]


def _text(events):
    return "".join(e["text"] for e in events if e["type"] == "text")


def _conv(client, env="e1", **body):
    client.post("/api/environment", json={"env": env})
    return client.post("/api/conversations", json=body).json()["id"]


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


SKILL = "---\nname: haiku\ndescription: 俳句を作る\n---\n\n五七五で書く。\n"


def test_streaming_thinking_and_usage(client):
    cid = _conv(client)
    events = _send(client, cid, text="よく考えて答えて")
    kinds = {e["type"] for e in events}
    assert {"usage", "thinking", "text", "done"} <= kinds
    turn = client.get(f"/api/conversations/{cid}").json()["turns"][1]
    assert turn["thinking"] and turn["content"] == _text(events)


def test_regenerate_edit_branch_export(client):
    cid = _conv(client)
    _send(client, cid, text="一つ目")
    _send(client, cid, text="二つ目")
    _send(client, cid, regenerate=True)
    c = client.get(f"/api/conversations/{cid}").json()
    assert [t["role"] for t in c["turns"]] == ["user", "assistant"] * 2
    _send(client, cid, text="一つ目（直し）", edit_turn=0)
    c = client.get(f"/api/conversations/{cid}").json()
    assert len(c["turns"]) == 2 and c["turns"][0]["content"] == "一つ目（直し）"
    b = client.post(f"/api/conversations/{cid}/branch", json={"upto": 0}).json()
    assert len(client.get(f"/api/conversations/{b['id']}").json()["turns"]) == 1
    assert client.post(f"/api/conversations/{cid}/branch", json={"upto": 99}).status_code == 400
    md = client.get(f"/api/conversations/{cid}/export").text
    assert "一つ目（直し）" in md
    assert client.get("/api/conversations", params={"q": "直し"}).json()["items"]


def test_pin_and_incognito_not_saved(client, svc):
    cid = _conv(client)
    _send(client, cid, text="ふつう")
    assert client.patch(f"/api/conversations/{cid}", json={"pinned": True}).json()["pinned"]
    gid = _conv(client, incognito=True)
    _send(client, gid, text="これを覚えて：秘密")
    assert not list(svc.conversations.root.glob(f"{gid}*"))
    assert all("秘密" not in m.text for m in svc.memory.list())
    assert gid not in [c["id"] for c in client.get("/api/conversations").json()["items"]]


def test_memory_remember_and_manage(client):
    cid = _conv(client)
    events = _send(client, cid, text="これを覚えて：猫が好き")
    assert any(e["type"] == "progress" and "覚えました" in e["text"] for e in events)
    items = client.get("/api/memory").json()["items"]
    assert any("猫が好き" in m["text"] for m in items)
    mid = items[0]["id"]
    assert client.patch(f"/api/memory/{mid}", json={"text": "犬が好き"}).json()["text"] == "犬が好き"
    assert client.post("/api/memory", json={"import": "- 一\n- 二\n"}).json()["imported"] == 2
    assert "- 二" in client.get("/api/memory/export").text
    client.delete("/api/memory/all")
    assert client.get("/api/memory").json()["items"] == []
    # メモリを切ると覚えない
    client.put("/api/settings", json={"memory_enabled": False})
    _send(client, cid, text="これを覚えて：鳥")
    assert client.get("/api/memory").json()["items"] == []


def test_settings_validation(client):
    s = client.get("/api/settings").json()
    assert "concise" in s["styles"]
    assert client.put("/api/settings", json={"style": "concise", "about_me": "開発者"}).json()["style"] == "concise"
    assert client.put("/api/settings", json={"style": "nope"}).status_code == 400
    assert client.put("/api/settings", json={"max_tokens": 10}).status_code == 400
    r = client.put("/api/settings", json={"custom_styles": [{"name": "関西", "text": "関西弁で"}], "style": "custom:関西"})
    assert r.status_code == 200 and r.json()["style"] == "custom:関西"


def test_projects_instructions_and_knowledge(client, tmp_path):
    client.post("/api/environment", json={"env": "e1"})
    up = client.post("/api/upload", files={"file": ("memo.txt", "合言葉はあおぞら".encode())}).json()
    ref = up["ref"]
    p = client.post("/api/projects", json={"name": "企画", "instructions": "必ず敬語", "knowledge": [ref]}).json()
    assert client.post("/api/projects", json={"knowledge": ["000000000000@1"]}).status_code == 400
    cid = client.post("/api/conversations", json={"project_id": p["id"]}).json()["id"]
    _send(client, cid, text="合言葉は？")
    assert client.get("/api/projects").json()["items"][0]["conversations"] == 1
    client.delete(f"/api/projects/{p['id']}")
    assert client.get(f"/api/conversations/{cid}").json()["project_id"] == ""


def test_skills_commands_agents(client):
    assert client.put("/api/skills/haiku", json={"description": "俳句を作る", "body": "五七五"}).status_code == 200
    assert client.put("/api/skills/Bad Name", json={"description": "x", "body": "y"}).status_code == 400
    cid = _conv(client)
    events = _send(client, cid, text="スキルで俳句を")
    assert any("スキル「haiku」" in e.get("text", "") for e in events)
    # コマンド：/名前 で本文に展開
    assert client.put("/api/commands/greet", json={"description": "挨拶", "body": "$ARGUMENTS さんに挨拶して"}).status_code == 200
    _send(client, cid, text="/greet 山田")
    turns = client.get(f"/api/conversations/{cid}").json()["turns"]
    assert turns[-2]["content"] == "山田 さんに挨拶して" and turns[-2]["command"] == "greet"
    names = [c["name"] for c in client.get("/api/commands").json()["items"]]
    assert "greet" in names and "要約" in names
    # サブエージェント
    assert client.put("/api/agents/researcher", json={"description": "調べる", "body": "根拠を書く"}).status_code == 200
    events = _send(client, cid, text="この調べものを任せて")
    assert "サブエージェントの結果" in _text(events)


def test_plugins_zip(client):
    data = _zip({"p/plugin.json": json.dumps({"name": "kit", "description": "道具"}),
                 "p/skills/haiku/SKILL.md": SKILL, "p/commands/hi.md": "---\ndescription: 挨拶\n---\nこんにちは",
                 "p/agents/rev.md": "---\ndescription: 見直し\n---\n誤りを探す"})
    r = client.post("/api/plugins", files={"file": ("kit.zip", data)})
    assert r.status_code == 200 and r.json()["skills"][0]["name"] == "haiku"
    assert client.post("/api/plugins", files={"file": ("kit.zip", data)}).status_code == 400  # 既にある
    assert client.post("/api/plugins?replace=true", files={"file": ("kit.zip", data)}).status_code == 200
    assert "kit:hi" in [c["name"] for c in client.get("/api/commands").json()["items"]]
    assert "kit:haiku" in [s["name"] for s in client.get("/api/skills").json()["items"]]
    client.patch("/api/plugins/kit", json={"enabled": False})
    assert "kit:hi" not in [c["name"] for c in client.get("/api/commands").json()["items"]]
    client.delete("/api/plugins/kit")
    assert client.get("/api/plugins").json()["items"] == []
    # 書き出したものはそのまま取り込める
    client.put("/api/skills/haiku", json={"description": "俳句", "body": "五七五"})
    z = client.post("/api/plugins/export", json={"name": "mine"}).content
    assert client.post("/api/plugins", files={"file": ("mine.zip", z)}).json()["name"] == "mine"


@pytest.mark.parametrize("files", [
    {"plugin.json": '{"name": "x"}', "hooks/run.sh": "curl evil"},
    {"plugin.json": '{"name": "x"}', "skills/../../a/SKILL.md": SKILL},
    {"plugin.json": '{"name": "x"}', "commands/a.py": "print()"},
    {"plugin.json": '{"name": "Bad Name"}'},
    {"skills/a/SKILL.md": SKILL},
])
def test_plugin_rejects_programs_and_bad_paths(tmp_path, files):
    with pytest.raises(PluginError):
        PluginStore(tmp_path).install_zip(_zip(files))
    assert not any(tmp_path.iterdir())


def test_documents_office_and_excerpts(client, tmp_path):
    from docx import Document
    d = Document()
    d.add_paragraph("社内規程の本文")
    path = tmp_path / "a.docx"
    d.save(path)
    assert "社内規程" in extract_text(path)
    client.post("/api/environment", json={"env": "e1"})
    assert client.post("/api/upload", files={"file": ("a.docx", path.read_bytes())}).status_code == 200
    assert client.post("/api/upload", files={"file": ("b.docx", b"junk")}).status_code == 400
    docs = [("a", "りんごの話。" * 200 + "\n" + "合言葉はあおぞらです。" + "\n" + "みかんの話。" * 200)]
    picked = best_chunks(docs, "合言葉は何？", 900)
    assert any("あおぞら" in c for _, c in picked)
    assert sum(len(c) for _, c in picked) <= 900


def test_library_text_and_speak(client):
    client.post("/api/environment", json={"env": "e1"})
    r = client.post("/api/library/text", json={"text": "<p>x</p>", "ext": ".html", "title": "ページ"})
    assert r.status_code == 200
    ref = r.json()["ref"]
    v2 = client.post("/api/library/text", json={"text": "<p>y</p>", "ext": ".html", "parent": ref}).json()
    assert v2["ref"] != ref
    assert client.post("/api/library/text", json={"text": "x", "ext": ".exe"}).status_code == 400
    assert client.post("/api/speak", json={"text": "こんにちは"}).status_code == 400  # E1 は読み上げなし
    client.post("/api/environment", json={"env": "e3"})
    job = client.post("/api/speak", json={"text": "こんにちは"}).json()["job"]
    assert client.get(f"/api/jobs/{job}").status_code == 200


def test_create_office_file_in_conversation(client):
    cid = _conv(client)
    events = _send(client, cid, text="Wordで報告書を作って")
    refs = [r for e in events if e["type"] == "artifacts" for r in e["data"]["refs"]]
    assert len(refs) == 1
    item = next(i for i in client.get("/api/library").json()["items"] if i["ref"] == refs[0])
    assert item["filename"].endswith(".docx") and item["title"] == "ダミー資料.docx"
    assert "見出し" in client.get(f"/api/preview/{refs[0]}").json()["text"]
    # 直すと同じ成果物の新しい版になる
    events = _send(client, cid, text=f"{refs[0]} を直して、PDFで")
    new = [r for e in events if e["type"] == "artifacts" for r in e["data"]["refs"]]
    assert new and new[0].split("@")[0] == refs[0].split("@")[0] and new[0] != refs[0]
    assert client.get("/api/preview/000000000000@1").status_code == 404
