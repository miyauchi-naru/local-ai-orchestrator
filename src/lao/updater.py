"""lao update：リポジトリを最新にし、lao serve を再起動して、動かなければ元の版に戻す。

メイン PC の Claude Code（lao mcp の update）や、Spark の画面から呼べるようにするためのもの。
lao は編集可能な形（pip install -e）で入っているので、コードの更新は git pull だけで効く。
依存（pyproject.toml）が変わったときだけ依存を入れ直す。lao.exe 自体は入れ直さない
（lao mcp や lao serve が動いている間は Windows が lao.exe を書き換えさせないため）。
"""

from __future__ import annotations

import subprocess
import sys
import time
import tomllib
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
TASK = "lao-serve"


class UpdateError(RuntimeError):
    pass


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        raise UpdateError(f"git {' '.join(args)} が失敗しました：{(r.stderr or r.stdout).strip()[:500]}")
    return r.stdout.strip()


def plan_text() -> str:
    return ("lao update がすること（execute で行う）：\n"
            f"1. {REPO} に手で変えたところが無いか確かめる（あれば止める）\n"
            "2. git pull --ff-only で最新にする（変わらなければ終わり）\n"
            "3. pyproject.toml の依存が変わっていれば、依存だけを入れ直す\n"
            f"4. lao serve を再起動する（タスク {TASK}）\n"
            "5. 1 分以内に lao serve が応えなければ、元の版に戻して再起動する")


def dependencies(text: str) -> list[str]:
    return list(tomllib.loads(text).get("project", {}).get("dependencies", []))


def restart_serve() -> str:
    if sys.platform != "win32":
        return "（Windows 以外なので lao serve の再起動は飛ばしました。手で起動し直してください）"
    found = subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True, text=True)
    if found.returncode != 0:
        return f"（タスク {TASK} が無いので再起動していません）"
    subprocess.run(["schtasks", "/End", "/TN", TASK], capture_output=True, text=True)
    time.sleep(3)
    r = subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True, text=True)
    if r.returncode != 0:
        raise UpdateError(f"lao serve を起動できません：{r.stderr.strip()[:300]}")
    return "lao serve を再起動しました"


def wait_healthy(url: str, seconds: float = 60) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            # 鍵なしで 401 が返れば、lao serve は動いている
            if httpx.get(f"{url}/api/status", timeout=5, trust_env=False).status_code in (200, 401):
                return True
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def update(machine, execute: bool = False, restart: bool = True, log=print) -> int:
    if not execute:
        log(plan_text())
        return 0
    url = f"http://127.0.0.1:{int(machine['server']['port'])}"
    if git("status", "--porcelain"):
        raise UpdateError(f"{REPO} に手で変えたところがあるので止めました（git status で確かめてください）")
    before = git("rev-parse", "HEAD")
    old_py = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    git("pull", "--ff-only")
    after = git("rev-parse", "HEAD")
    if after == before:
        log(f"最新です（{before[:7]}）")
        return 0
    log(f"{before[:7]} → {after[:7]}")
    log(git("log", "--oneline", f"{before}..{after}"))
    try:
        new_py = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        if dependencies(new_py) != dependencies(old_py):
            log("依存を入れ直します")
            r = subprocess.run([sys.executable, "-m", "pip", "install", *dependencies(new_py)],
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise UpdateError(f"依存を入れられません：{r.stderr.strip()[-500:]}")
        if restart:
            log(restart_serve())
            if sys.platform == "win32" and not wait_healthy(url):
                raise UpdateError("再起動した lao serve が応えません")
    except UpdateError as e:
        log(f"失敗：{e}　元の版（{before[:7]}）に戻します")
        git("reset", "--hard", before)
        if restart:
            try:
                log(restart_serve())
            except UpdateError as e2:
                log(f"元の版での再起動も失敗しました：{e2}")
        return 1
    log("更新しました")
    return 0


def start_detached(machine) -> Path:
    """lao update --execute を別のプロセスで始める（呼んだ lao mcp や lao serve が再起動で切れても続くように）。"""
    logs = Path(machine.path_of("root")) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"update-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    with log.open("w", encoding="utf-8") as f:
        subprocess.Popen([sys.executable, "-m", "lao.cli", "update", "--execute"], stdout=f, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, cwd=str(REPO), creationflags=flags, close_fds=True)
    return log
