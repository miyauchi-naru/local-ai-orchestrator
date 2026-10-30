"""手入れ：健康状態（容量・GPU の温度・直近の失敗）、容量の掃除、会話や設定のバックアップ。

蓋を閉じて置いたままの PC を、別の PC の Claude Code や画面から見守るためのもの。
消すものは「作業の残り」と「古い記録」だけで、成果物（ライブラリ）には触れない。
"""

from __future__ import annotations

import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

DAY = 86400
# バックアップに入れるもの（root からの相対）。成果物・モデル・ログは大きいので入れない
BACKUP_ITEMS = ["conversations", "projects", "skills", "commands", "plugins", "agents", "memory.json",
                "settings.json", "tasks.json", "image_styles.json"]


def _existing(p: Path) -> Path:
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def disk(path: Path) -> dict:
    u = shutil.disk_usage(_existing(path))
    return {"path": str(path), "free_gb": round(u.free / 1024**3, 1), "total_gb": round(u.total / 1024**3, 1),
            "used_pct": round(100 * (u.total - u.free) / u.total, 1) if u.total else 0.0}


def gpu(nvidia_smi: str = "nvidia-smi") -> dict | None:
    """nvidia-smi で温度・使用率・電力を読む。無ければ None。読めない項目は None にする。"""
    exe = shutil.which(nvidia_smi)
    if not exe:
        return None
    q = "name,temperature.gpu,utilization.gpu,power.draw,clocks_event_reasons.hw_thermal_slowdown"
    try:
        out = subprocess.run([exe, f"--query-gpu={q}", "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=10).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    cols = [c.strip() for c in out[0].split(",")]

    def num(i: int) -> float | None:
        try:
            return float(cols[i])
        except (IndexError, ValueError):
            return None
    return {"name": cols[0] if cols else "", "temp_c": num(1), "util_pct": num(2), "power_w": num(3),
            "thermal_slowdown": (cols[4].lower() == "active") if len(cols) > 4 else None}


@dataclass
class Health:
    disks: dict[str, dict]
    gpu: dict | None
    loaded: list[str]
    idle_minutes: float | None
    failed_recent: list[dict]
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{k} 空き {d['free_gb']:.0f}GB（{d['used_pct']:.0f}% 使用）" for k, d in self.disks.items()]
        if self.gpu and self.gpu.get("temp_c") is not None:
            parts.append(f"GPU {self.gpu['temp_c']:.0f}℃")
        parts.append("読み込み中のモデル：" + (", ".join(self.loaded) or "なし"))
        lines = ["　".join(parts)]
        lines += [f"注意：{w}" for w in self.warnings]
        return "\n".join(lines)


def health(service, nvidia_smi: str = "nvidia-smi", now: float | None = None) -> Health:
    now = now or time.time()
    cfg = service.machine.raw.get("health", {})
    disk_warn = float(cfg.get("disk_warn_pct", 90))
    temp_warn = float(cfg.get("gpu_temp_warn_c", 85))
    disks = {"モデル": disk(service.machine.path_of("models")), "成果物": disk(service.root)}
    g = gpu(nvidia_smi)
    rt = service.runtime
    loaded = rt.loaded() if rt else []
    idle = round((time.monotonic() - rt.last_used) / 60, 1) if rt else None
    failed = [{"id": j.id, "kind": j.kind, "error": j.error[:200]} for j in service.jobs.list()
              if j.state == "failed" and now - j.updated_at < DAY][:5]
    h = Health(disks, g, loaded, idle, failed)
    for k, d in disks.items():
        if d["used_pct"] >= disk_warn:
            h.warnings.append(f"{k}のドライブが {d['used_pct']:.0f}% 埋まっています（cleanup で片づけられます）")
    if g and g.get("temp_c") is not None and g["temp_c"] >= temp_warn:
        h.warnings.append(f"GPU が {g['temp_c']:.0f}℃ です。長い生成の前に冷ますか、置き方を変えてください")
    if g and g.get("thermal_slowdown"):
        h.warnings.append("熱で速度を落としています（蓋を閉じているなら置き方を見直してください）")
    if rt and rt.cooling is not None:
        h.warnings.append(f"GPU が {rt.cooling:.0f}℃ なので、{rt.cool_to:.0f}℃ に下がるまで生成を小休止しています")
    if failed:
        h.warnings.append(f"直近 24 時間で {len(failed)} 件のジョブが失敗しています")
    return h


@dataclass
class Candidate:
    path: str
    kind: str
    bytes: int


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _older(p: Path, cutoff: float) -> bool:
    try:
        return p.stat().st_mtime < cutoff
    except OSError:
        return False


def cleanup(service, execute: bool = False, days: int = 30, now: float | None = None) -> dict:
    """片づけてよいものを一覧にする。execute のときだけ消す。成果物には触れない。

    - work：生成の途中に作った一時フォルダ（1 日より古いもの）
    - comfyui：ComfyUI の出力の複製（lao は成果物に取り込み済み。1 日より古いもの）
    - trash：PC のフォルダから「ごみ箱へ移した」もの（days 日より古いもの）
    - jobs：終わったジョブの記録（days 日より古いもの。成果物は残る）
    - logs：エンジンのログ（days 日より古いもの）
    - backups：古いバックアップ（新しい 7 個は残す）
    """
    now = now or time.time()
    root = service.root
    old, day_old = now - days * DAY, now - DAY
    cands: list[Candidate] = []

    def add(p: Path, kind: str) -> None:
        cands.append(Candidate(str(p), kind, _size(p)))

    for base, kind, cutoff in ((service.work_dir, "work", day_old), (root / "comfyui", "comfyui", day_old),
                               (root / "trash", "trash", old)):
        if base.is_dir():
            for p in base.rglob("*") if kind == "comfyui" else base.iterdir():
                if (p.is_file() or kind != "comfyui") and _older(p, cutoff):
                    add(p, kind)
    for j in service.jobs.list():
        if j.state in ("done", "cancelled", "failed", "interrupted") and j.updated_at < old:
            p = service.jobs.root / f"{j.id}.json"
            if p.exists():
                add(p, "jobs")
    if service.log_dir.is_dir():
        for p in service.log_dir.glob("*"):
            if p.is_file() and _older(p, old):
                add(p, "logs")
    backups = sorted((root / "backups").glob("lao-*.zip"), reverse=True) if (root / "backups").is_dir() else []
    for p in backups[7:]:
        add(p, "backups")

    removed = 0
    if execute:
        for c in cands:
            p = Path(c.path)
            try:
                if c.kind == "jobs":
                    service.jobs.forget(p.stem)
                elif p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                removed += c.bytes
            except OSError:
                continue
    by_kind: dict[str, dict] = {}
    for c in cands:
        k = by_kind.setdefault(c.kind, {"count": 0, "bytes": 0})
        k["count"] += 1
        k["bytes"] += c.bytes
    return {"executed": execute, "days": days, "total_bytes": sum(c.bytes for c in cands),
            "removed_bytes": removed, "by_kind": by_kind, "items": [c.__dict__ for c in cands[:200]]}


def backup(root: Path, keep: int = 14, now: float | None = None) -> Path:
    """会話・プロジェクト・記憶・設定・スキル・スタイルを zip にまとめ、root/backups に置く。"""
    out_dir = root / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    out = out_dir / f"lao-{stamp}.zip"
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name in BACKUP_ITEMS:
            p = root / name
            if p.is_file():
                z.write(p, name)
            elif p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file():
                        z.write(f, f.relative_to(root).as_posix())
    tmp.replace(out)
    for old in sorted(out_dir.glob("lao-*.zip"), reverse=True)[keep:]:
        old.unlink(missing_ok=True)
    return out
