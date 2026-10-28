import asyncio
import json

import pytest

from lao.jobs import JobManager
from lao.library import Library
from lao.lock import InferenceLock


async def test_lock_serializes():
    lock = InferenceLock()
    active, peak = 0, 0

    async def work(i):
        nonlocal active, peak
        async with lock.hold(f"w{i}"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(work(i) for i in range(5)))
    assert peak == 1


async def test_job_done_failed_cancelled(tmp_path):
    jm = JobManager(tmp_path)

    async def ok(ctx):
        ctx.set_state("processing")
        return ["abc@1"]

    async def bad(ctx):
        raise RuntimeError("boom")

    async def slow(ctx):
        for _ in range(100):
            await asyncio.sleep(0.01)
            ctx.check_cancel()
        return []

    j1 = jm.submit("t", "e1", "c", {}, ok)
    j2 = jm.submit("t", "e1", "c", {}, bad)
    j3 = jm.submit("t", "e1", "c", {}, slow)
    await asyncio.sleep(0.05)
    await jm.cancel(j3.id)
    for j in (j1, j2, j3):
        await jm.wait(j.id)
    assert jm.get(j1.id).state == "done" and jm.get(j1.id).result == ["abc@1"]
    assert jm.get(j2.id).state == "failed" and "boom" in jm.get(j2.id).error
    assert jm.get(j3.id).state == "cancelled"


def test_unfinished_jobs_become_interrupted_after_restart(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps({
        "id": "x", "kind": "t", "env_id": "e1", "configuration": "c", "args": {}, "state": "processing",
    }), encoding="utf-8")
    jm = JobManager(tmp_path)
    assert jm.get("x").state == "interrupted"


def test_library_versions_and_integrity(tmp_path, sample_image):
    lib = Library(tmp_path / "lib")
    a = lib.add_file(sample_image, env_id="e2", configuration="c", title="cat")
    b = lib.add_file(sample_image, env_id="e2", configuration="c", parent=a.ref)
    assert a.ref.endswith("@1") and b.ref == f"{a.id}@2" and b.parent_id == a.ref
    assert lib.get(a.ref).sha256 == a.sha256
    with pytest.raises(ValueError):
        lib.get(a.id)  # 版の指定が必須
    lib.path(a).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        lib.path(a)
