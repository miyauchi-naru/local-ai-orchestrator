"""使い勝手を上げる機能：受け取り箱からの要約、まとめて作る画像、スタイル、動画と音声の合成、
使わないモデルを下ろす、健康状態、片づけ、バックアップ、更新。"""

import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from lao import maintenance, updater
from lao.backends.fake import fake_engine_factory
from lao.mcp import TOOL_DEFS, Bridge, handle
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
def bridge(client, machine):
    return Bridge(client, machine)


def call(bridge, tool, **args):
    r = handle(bridge, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    return r["result"]


def text(result):
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


def test_new_tools_are_listed():
    names = {t["name"] for t in TOOL_DEFS}
    assert {"inbox", "make_clip", "combine", "styles", "save_style", "health", "cleanup", "backup", "unload",
            "update"} <= names
    gi = next(t for t in TOOL_DEFS if t["name"] == "generate_image")["inputSchema"]["properties"]
    assert {"count", "aspect", "style"} <= set(gi)
    assert "inbox" in next(t for t in TOOL_DEFS if t["name"] == "ask_local")["inputSchema"]["properties"]


def test_ask_local_reads_inbox_folder_without_caller_reading_it(bridge, svc):
    box = svc.root / "inbox" / "資料"
    (box / "sub").mkdir(parents=True)
    (box / "memo.md").write_text("予算は 300 万円に決まった。", encoding="utf-8")
    (box / "sub" / "todo.txt").write_text("来週までに見積もりを出す。", encoding="utf-8")
    (box / "image.bin").write_bytes(b"\0\1")  # 取り込めないものは飛ばす
    r = call(bridge, "ask_local", task="三行で要約して", inbox=["資料"])
    assert not r["isError"], text(r)
    assert "取り込んだ資料" in text(r)
    sent = svc.runtime.engine(svc.runtime.conf.chat_component).calls[-1][-1]["content"]
    assert "300 万円" in sent and "見積もり" in sent
    assert not box.exists()  # 取り込んだら箱から消える
    assert {a.title for a in svc.library.list()} >= {"資料/memo.md", "資料/sub/todo.txt"}


def test_ask_local_splits_long_material(bridge, svc):
    call(bridge, "switch_environment", env="e1")
    room = svc.orchestrator.context_limit - 1024 - 1024
    r = call(bridge, "ask_local", task="要約", text="あ" * (room * 3))
    assert not r["isError"], text(r)
    assert "区切りに分けて読みました" in text(r)
    calls = svc.runtime.engine(svc.runtime.conf.chat_component).calls
    assert all(len(c[-1]["content"]) <= room + 1000 for c in calls)


def test_import_rejects_escape_from_inbox(bridge, svc, tmp_path):
    (svc.root / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    r = call(bridge, "import_file", name="../../secret.md")
    assert r["isError"]
    r = call(bridge, "ask_local", task="要約", inbox=["../"])
    assert r["isError"]


def test_inbox_listing(bridge, svc):
    assert "空です" in text(call(bridge, "inbox"))
    (svc.root / "inbox" / "a").mkdir(parents=True)
    (svc.root / "inbox" / "b.png").write_bytes(b"x" * 10)
    out = text(call(bridge, "inbox"))
    assert "- a/" in out and "- b.png" in out


def test_generate_several_images_with_style_and_aspect(bridge, svc):
    r = call(bridge, "save_style", name="ドット絵", prompt="pixel art, {prompt}, 16 colors", negative="blurry",
             aspect="16:9", note="ゲーム用")
    assert "保存しました" in text(r)
    assert "ドット絵" in text(call(bridge, "styles"))
    r = call(bridge, "generate_image", prompt="a knight", count=3, style="ドット絵", seed=10)
    assert not r["isError"], text(r)
    refs = [line.split()[1] for line in text(r).splitlines() if line.startswith("- ")]
    assert len(refs) == 3
    gens = [svc.library.get(x).generation for x in refs]
    assert [g["seed"] for g in gens] == [10, 11, 12]
    assert gens[0]["prompt"] == "pixel art, a knight, 16 colors" and gens[0]["negative"] == "blurry"
    assert (gens[0]["width"], gens[0]["height"]) == (1344, 768) and gens[0]["style"] == "ドット絵"
    # 並べた 1 枚で見比べる
    v = call(bridge, "view", refs=refs, size=128)
    assert v["content"][0]["type"] == "image"
    # 明示の縦横比はスタイルより優先
    r = call(bridge, "generate_image", prompt="a cat", aspect="9:16", style="ドット絵")
    ref = text(r).splitlines()[1].split()[1]
    assert (svc.library.get(ref).generation["width"], svc.library.get(ref).generation["height"]) == (768, 1344)
    r = call(bridge, "generate_image", prompt="x", style="ない")
    assert r["isError"] and "ありません" in text(r)
    assert "消しました" in text(call(bridge, "save_style", name="ドット絵", delete=True))


def test_make_clip_puts_narration_on_video(bridge, svc, sample_image):
    (svc.root / "inbox").mkdir(parents=True, exist_ok=True)
    (svc.root / "inbox" / "start.png").write_bytes(sample_image.read_bytes())
    img = text(call(bridge, "import_file", name="start.png")).split("：")[1].split()[0]
    r = call(bridge, "make_clip", image_ref=img, prompt="the camera pans", seconds=2, narration="こんにちは。")
    assert not r["isError"], text(r)
    assert "できました" in text(r) and "has_audio=True" in text(r)
    ref = text(r).split("：")[1].split()[0]
    a = svc.library.get(ref)
    assert a.kind == "video" and len(a.sources) == 2


def test_combine_concatenates_videos(bridge, svc, sample_video):
    v = svc.library.add_file(sample_video, env_id="e4", configuration="upload", title="v")
    r = call(bridge, "combine", refs=[v.ref, v.ref])
    assert not r["isError"], text(r)
    seconds = float(text(r).split("seconds=")[1].split()[0])
    assert 23 < seconds < 25
    assert call(bridge, "combine", refs=[])["isError"]


async def test_idle_models_are_unloaded(svc):
    rt = await svc.select("e1")
    await rt.preload()
    assert rt.loaded()
    assert await svc.unload_idle() == []  # 使ったばかり
    keys = await svc.unload_idle(now=time.monotonic() + svc.idle_unload_seconds() + 1)
    assert keys and not rt.loaded()


def test_switch_with_preload_and_unload_tool(bridge, svc):
    r = call(bridge, "switch_environment", env="e1", preload=True)
    assert not r["isError"]
    assert svc.runtime.loaded()
    # 同じ環境をもう一度選んでも、読み込んだモデルは残る
    call(bridge, "switch_environment", env="e1")
    assert svc.runtime.loaded()
    assert "下ろしました" in text(call(bridge, "unload"))
    assert not svc.runtime.loaded()


def test_health_and_status(bridge, svc, monkeypatch):
    monkeypatch.setattr(maintenance, "gpu", lambda *_: {"name": "GB10", "temp_c": 91.0, "util_pct": 50.0,
                                                         "power_w": 80.0, "thermal_slowdown": True})
    out = text(call(bridge, "health"))
    assert "GPU 91℃" in out and "注意" in out and "熱で速度" in out
    assert "空き" in text(call(bridge, "status"))


def test_gpu_reading_without_nvidia_smi():
    assert maintenance.gpu("no-such-nvidia-smi") is None


def test_cleanup_lists_then_removes_but_keeps_library(bridge, svc):
    old = time.time() - 40 * 86400
    work = svc.work_dir / "tmpabc"
    work.mkdir(parents=True)
    (work / "x.png").write_bytes(b"x" * 1000)
    os.utime(work, (old, old))
    trash = svc.root / "trash" / "old.txt"
    trash.parent.mkdir(parents=True)
    trash.write_text("x")
    os.utime(trash, (old, old))
    art = svc.library.add_text("残す", env_id="e1", configuration="upload", title="keep.md")
    out = text(call(bridge, "cleanup"))
    assert "work" in out and "trash" in out and work.exists()
    out = text(call(bridge, "cleanup", execute=True))
    assert "片づけました" in out and not work.exists() and not trash.exists()
    assert svc.library.get(art.ref)


def test_backup_contains_conversations_and_settings(bridge, svc):
    svc.save_settings({"about_me": "テスト"})
    svc.styles.save("s", prompt="p")
    out = text(call(bridge, "backup"))
    path = out.split("：")[1].split("（")[0]
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    assert "settings.json" in names and "image_styles.json" in names
    assert svc.daily_backup() is None  # 今日の分はもうある
    assert svc.daily_backup(now=time.time() + 86400 * 2) is not None


def test_backup_keeps_only_recent(tmp_path):
    (tmp_path / "settings.json").write_text("{}")
    for i in range(5):
        maintenance.backup(tmp_path, keep=3, now=1_700_000_000 + i * 100)
    assert len(list((tmp_path / "backups").glob("lao-*.zip"))) == 3


def test_update_plan_is_shown_without_execute(bridge):
    out = text(call(bridge, "update"))
    assert "git pull" in out and "元の版に戻して" in out


def test_update_rolls_back_when_serve_does_not_come_back(tmp_path, monkeypatch, machine):
    import subprocess

    def sh(*a, cwd):
        subprocess.run(list(a), cwd=cwd, check=True, capture_output=True)

    origin, work = tmp_path / "origin", tmp_path / "work"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    sh("git", "config", "user.email", "t@t", cwd=origin)
    sh("git", "config", "user.name", "t", cwd=origin)
    (origin / "pyproject.toml").write_text('[project]\nname="x"\ndependencies=["a"]\n')
    sh("git", "add", ".", cwd=origin)
    sh("git", "commit", "-qm", "1", cwd=origin)
    sh("git", "clone", "-q", str(origin), str(work), cwd=tmp_path)
    (origin / "new.txt").write_text("x")
    sh("git", "add", ".", cwd=origin)
    sh("git", "commit", "-qm", "2", cwd=origin)
    monkeypatch.setattr(updater, "REPO", work)
    before = updater.git("rev-parse", "HEAD")

    def bad_restart():
        raise updater.UpdateError("起動しない")
    monkeypatch.setattr(updater, "restart_serve", bad_restart)
    logs = []
    assert updater.update(machine, execute=True, log=logs.append) == 1
    assert any("元の版" in x for x in logs)
    assert updater.git("rev-parse", "HEAD") == before

    monkeypatch.setattr(updater, "restart_serve", lambda: "再起動しました")
    assert updater.update(machine, execute=True, log=logs.append) == 0
    assert (work / "new.txt").exists()
    assert updater.update(machine, execute=True, log=logs.append) == 0 and "最新です" in logs[-1]


def test_style_store_validation(svc):
    from lao.image_styles import StyleError
    with pytest.raises(StyleError):
        svc.styles.save("", prompt="x")
    with pytest.raises(StyleError):
        svc.styles.save("a", aspect="5:1")
    assert svc.styles.list() == []


async def test_lock_serves_generation_first_then_delegation():
    import asyncio
    from lao.lock import DELEGATE, PRIORITY, InferenceLock

    lock, order = InferenceLock(), []

    async def worker(holder, delegate=False):
        if delegate:
            PRIORITY.set(DELEGATE)
        async with lock.hold(holder):
            order.append(holder)
            await asyncio.sleep(0.01)

    async with lock.hold("chat:first"):
        tasks = [asyncio.create_task(worker("chat:ui")),
                 asyncio.create_task(worker("chat:delegate", delegate=True)),
                 asyncio.create_task(worker("generate:image")),
                 asyncio.create_task(worker("tts:voice"))]
        await asyncio.sleep(0.01)
        assert lock.status()["waiting"] == 4
    await asyncio.gather(*tasks)
    assert order == ["generate:image", "tts:voice", "chat:delegate", "chat:ui"]


async def test_lock_survives_cancelled_waiter():
    import asyncio
    from lao.lock import InferenceLock

    lock = InferenceLock()
    async with lock.hold("a"):
        t = asyncio.create_task(lock.hold("b").__aenter__())
        await asyncio.sleep(0)
        t.cancel()
    async with lock.hold("c"):
        assert lock.status()["holder"] == "c"
