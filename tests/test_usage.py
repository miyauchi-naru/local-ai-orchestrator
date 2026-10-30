"""Claude のトークンの記録：道具の呼び出しごとの見積もりと、Claude Code の使用量のまとめ。"""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from lao.backends.fake import fake_engine_factory
from lao.mcp import Bridge, handle
from lao.server import create_app
from lao.service import Service
from lao.usage import UsageLog, category, est_tokens, summary_text


@pytest.fixture
def svc(machine, catalog_dir):
    return Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0))


@pytest.fixture
def bridge(svc, machine):
    with TestClient(create_app(svc, "k")) as c:
        c.headers["Authorization"] = "Bearer k"
        yield Bridge(c, machine)


def call(bridge, tool, **args):
    return handle(bridge, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})["result"]


def test_token_estimate():
    assert est_tokens("") == 0
    assert est_tokens("abcdefgh") == 2
    assert est_tokens("日本語") == 3
    assert category("generate_video") == "cannot"
    assert category("describe_media", "audio") == "cannot"
    assert category("describe_media", "image") == "saved"
    assert category("ask_local") == "saved" and category("status") == "other"


def test_calls_are_recorded_with_savings(bridge, svc, sample_image):
    month = datetime.now().strftime("%Y-%m")
    (svc.root / "inbox").mkdir(parents=True, exist_ok=True)
    (svc.root / "inbox" / "long.md").write_text("会議の記録。" * 2000, encoding="utf-8")
    assert not call(bridge, "ask_local", task="三行で要約して", inbox=["long.md"])["isError"]
    call(bridge, "generate_image", prompt="a cat")
    img = svc.library.add_file(sample_image, env_id="e1", configuration="upload", title="s.png")
    call(bridge, "describe_media", ref=img.ref, question="何が写っている？")
    call(bridge, "view", ref=img.ref, size=128)
    call(bridge, "nope")  # 失敗も残す
    recs = svc.usage.records(month)
    by = {r["tool"]: r for r in recs}
    assert set(by) >= {"ask_local", "generate_image", "describe_media", "view", "nope"}
    assert by["ask_local"]["category"] == "saved" and by["ask_local"]["saved_tokens"] > 10000
    assert by["generate_image"]["category"] == "cannot" and by["generate_image"]["saved_tokens"] == 0
    assert by["describe_media"]["saved_tokens"] > 1000
    assert 0 < by["view"]["returned_tokens"] < 100  # 128px の縮小版は安い
    assert by["nope"]["ok"] is False
    out = "\n".join(c["text"] for c in call(bridge, "usage")["content"])
    assert "節約できたトークン" in out and "まだ届いていません" in out
    assert not any(r["tool"] == "usage" for r in svc.usage.records(month))


def test_text_written_by_claude_is_not_counted_as_saved(bridge, svc):
    call(bridge, "ask_local", task="要約", text="すでに Claude が読んだ文章。" * 100)
    r = svc.usage.records(datetime.now().strftime("%Y-%m"))[-1]
    assert r["saved_tokens"] == 0


def test_report_merges_claude_code_days(tmp_path):
    log = UsageLog(tmp_path)
    ts = datetime(2026, 11, 3, 12).timestamp()
    log.add({"ts": ts, "tool": "ask_local", "category": "saved", "offloaded_tokens": 50000,
             "returned_tokens": 500, "saved_tokens": 49500, "seconds": 30})
    log.add({"ts": ts, "tool": "generate_video", "category": "cannot", "seconds": 240})
    (tmp_path / "claude-code").mkdir()
    (tmp_path / "claude-code" / "2026-11-03.json").write_text(json.dumps(
        {"date": "2026-11-03", "input_tokens": 1000, "output_tokens": 2000, "cache_read_input_tokens": 100000,
         "cache_creation_input_tokens": 7000, "messages": 40, "lao_calls": 2}), encoding="utf-8-sig")
    r = log.report("2026-11")
    assert r["calls"] == 2 and r["saved_tokens"] == 49500 and r["cannot_calls"] == 1 and r["cannot_seconds"] == 240
    assert r["claude_code"]["total_tokens"] == 1000 + 2000 + 7000 + 10000
    assert r["saved_share"] == round(49500 / (20000 + 49500), 3)
    assert r["per_day"]["2026-11-03"]["claude_code_tokens"] == 20000
    assert "約 71%" in summary_text(r)
    with pytest.raises(ValueError):
        log.report("../x")
