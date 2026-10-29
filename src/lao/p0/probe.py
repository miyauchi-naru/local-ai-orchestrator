"""実機検証（P0）：測って、設定値を提案する。

  lao p0 run      … 機器情報・GPU が確保できるメモリ・エンジンの有無・モデルごとの速度とピークを測る
  lao p0 propose  … 最新の測定結果から config/machine.proposed.toml を作る（人が確認して採用する）

測定はユーザーの「開始」指示の後、実機で行う。ここでは何もインストールしない。
"""

from __future__ import annotations

import asyncio
import ctypes
import datetime as dt
import json
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..config import MachineConfig, load_catalog
from ..memory import system_memory

GPU_PROBE = r"""
import json, sys, torch
step = int(sys.argv[1]) if len(sys.argv) > 1 else 1
if not torch.cuda.is_available():
    print(json.dumps({"cuda": False})); sys.exit(0)
blocks, gb = [], 0
try:
    while gb < 200:
        blocks.append(torch.empty(step * 1024**3, dtype=torch.uint8, device="cuda"))
        gb += step
        torch.cuda.synchronize()
except RuntimeError:
    pass
name = torch.cuda.get_device_name(0)
print(json.dumps({"cuda": True, "device": name, "allocatable_gb": gb, "torch": torch.__version__,
                  "cuda_version": torch.version.cuda}))
"""


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return -1, str(e)


def power_status() -> dict:
    if sys.platform != "win32":
        return {"known": False}

    class SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    s = SPS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))  # type: ignore[attr-defined]
    return {"known": True, "ac_power": s.ACLineStatus == 1, "battery_percent": s.BatteryLifePercent}


def system_info(machine: MachineConfig) -> dict[str, Any]:
    total, free = system_memory()
    root = machine.path_of("root")
    disk_root = root if root.exists() else Path(root.anchor or "/")
    info = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "python_arch": platform.architecture()[0],
        "memory_total_gb": round(total, 1),
        "memory_free_gb": round(free, 1),
        "disk_free_gb": round(shutil.disk_usage(disk_root).free / 1e9, 1),
        "power": power_status(),
    }
    rc, out = _run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
    info["nvidia_smi"] = out if rc == 0 else None
    if sys.platform == "win32":
        rc, out = _run(["wsl", "--status"])
        info["wsl"] = out if rc == 0 else None
    return info


def checks(info: dict, machine: MachineConfig) -> list[tuple[bool, str]]:
    exp = float(machine["machine"].get("expected_total_memory_gb", 128))
    res = [
        (info["machine"].lower() in ("arm64", "aarch64"), f"CPU が Arm64（{info['machine']}）"),
        (info["memory_total_gb"] >= exp * 0.9, f"メモリ {info['memory_total_gb']}GB（想定 {exp:.0f}GB）"),
        (info["nvidia_smi"] is not None, f"NVIDIA ドライバ（{info['nvidia_smi'] or '見つからない'}）"),
        (info["disk_free_gb"] >= 500, f"ディスク空き {info['disk_free_gb']}GB（推奨 500GB 以上）"),
    ]
    p = info["power"]
    if p.get("known"):
        res.append((bool(p.get("ac_power")), "AC 電源に接続（測定は AC 電源で行う）"))
    return res


def gpu_probe(python: str, step_gb: int = 1) -> dict:
    """別プロセスで GPU に少しずつ確保し、確保できた最大量を測る。"""
    rc, out = _run([python, "-c", GPU_PROBE, str(step_gb)], timeout=900)
    if rc != 0:
        return {"error": out[-1000:]}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": out[-1000:]}


def engine_versions(machine: MachineConfig) -> dict:
    e = machine["engines"]
    out = {}
    rc, txt = _run([e["llama"]["binary"], "--version"])
    out["llama_server"] = txt.splitlines()[-1] if rc == 0 and txt else None
    ff = e.get("ffmpeg", {})
    rc, txt = _run([ff.get("binary", "ffmpeg"), "-version"])
    out["ffmpeg"] = txt.splitlines()[0] if rc == 0 else None
    out["ollama_found"] = shutil.which("ollama") is not None
    return out


class PeakSampler:
    """測定中の OS 全体の使用メモリのピークを取る（統合メモリなので GPU 分も含む）。"""

    def __init__(self, interval: float = 0.2):
        self.interval = interval
        self.base = self.peak = 0.0
        self._stop = threading.Event()

    def __enter__(self):
        total, free = system_memory()
        self.base = self.peak = total - free
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            total, free = system_memory()
            self.peak = max(self.peak, total - free)
            time.sleep(self.interval)

    def __exit__(self, *a):
        self._stop.set()
        self._t.join()

    @property
    def delta_gb(self) -> float:
        return round(self.peak - self.base, 2)


BENCH_PROMPT = "日本の四季について、それぞれの特徴を一段落ずつ、合計400字程度で説明してください。"


async def bench_llama(machine: MachineConfig, env_id: str, conf, comp, log_dir: Path) -> dict:
    from ..backends.llama import LlamaServer

    srv = LlamaServer(f"p0-{env_id}-{comp.key}", machine["engines"]["llama"]["binary"],
                      machine.path_of("models") / conf.id / comp.key, 8799,
                      int(comp.raw.get("context", {}).get("default", 8192)), log_dir, list(comp.raw.get("args", [])))
    result: dict[str, Any] = {"model": comp.model}
    try:
        with PeakSampler() as s:
            t0 = time.monotonic()
            await srv.load()
            result["load_seconds"] = round(time.monotonic() - t0, 1)
            t0 = time.monotonic()
            r = await srv.chat([{"role": "user", "content": BENCH_PROMPT}], max_tokens=512)
            result["first_answer_seconds"] = round(time.monotonic() - t0, 1)
            timings = r.get("timings", {})
            result["tokens_per_second"] = round(timings.get("predicted_per_second", 0), 1)
            result["prompt_tokens_per_second"] = round(timings.get("prompt_per_second", 0), 1)
            result["sample"] = (r["choices"][0]["message"].get("content") or "")[:300]
        result["peak_gb"] = s.delta_gb
    except Exception as e:  # noqa: BLE001 失敗も結果として残す
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        await srv.unload()
        await srv.close()
    return result


def run(machine: MachineConfig, catalog_dir: Path, out_dir: Path, *, gpu_python: str | None, envs: list[str],
        skip_models: bool = False) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    res: dict[str, Any] = {"timestamp": stamp}
    print("== 機器情報 ==")
    info = system_info(machine)
    res["system"] = info
    for ok, msg in checks(info, machine):
        print(f"  [{'OK' if ok else 'NG'}] {msg}")
    print("== エンジン ==")
    res["engines"] = engine_versions(machine)
    for k, v in res["engines"].items():
        print(f"  {k}: {v}")
    if res["engines"]["ollama_found"]:
        print("  [注意] ollama が PATH にあります。本ツールは使いません（R05）")
    if gpu_python:
        print("== GPU が確保できるメモリ（PyTorch） ==")
        res["gpu"] = gpu_probe(gpu_python)
        print(f"  {res['gpu']}")
    if not skip_models:
        catalog = load_catalog(catalog_dir)
        res["models"] = {}
        log_dir = out_dir / "logs"
        for env_id in envs:
            for conf in catalog[env_id].configurations.values():
                for comp in conf.components.values():
                    if comp.engine != "llama":
                        continue
                    if not (machine.path_of("models") / conf.id / comp.key).exists():
                        print(f"  {env_id}.{comp.key}: モデル未取得のため省略")
                        continue
                    print(f"== {env_id}.{comp.key}（{comp.model}） ==")
                    r = asyncio.run(bench_llama(machine, env_id, conf, comp, log_dir))
                    res["models"][f"{env_id}.{comp.key}"] = r
                    print(f"  {r}")
    path = out_dir / f"p0-{stamp}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果：{path}")
    return path


def propose(machine: MachineConfig, results_dir: Path, out: Path) -> dict:
    files = sorted(results_dir.glob("p0-*.json"))
    if not files:
        raise FileNotFoundError(f"{results_dir} に測定結果がありません。先に lao p0 run を実行してください")
    res = json.loads(files[-1].read_text(encoding="utf-8"))
    raw = json.loads(json.dumps(machine.raw))  # 深いコピー
    notes: list[str] = []
    gpu = res.get("gpu") or {}
    if gpu.get("cuda") and gpu.get("allocatable_gb"):
        cap = int(gpu["allocatable_gb"] * 0.95)
        raw["memory"]["gpu_allocatable_gb"] = cap
        notes.append(f"GPU が確保できた量 {gpu['allocatable_gb']}GB → gpu_allocatable_gb = {cap}（5% の余裕）")
        if raw["memory"]["budget_gb"] > cap:
            notes.append(f"予算 {raw['memory']['budget_gb']}GB は GPU 上限を超えるため、実際は {cap}GB で判定されます")
    elif "gpu" in res:
        notes.append(f"GPU の確保量を測れませんでした：{gpu.get('error') or 'CUDA が使えない'}")
    measured = raw.setdefault("measured", {})
    for key, m in (res.get("models") or {}).items():
        if "error" in m:
            notes.append(f"{key}: 失敗 {m['error']}")
            continue
        measured[key] = {k: m[k] for k in ("peak_gb", "load_seconds", "tokens_per_second") if k in m}
        notes.append(f"{key}: ピーク {m.get('peak_gb')}GB、ロード {m.get('load_seconds')} 秒、"
                     f"生成 {m.get('tokens_per_second')} tok/s")
    slow = max((m.get("first_answer_seconds", 0) for m in (res.get("models") or {}).values()), default=0)
    if slow:
        wd = max(900, int(slow * 10))
        raw.setdefault("gpu", {})["watchdog_seconds"] = wd
        notes.append(f"最も遅い応答 {slow} 秒 → watchdog_seconds = {wd}")
    out.write_text(to_toml(raw), encoding="utf-8")
    return {"source": str(files[-1]), "proposed": str(out), "notes": notes}


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(type(v))


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else json.dumps(k)


def to_toml(d: dict, prefix: str = "") -> str:
    """設定の dict を TOML に書き出す（表は [a.b]、値は単純型・配列・インライン表）。"""
    lines, tables = [], []
    for k, v in d.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_toml_value(v)}")
    out = "\n".join(lines)
    for k, v in tables:
        name = f"{prefix}.{_key(k)}" if prefix else _key(k)
        if name.endswith("measured"):
            body = "\n".join(f"{_key(mk)} = {_toml_value(mv)}" for mk, mv in v.items())
            out += f"\n\n[{name}]\n{body}"
        else:
            out += f"\n\n[{name}]\n" + to_toml(v, name)
    return out.strip() + "\n"
