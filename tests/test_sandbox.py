"""コード実行の箱。bwrap が動く環境では実際に閉じていることまで確かめる。"""

import asyncio
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from lao.backends.fake import fake_engine_factory
from lao.sandbox import Sandbox, SandboxConfig, SandboxError, check
from lao.server import create_app
from lao.service import Service
from lao.skills import Skill, SkillError, SkillStore


def _bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    r = subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
                        "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin", "--proc", "/proc",
                        "--dev", "/dev", "--", "true"], capture_output=True)
    return r.returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="bwrap が使えない環境")


def test_command_wsl_and_disabled(tmp_path):
    c = SandboxConfig.from_machine({"sandbox": {"mode": "wsl"}}, tmp_path)
    assert c.work_host.startswith("\\\\wsl.localhost\\lao-sandbox") and c.work_guest == "/home/sandbox/work"
    cmd = Sandbox(c).command("/home/sandbox/work/x", 60)
    assert cmd[:6] == ["wsl.exe", "-d", "lao-sandbox", "-u", "sandbox", "--exec"]
    assert "--unshare-all" in cmd and cmd[-2:] == ["python3", "main.py"]
    with pytest.raises(SandboxError):
        asyncio.run(Sandbox(c).run("print(1)"))  # 既定は無効


def test_skill_files(tmp_path):
    store = SkillStore(tmp_path)
    store.save(Skill("calc", "計算", "手順"))
    store.save_file("calc", "helper.py", b"x = 1")
    for bad in ("../x.py", "run.exe", ".hidden"):
        with pytest.raises((SkillError, KeyError)):
            store.save_file("calc", bad, b"")
    with pytest.raises(SkillError):
        store.save_file("calc", "big.txt", b"x" * (1024 * 1024 + 1))
    store.save(Skill("calc2", "計算", "手順"), rename_from="calc")  # 名前を変えてもファイルは残る
    assert [f["name"] for f in store.files("calc2")] == ["helper.py"]


@needs_bwrap
def test_check_passes(tmp_path):
    rows = asyncio.run(check(SandboxConfig(work_host=str(tmp_path), work_guest=str(tmp_path))))
    assert all(ok for _, ok, _ in rows), rows


@needs_bwrap
def test_run_inputs_outputs_and_timeout(tmp_path):
    src = tmp_path / "data.csv"
    src.write_text("1\n2\n3\n")
    work = tmp_path / "w"
    sb = Sandbox(SandboxConfig(enabled=True, work_host=str(work), work_guest=str(work)))
    code = "n = sum(int(x) for x in open('in/data.csv'))\nprint(n)\nopen('out/n.txt', 'w').write(str(n))"
    r = asyncio.run(sb.run(code, inputs=[("data.csv", src)]))
    assert r.exit_code == 0 and r.stdout.strip() == "6" and [p.name for p in r.outputs] == ["n.txt"]
    Sandbox.cleanup(r)
    r = asyncio.run(sb.run("import os\nprint(os.path.exists('" + str(tmp_path) + "'))"))
    assert r.stdout.strip() == "False"  # 箱の外のフォルダは見えない
    r = asyncio.run(sb.run("while True: pass", timeout=1))
    assert r.timed_out


@needs_bwrap
def test_run_code_in_conversation(machine, catalog_dir, tmp_path):
    svc = Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0))
    svc.sandbox.conf.enabled = True
    svc.sandbox.conf.work_host = svc.sandbox.conf.work_guest = str(tmp_path / "sb")
    with TestClient(create_app(svc, "k")) as c:
        c.headers["Authorization"] = "Bearer k"
        c.post("/api/environment", json={"env": "e1"})
        cid = c.post("/api/conversations").json()["id"]
        with c.stream("POST", f"/api/conversations/{cid}/messages", json={"text": "計算して"}) as r:
            events = [json.loads(x) for x in r.iter_lines() if x]
        assert any("5050" in e.get("text", "") for e in events if e["type"] == "progress")
        refs = [x for e in events if e["type"] == "artifacts" for x in e["data"]["refs"]]
        assert refs and c.get(f"/files/{refs[0]}").text == "a,b\n1,2\n"
        assert not any((tmp_path / "sb").iterdir())  # 作業フォルダは片付ける
