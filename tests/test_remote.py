"""別の PC の Claude Code から使う MCP サーバー（lao mcp）と、その中継先の API。"""

import base64
import io
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from lao.backends.fake import fake_engine_factory
from lao.mcp import TOOL_DEFS, Bridge, format_job, handle, serve_stdio, server_key
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


@pytest.fixture
def bridge(client):
    return Bridge(client)


def call(bridge, tool, **args):
    r = handle(bridge, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": tool, "arguments": args}})
    return r["result"]


def text(result):
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


def jpeg_size(data: bytes) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json", "-"],
                         input=data, capture_output=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


def test_protocol_basics(bridge):
    init = handle(bridge, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert "tools" in init["capabilities"] and "ask_local" in init["instructions"]
    assert handle(bridge, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in handle(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert {"generate_image", "generate_video", "describe_media", "ask_local", "view", "job"} <= names
    assert handle(bridge, {"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601
    # 生成ツールの引数は lao 本体の定義と同じで、待ち時間だけ加わる
    gi = next(t for t in TOOL_DEFS if t["name"] == "generate_image")["inputSchema"]
    assert gi["required"] == ["prompt"] and "wait_seconds" in gi["properties"]


def test_stdio_roundtrip(bridge):
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             "not json",
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})]
    out = io.StringIO()
    serve_stdio(bridge, io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700


def test_switches_environment_for_the_tool(bridge):
    assert "未選択" in text(call(bridge, "status"))
    r = call(bridge, "generate_image", prompt="a cat")  # 未選択なら画像の E2 を選ぶ
    assert not r["isError"] and text(r).startswith("完了")
    assert "e2 に切り替えて実行" in text(r)
    assert "e2" in text(call(bridge, "status"))
    r = call(bridge, "generate_image", prompt="a dog")  # 同じ環境なら切り替えない
    assert "切り替え" not in text(r)
    r = call(bridge, "generate_speech", text="こんにちは")  # 音声は E3 へ
    assert not r["isError"] and "e3 に切り替えて実行" in text(r)


def test_explicit_switch_still_available(bridge):
    r = call(bridge, "switch_environment", env="e2")
    assert not r["isError"] and "generate_image" in text(r)


def test_generate_image_returns_compact_result_and_thumbnail(bridge, svc):
    call(bridge, "switch_environment", env="e2")
    r = call(bridge, "generate_image", prompt="a red fox", seed=7, width=512, height=512)
    t = text(r)
    assert not r["isError"] and t.startswith("完了")
    assert "seed=7" in t and "width=512" in t
    ref = t.split("\n")[1].split()[1]
    art = svc.library.get(ref)
    assert str(svc.library.path(art)) in t
    assert "base64" not in t and len(t) < 400  # 画像そのものは返さない
    v = call(bridge, "view", ref=ref, size=128)["content"][0]
    assert v["type"] == "image" and v["mimeType"] == "image/jpeg"
    assert max(jpeg_size(base64.b64decode(v["data"]))) == 128


def test_switch_waits_for_running_job(machine, catalog_dir):
    slow = Service(machine, catalog_dir, fake_engine_factory(0.3), memory_probe=lambda: (128.0, 128.0))
    with TestClient(create_app(slow, "k")) as c:
        c.headers["Authorization"] = "Bearer k"
        b = Bridge(c)
        t = text(call(b, "generate_image", prompt="x", wait_seconds=0))
        assert "終わっていません" in t
        r = call(b, "generate_speech", text="続けて読み上げ")  # 画像が終わるのを待ってから E3 へ
        assert not r["isError"] and text(r).startswith("完了"), text(r)
        assert all(j.state == "done" for j in slow.jobs.list())


def test_video_via_inbox_and_contact_sheet(bridge, svc, sample_image):
    call(bridge, "switch_environment", env="e4")
    inbox = svc.root / "inbox"
    inbox.mkdir(parents=True)
    shutil.copy(sample_image, inbox / "start.png")
    r = call(bridge, "import_file", name="start.png")
    assert not r["isError"], text(r)
    img_ref = text(r).split()[0].split("：")[1]
    assert not (inbox / "start.png").exists()
    r = call(bridge, "generate_video", image_ref=img_ref, prompt="slow pan", seconds=3)
    t = text(r)
    assert t.startswith("完了") and "actual_seconds=" in t, t
    vid_ref = t.split("\n")[1].split()[1]
    v = call(bridge, "view", ref=vid_ref, size=128)["content"][0]
    w, h = jpeg_size(base64.b64decode(v["data"]))
    assert w > 3 * h  # 4 コマを横に並べた 1 枚


def test_import_rejects_paths_outside_inbox(bridge, svc, tmp_path):
    (svc.root / "inbox").mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("x")
    r = call(bridge, "import_file", name="../secret.txt")
    assert r["isError"] and "ありません" in text(r)


def test_describe_media_answers_in_text(bridge, svc, sample_image):
    call(bridge, "switch_environment", env="e2")
    art = svc.library.add_file(sample_image, env_id="e2", configuration="upload", title="s.png")
    r = call(bridge, "describe_media", ref=art.ref, question="何が写っている？")
    assert not r["isError"] and "ダミー理解" in text(r)


def test_ask_local_returns_text_only(bridge, svc):
    call(bridge, "switch_environment", env="e1")
    doc = svc.library.add_text("会議で予算を決めた。", env_id="e1", configuration="upload", title="memo.md")
    r = call(bridge, "ask_local", task="三行で要約して", text="追加の素材", refs=[doc.ref])
    assert not r["isError"] and "ダミー応答" in text(r)
    r = call(bridge, "ask_local", task="要約", text="あ" * 1_000_000)
    assert r["isError"] and "長すぎます" in text(r)


def test_job_wait_and_cancel(bridge, svc, machine, catalog_dir):
    slow = Service(machine, catalog_dir, fake_engine_factory(0.5), memory_probe=lambda: (128.0, 128.0))
    with TestClient(create_app(slow, "k")) as c:
        c.headers["Authorization"] = "Bearer k"
        b = Bridge(c)
        call(b, "switch_environment", env="e2")
        t = text(call(b, "generate_image", prompt="x", wait_seconds=0))
        assert "終わっていません" in t
        job_id = t.split("job=")[1].split()[0]
        t = text(call(b, "job", job_id=job_id, cancel=True, wait_seconds=10))
        assert t.startswith(("取消", "失敗")), t  # ComfyUI の中断は失敗として記録されることがある


def test_format_job_running():
    j = {"id": "abc", "kind": "generate_video", "label": "処理中", "state": "processing", "progress": 0.42}
    assert format_job(j) == "処理中 job=abc generate_video 42%（終わっていません。job ツールで待てます）"


def test_server_key_from_file(machine):
    with pytest.raises(Exception):
        server_key(machine)
    root = machine.path_of("root")
    root.mkdir(parents=True, exist_ok=True)
    (root / "server.key").write_text("secret\n", encoding="utf-8")
    assert server_key(machine) == "secret"
