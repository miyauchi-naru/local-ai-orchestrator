"""PC のフォルダと定期実行。"""

import asyncio
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from lao.backends.fake import fake_engine_factory
from lao.folders import Folder, FolderError, Folders
from lao.schedule import ScheduleError, Task, check, next_run
from lao.server import create_app
from lao.service import Service


@pytest.fixture
def svc(machine, catalog_dir):
    return Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0))


@pytest.fixture
def client(svc):
    with TestClient(create_app(svc, "k")) as c:
        c.headers["Authorization"] = "Bearer k"
        c.post("/api/environment", json={"env": "e1"})
        yield c


def _send(client, cid, text):
    with client.stream("POST", f"/api/conversations/{cid}/messages", json={"text": text}) as r:
        assert r.status_code == 200
        return [json.loads(x) for x in r.iter_lines() if x]


def test_folders_stay_inside(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("x")
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    (root / "link").symlink_to(outside)
    f = Folders([Folder(str(root), write=False)], tmp_path / "trash")
    assert {x["path"].split("/")[-1] for x in f.list(str(root))} == {"sub", "a.txt"}  # リンクは出さない
    for bad in ("../secret.txt", str(outside), str(root / "link"), "/etc/passwd"):
        with pytest.raises(FolderError):
            f.resolve(bad)
    with pytest.raises(FolderError):
        f.write_text(str(root / "b.txt"), "y")  # 読むだけの許可
    w = Folders([Folder(str(root), write=True)], tmp_path / "trash")
    w.write_text("b.txt", "y")
    with pytest.raises(FolderError):
        w.write_text("b.txt", "z")  # 上書きは明示したときだけ
    moved = w.move("b.txt", "sub")
    assert moved.endswith("sub/b.txt")
    trashed = w.remove("sub/b.txt")
    assert (tmp_path / "trash") in __import__("pathlib").Path(trashed).parents
    with pytest.raises(FolderError):
        w.remove(str(root))


def test_folder_tools_and_approval(client, svc, tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "memo.md").write_text("会議は火曜")
    assert client.put("/api/settings", json={"folders": [{"path": str(tmp_path / "nope")}]}).status_code == 400
    client.put("/api/settings", json={"folders": [{"path": str(root), "write": True}]})
    cid = client.post("/api/conversations").json()["id"]
    events = _send(client, cid, "フォルダを見て")
    assert any("フォルダを見ました" in e.get("text", "") for e in events)
    events = _send(client, cid, "フォルダにメモを書いて")
    acts = [e["data"] for e in events if e["type"] == "action"]
    assert len(acts) == 1 and acts[0]["status"] == "pending"
    assert not (root / "メモ.txt").exists()  # 承認前は書かない
    r = client.post(f"/api/conversations/{cid}/actions/{acts[0]['id']}", json={"approve": True}).json()
    assert r["status"] == "done" and (root / "メモ.txt").read_text() == "ダミーのメモ"
    assert client.post(f"/api/conversations/{cid}/actions/{acts[0]['id']}", json={"approve": True}).status_code == 409
    turn = client.get(f"/api/conversations/{cid}").json()["turns"][-1]
    assert turn["actions"][0]["status"] == "done"
    # 許可を外すと、承認しても書かない
    events = _send(client, cid, "フォルダにメモを書いて")
    aid = [e["data"] for e in events if e["type"] == "action"][0]["id"]
    client.put("/api/settings", json={"folders": []})
    assert client.post(f"/api/conversations/{cid}/actions/{aid}", json={"approve": True}).json()["status"] == "failed"


def test_next_run_rules():
    t = Task("1", "n", "p", "e1", "c", kind="weekdays", time="08:00")
    fri_evening = datetime(2026, 10, 30, 20, 0)  # 金曜
    assert datetime.fromtimestamp(next_run(t, fri_evening)) == datetime(2026, 11, 2, 8, 0)  # 月曜
    t.kind, t.weekday = "weekly", 2
    assert datetime.fromtimestamp(next_run(t, fri_evening)).weekday() == 2
    t.kind, t.date = "once", "2026-10-01"
    assert next_run(t, fri_evening) == 0
    for bad in (dict(time="25:00"), dict(kind="interval", every_minutes=5), dict(kind="x")):
        with pytest.raises(ScheduleError):
            check(Task("1", "n", "p", "e1", "c", **bad))


def test_schedule_from_conversation_and_run(client, svc):
    cid = client.post("/api/conversations").json()["id"]
    events = _send(client, cid, "毎朝まとめて")
    aid = [e["data"] for e in events if e["type"] == "action"][0]["id"]
    assert client.get("/api/tasks").json()["items"] == []
    client.post(f"/api/conversations/{cid}/actions/{aid}", json={"approve": True})
    items = client.get("/api/tasks").json()["items"]
    assert items[0]["name"] == "朝のまとめ" and items[0]["label"] == "毎日 08:00" and items[0]["next_run"] > 0
    # 時刻が来たら新しい会話に結果を残す
    done = asyncio.run(svc.run_due(now=items[0]["next_run"] + 1))
    assert done and done[0].last_status == "実行しました" and done[0].unread
    conv = client.get(f"/api/conversations/{done[0].last_conversation}").json()
    assert conv["title"].startswith("⏰ 朝のまとめ") and conv["turns"][1]["content"]
    # 環境が違えば実行しない
    client.post("/api/environment", json={"env": "e2"})
    t = client.post(f"/api/tasks/{items[0]['id']}/run").json()
    assert "実行しませんでした" in t["last_status"]
    assert client.post("/api/tasks", json={"name": "x", "prompt": "y", "kind": "daily", "time": "9:00"}).status_code == 400
    assert client.patch(f"/api/tasks/{items[0]['id']}", json={"enabled": False}).json()["next_run"] == 0
    client.delete(f"/api/tasks/{items[0]['id']}")
    assert client.get("/api/tasks").json()["items"] == []


def test_incognito_cannot_schedule(client):
    cid = client.post("/api/conversations", json={"incognito": True}).json()["id"]
    events = _send(client, cid, "毎朝まとめて")
    assert not [e for e in events if e["type"] == "action"]
