"""コード実行（分析ツール）。ネットにつながらない箱の中で Python を動かす。

箱は bubblewrap（bwrap）で作る。
- 通信：ネットワークの名前空間を分けるので、外にも PC の中のサービスにもつながらない
- ファイル：/usr を読み取り専用で見せ、作業フォルダ /work だけ書ける。利用者のファイル・設定・鍵は見えない
- 時間とメモリ：timeout と prlimit で上限を付ける

動かし方は二つ（machine.toml の [sandbox] mode）。
- "bwrap"：サーバーが Linux（WSL2 の中）で動くとき、そのまま bwrap を使う
- "wsl"  ：サーバーが Windows で動くとき、箱専用の WSL ディストリビューションの中で bwrap を使う。
           ディストリビューション側は Windows のドライブの自動マウントと Windows のプログラム呼び出しを切っておく
           （scripts/install-sandbox.ps1）

既定では使わない（enabled = false）。実機で `lao sandbox-check` が全部通ってから有効にする。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_CODE_CHARS = 100_000
MAX_OUTPUT_CHARS = 20_000
MAX_OUTPUT_FILES = 20
MAX_OUTPUT_BYTES = 50 * 1024 * 1024


class SandboxError(RuntimeError):
    pass


@dataclass
class SandboxConfig:
    enabled: bool = False
    mode: str = "bwrap"  # bwrap / wsl
    distro: str = "lao-sandbox"
    user: str = "sandbox"
    python: str = "python3"
    # 作業フォルダ。サーバーから見た場所（host）と箱の側から見た場所（guest）
    work_host: str = ""
    work_guest: str = ""
    timeout_seconds: int = 60
    max_timeout_seconds: int = 600
    memory_mb: int = 4096

    @classmethod
    def from_machine(cls, raw: dict[str, Any], default_work: Path) -> "SandboxConfig":
        s = dict(raw.get("sandbox", {}))
        c = cls(**{k: v for k, v in s.items() if k in cls.__dataclass_fields__})
        if not c.work_host:
            c.work_host = str(default_work) if c.mode == "bwrap" else rf"\\wsl.localhost\{c.distro}\home\{c.user}\work"
        if not c.work_guest:
            c.work_guest = c.work_host if c.mode == "bwrap" else f"/home/{c.user}/work"
        return c


@dataclass
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    outputs: list[Path] = field(default_factory=list)
    work: Path | None = None

    def summary(self) -> dict:
        return {"exit_code": self.exit_code, "timed_out": self.timed_out,
                "stdout": self.stdout, "stderr": self.stderr}


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n…（{len(text) - MAX_OUTPUT_CHARS} 文字を省略）…\n{text[-half:]}"


class Sandbox:
    def __init__(self, conf: SandboxConfig):
        self.conf = conf

    def command(self, guest_work: str, timeout: int) -> list[str]:
        c = self.conf
        inner = [
            "bwrap", "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--symlink", "usr/bin", "/bin", "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", guest_work, "/work", "--chdir", "/work", "--clearenv",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "HOME", "/work",
            "--setenv", "MPLBACKEND", "Agg", "--setenv", "MPLCONFIGDIR", "/tmp", "--setenv", "LANG", "C.UTF-8",
            "--setenv", "PYTHONIOENCODING", "utf-8", "--setenv", "OPENBLAS_NUM_THREADS", "4",
            "--", "timeout", "-s", "KILL", str(timeout),
        ]
        if c.memory_mb > 0:
            inner += ["prlimit", f"--as={c.memory_mb * 1024 * 1024}", "--"]
        inner += [c.python, "main.py"]
        if c.mode == "wsl":
            return ["wsl.exe", "-d", c.distro, "-u", c.user, "--exec", *inner]
        if c.mode == "bwrap":
            return inner
        raise SandboxError(f"[sandbox] mode は bwrap か wsl です（{c.mode}）")

    async def run(self, code: str, *, inputs: list[tuple[str, Path]] = (), skill_dir: Path | None = None,
                  timeout: int | None = None) -> RunResult:
        if not self.conf.enabled:
            raise SandboxError("コード実行は有効になっていません（lao sandbox-check が通ってから [sandbox] enabled = true）")
        if len(code) > MAX_CODE_CHARS:
            raise SandboxError(f"コードは {MAX_CODE_CHARS} 文字までです")
        t = max(1, min(int(timeout or self.conf.timeout_seconds), self.conf.max_timeout_seconds))
        run_id = uuid.uuid4().hex[:12]
        host = Path(self.conf.work_host) / run_id
        (host / "in").mkdir(parents=True)
        (host / "out").mkdir()
        (host / "main.py").write_text(code, encoding="utf-8")
        used = set()
        for name, src in inputs:
            safe = Path(name).name or "file"
            while safe in used:
                safe = f"_{safe}"
            used.add(safe)
            shutil.copy2(src, host / "in" / safe)
        if skill_dir and skill_dir.is_dir():
            shutil.copytree(skill_dir, host / "skill")
        guest = f"{self.conf.work_guest.rstrip('/')}/{run_id}"
        proc = await asyncio.create_subprocess_exec(
            *self.command(guest, t), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), t + 15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        timed_out = proc.returncode in (-9, 137)
        outputs, total = [], 0
        for p in sorted((host / "out").rglob("*")):
            if p.is_file() and not p.is_symlink() and len(outputs) < MAX_OUTPUT_FILES:
                total += p.stat().st_size
                if total > MAX_OUTPUT_BYTES:
                    break
                outputs.append(p)
        return RunResult(proc.returncode, _clip(out.decode("utf-8", "replace")), _clip(err.decode("utf-8", "replace")),
                         timed_out, outputs, host)

    @staticmethod
    def cleanup(result: RunResult) -> None:
        if result.work:
            shutil.rmtree(result.work, ignore_errors=True)


CHECK_CODE = r'''
import json, os, socket, subprocess
r = {}
def tcp(host, port):
    try:
        socket.create_connection((host, port), timeout=3).close()
        return "つながった"
    except OSError as e:
        return f"つながらない（{e.__class__.__name__}）"
r["外部への通信"] = tcp("1.1.1.1", 443)
r["PC の中のサービス"] = tcp("127.0.0.1", int(os.environ.get("LAO_PORT", "8710")))
try:
    socket.getaddrinfo("example.com", 443)
    r["名前解決"] = "できた"
except OSError:
    r["名前解決"] = "できない"
r["Windows のドライブ"] = "見える" if os.path.exists("/mnt/c") else "見えない"
try:
    subprocess.run(["cmd.exe", "/c", "echo"], capture_output=True, timeout=5)
    r["Windows のプログラム"] = "動いた"
except Exception:
    r["Windows のプログラム"] = "動かない"
r["見えるフォルダ"] = sorted(os.listdir("/"))
open("out/ok.txt", "w").write("ok")
try:
    import numpy, matplotlib
    r["numpy・matplotlib"] = "ある"
except ImportError:
    r["numpy・matplotlib"] = "ない"
print(json.dumps(r, ensure_ascii=False))
'''

EXPECT = {"外部への通信": "つながらない", "PC の中のサービス": "つながらない", "名前解決": "できない",
          "Windows のドライブ": "見えない", "Windows のプログラム": "動かない"}


async def check(conf: SandboxConfig) -> list[tuple[str, bool, str]]:
    """箱が閉じているかを確かめる。（項目, 合格か, 詳細）の一覧を返す。"""
    import json
    conf = SandboxConfig(**{**conf.__dict__, "enabled": True})
    sb = Sandbox(conf)
    rows: list[tuple[str, bool, str]] = []
    try:
        res = await sb.run(CHECK_CODE, timeout=30)
    except (OSError, SandboxError) as e:
        return [("箱を起動できる", False, str(e))]
    rows.append(("箱を起動できる", res.exit_code == 0, res.stderr.strip()[-300:]))
    try:
        got = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        Sandbox.cleanup(res)
        return rows
    for k, want in EXPECT.items():
        rows.append((k, str(got.get(k, "")).startswith(want), str(got.get(k))))
    rows.append(("作業フォルダに書ける", any(p.name == "ok.txt" for p in res.outputs), ""))
    rows.append(("見えるフォルダ", not {"home", "root", "mnt", "c"} & set(got.get("見えるフォルダ", [])),
                 ", ".join(got.get("見えるフォルダ", []))))
    rows.append(("numpy・matplotlib（なくても動く）", True, str(got.get("numpy・matplotlib"))))
    Sandbox.cleanup(res)
    slow = await sb.run("import time\ntime.sleep(30)", timeout=2)
    rows.append(("時間切れで止まる", slow.timed_out, f"終了コード {slow.exit_code}"))
    Sandbox.cleanup(slow)
    return rows


def available() -> bool:
    return shutil.which("bwrap") is not None or (os.name == "nt" and shutil.which("wsl.exe") is not None)
