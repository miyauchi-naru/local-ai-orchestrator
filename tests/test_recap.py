"""月のふりかえりと休憩の声かけの設定。"""

from datetime import datetime

import pytest

from lao.conversations import Conversation, Turn
from lao.library import Artifact
from lao.recap import month_range, previous, recap


def _ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


def _conv(cid, env, times, command=""):
    turns = []
    for t in times:
        turns += [Turn("user", "質問です", command=command, created_at=_ts(t)), Turn("assistant", "答えです。", created_at=_ts(t) + 5)]
    return Conversation(cid, f"会話{cid}", env, "c", _ts(times[0]), _ts(times[-1]), turns)


def _art(kind, when, conf="c"):
    return Artifact("a" * 12, 1, kind, "image/png", "x.png", "", _ts(when), "e2", conf)


def test_month_helpers():
    assert previous("2026-01") == "2025-12" and previous("2026-10") == "2026-09"
    s, e = month_range("2026-12")
    assert datetime.fromtimestamp(e) == datetime(2027, 1, 1)
    with pytest.raises(ValueError):
        month_range("2026-13")


def test_recap_counts():
    convs = [
        _conv("1", "e1", ["2026-10-01 09:00", "2026-10-02 09:30", "2026-10-03 23:10"], command="要約"),
        _conv("2", "e2", ["2026-10-03 02:00"]),
        _conv("3", "e1", ["2026-09-30 10:00", "2026-09-29 10:00"]),
    ]
    arts = [_art("image", "2026-10-03 02:01"), _art("image", "2026-10-04 10:00", conf="upload"), _art("video", "2026-09-01 10:00")]
    r = recap(convs, arts, "2026-10")
    assert r["messages"] == 4 and r["replies"] == 4 and r["previous_messages"] == 2
    assert r["conversations"] == 2 and r["started"] == 2
    assert r["active_days"] == 3 and r["longest_streak"] == 3
    assert r["busiest_day"] == {"date": "2026-10-03", "messages": 2}
    assert r["by_env"] == {"e1": 3, "e2": 1} and r["by_hour"][9] == 2 and r["by_weekday"][3] == 1
    assert r["artifacts"] == {"image": 1}  # 取り込んだファイルは数えない
    assert r["commands"] == {"要約": 3} and r["late_night_messages"] == 1
    assert r["top_conversations"][0] == {"id": "1", "title": "会話1", "messages": 3}
    assert recap(convs, arts, "2026-08")["messages"] == 0


def test_recap_api_and_break_setting(tmp_path, machine, catalog_dir):
    from fastapi.testclient import TestClient

    from lao.backends.fake import fake_engine_factory
    from lao.server import create_app
    from lao.service import Service

    svc = Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0))
    with TestClient(create_app(svc, "k")) as c:
        c.headers["Authorization"] = "Bearer k"
        assert c.get("/api/recap").json()["messages"] == 0
        assert c.get("/api/recap?month=2026-1").status_code == 400
        assert c.get("/api/settings").json()["break_minutes"] == 90
        assert c.put("/api/settings", json={"break_minutes": 0}).status_code == 200
        assert c.put("/api/settings", json={"break_minutes": 5}).status_code == 400
