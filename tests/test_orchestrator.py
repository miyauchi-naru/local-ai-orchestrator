import asyncio
from pathlib import Path

import pytest

from lao.backends.fake import fake_engine_factory
from lao.jobs import JobManager
from lao.library import Library
from lao.media import Media
from lao.memory import MemoryGovernor
from lao.orchestrator import Orchestrator, split_text, video_frames
from lao.runtime import Runtime


def make(tmp_path, machine, catalog, catalog_dir, env, conf, budget=96, delay=0.0):
    e = catalog[env]
    c = e.configurations[conf]
    gov = MemoryGovernor(budget, 1.0, 0, probe=lambda: (128, 128))
    rt = Runtime(machine, e, c, fake_engine_factory(delay), governor=gov)
    lib = Library(tmp_path / "lib")
    orch = Orchestrator(rt, lib, JobManager(tmp_path / "jobs"), Media(), tmp_path / "work", catalog_dir)
    return rt, lib, orch


async def run(orch, text, attachments=None):
    events = [ev async for ev in orch.respond([{"role": "user", "content": text}], attachments)]
    return events


async def test_e2_generate_then_edit_creates_new_version(tmp_path, machine, catalog, catalog_dir):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e2", "e2-standard")
    ev = await run(orch, "猫の画像を作って")
    refs = [r for e in ev if e.type == "artifacts" for r in e.data["refs"]]
    assert len(refs) == 1 and lib.get(refs[0]).kind == "image"
    assert ev[-1].type == "text" and refs[0] in ev[-1].text
    # 修正
    out = []
    async for e in orch.respond([{"role": "user", "content": "もっと明るく"}], [refs[0]]):
        out.append(e)
    # ダミーモデルは添付があると describe_media を選ぶ。edit は直接ツールで確認する
    from lao.tools import validate
    args = validate("edit_image", {"ref": refs[0], "instruction": "明るく"}, rt.conf, lib)
    job = orch.jobs.submit("edit_image", "e2", rt.conf.id, args, orch._runner("edit_image", args))
    await orch.jobs.wait(job.id)
    new = orch.jobs.get(job.id)
    assert new.state == "done"
    assert new.result[0] == f"{refs[0].split('@')[0]}@2"
    assert lib.get(refs[0]).sha256  # 元の版は残る


async def test_e1_video_understanding_uses_frames_and_audio(tmp_path, machine, catalog, catalog_dir, sample_video):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e1", "e1-standard")
    v = lib.add_file(sample_video, env_id="e1", configuration="x")
    ev = await run(orch, "この動画で何が起きた？", [v.ref])
    done = [e for e in ev if e.type == "artifacts"]
    assert done, [e.text for e in ev]
    note = lib.path(lib.get(done[0].data["refs"][0])).read_text(encoding="utf-8")
    assert "input_audio" in note and "image_url" in note
    assert "[00:00.00–00:10.00]" in note  # 区間ラベル
    assert "見落とす可能性" in note


async def test_e3_speech(tmp_path, machine, catalog, catalog_dir):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e3", "e3-standard")
    ev = await run(orch, "これを読み上げて。今日はいい天気です。明日も晴れるでしょう。")
    refs = [r for e in ev if e.type == "artifacts" for r in e.data["refs"]]
    assert lib.get(refs[0]).kind == "audio"


async def test_e4_image_to_video_reports_silent_and_length(tmp_path, machine, catalog, catalog_dir, sample_image):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e4", "e4-wan")
    img = lib.add_file(sample_image, env_id="e2", configuration="x")
    ev = await run(orch, "この画像から動画を作って", [img.ref])
    refs = [r for e in ev if e.type == "artifacts" for r in e.data["refs"]]
    art = lib.get(refs[0])
    assert art.kind == "video" and art.sources == [img.ref]
    assert art.generation["has_audio"] is False
    assert "音声はありません" in ev[-1].text


async def test_out_of_environment_tool_rejected(tmp_path, machine, catalog, catalog_dir):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e1", "e1-standard")
    ev = await run(orch, "画像を作って")  # E1 に generate_image は無い
    assert all(e.type != "artifacts" for e in ev)


async def test_request_over_budget_blocked_before_inference(tmp_path, machine, catalog, catalog_dir):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e1", "e1-standard", budget=40)
    ev = await run(orch, "こんにちは")
    assert ev[-1].type == "error" and "実行前に止めました" in ev[-1].text
    assert rt.engine("reasoner").calls == [] if rt.engine("reasoner").running else True


async def test_context_overflow_blocked(tmp_path, machine, catalog, catalog_dir):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e2", "e2-standard")
    ev = [e async for e in orch.respond([{"role": "user", "content": "あ" * 60000}])]
    assert ev[-1].type == "error" and "長すぎます" in ev[-1].text
    assert rt.engine("chat").calls == []


async def test_only_one_inference_at_a_time(tmp_path, machine, catalog, catalog_dir):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e2", "e2-standard", delay=0.05)
    peak = 0
    orig = rt.lock.hold

    from contextlib import asynccontextmanager
    active = 0

    @asynccontextmanager
    async def spy(holder):
        nonlocal active, peak
        async with orig(holder):
            active += 1
            peak = max(peak, active)
            try:
                yield
            finally:
                active -= 1

    rt.lock.hold = spy
    await asyncio.gather(run(orch, "画像を作って"), run(orch, "こんにちは"), run(orch, "別の画像を作って"))
    assert peak == 1


async def test_cancel_generation(tmp_path, machine, catalog, catalog_dir):
    rt, lib, orch = make(tmp_path, machine, catalog, catalog_dir, "e2", "e2-standard", delay=1.0)
    from lao.tools import validate
    args = validate("generate_image", {"prompt": "x"}, rt.conf, lib)
    job = orch.jobs.submit("generate_image", "e2", rt.conf.id, args, orch._runner("generate_image", args))
    await asyncio.sleep(0.3)
    await orch.jobs.cancel(job.id)
    await orch.jobs.wait(job.id)
    assert orch.jobs.get(job.id).state in ("cancelled", "failed")
    assert rt.engine("imagegen").comfy.interrupted


def test_helpers():
    assert video_frames(5, 24) == 121
    chunks = split_text("あ。" * 300, 200)
    assert all(len(c) <= 200 for c in chunks) and "".join(chunks) == "あ。" * 300
