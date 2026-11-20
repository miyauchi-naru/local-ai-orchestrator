"""日本語の決まった声（E3 の voices）。"""

import pytest

from lao.tools import ToolError, tools_for, validate
from lao.library import Library

from test_orchestrator import make


def test_catalog_has_japanese_male_and_female_voices(catalog):
    conf = catalog["e3"].configurations["e3-standard"]
    voices = conf.voices
    genders = [v["gender"] for v in voices.values()]
    assert genders.count("male") >= 15 and genders.count("female") >= 15
    assert "jp-male-calm" in voices and "jp-female-young" in voices
    assert len({v["seed"] for v in voices.values()}) == len(voices)  # 声ごとに違う seed
    vd = conf.components["voice_design"]
    assert vd.license == "Apache-2.0" and "voice_design" in vd.capabilities


def test_voice_param_lists_voices_only_where_available(tmp_path, catalog):
    e3 = catalog["e3"].configurations["e3-standard"]
    tool = next(t for t in tools_for(e3) if t.name == "generate_speech")
    schema = tool.schema()["function"]["parameters"]["properties"]["voice"]
    assert "jp-male-calm" in schema["enum"] and "落ち着いた" in schema["description"]
    lib = Library(tmp_path / "lib")
    assert validate("generate_speech", {"text": "こんにちは", "voice": "jp-male-young"}, e3, lib)["voice"] == "jp-male-young"
    with pytest.raises(ToolError, match="いずれか"):
        validate("generate_speech", {"text": "こんにちは", "voice": "nobody"}, e3, lib)


async def test_voice_is_designed_once_then_cloned(tmp_path, machine, catalog, catalog_dir):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e3", "e3-standard")
    args = {"text": "今日はいい天気です。", "voice": "jp-male-calm"}
    for _ in range(2):
        job = orch.jobs.submit("generate_speech", "e3", "e3-standard", args, orch._runner("generate_speech", args))
        job = await orch.jobs.wait(job.id)
        assert job.state == "done", job.error
    design = rt.engine("voice_design").tts.calls
    assert len(design) == 1  # 見本は一度だけ作る
    assert "thirties" in design[0]["voice_design"] and design[0]["reference_audio"] is None
    speak = rt.engine("tts").tts.calls
    assert len(speak) == 2 and all(c["reference_audio"] and c["reference_text"] for c in speak)
    sample = [a for a in lib.list(kind="audio") if a.generation.get("tool") == "voice_design"]
    assert len(sample) == 1 and sample[0].title.startswith("声：男性")
    out = lib.get(job.result[0])
    assert out.generation["voice"] == "jp-male-calm" and out.sources == [sample[0].ref]


from test_remote import bridge, call, client, svc, text  # noqa: E402,F401


def test_voices_api_settings_and_speak(client, svc):
    vs = client.get("/api/voices").json()["voices"]
    assert {"jp-male-calm", "jp-female-calm"} <= {v["id"] for v in vs}
    assert client.put("/api/settings", json={"voice": "nobody"}).status_code == 400
    assert client.put("/api/settings", json={"voice": "jp-male-narrator"}).json()["voice"] == "jp-male-narrator"
    client.post("/api/environment", json={"env": "e3"})
    job = client.post("/api/speak", json={"text": "こんにちは"}).json()["job"]
    assert svc.jobs.get(job).args["voice"] == "jp-male-narrator"  # 設定の声で読む
    job = client.post("/api/speak", json={"text": "こんにちは", "voice": "jp-female-girl"}).json()["job"]
    assert svc.jobs.get(job).args["voice"] == "jp-female-girl"


def test_mcp_lists_voices(bridge):
    r = call(bridge, "voices")
    assert not r["isError"] and "jp-male-calm" in text(r) and "男性" in text(r)
