"""統合メモリの予算管理と受付判定（R04・R15）。

厳密な上限の強制はできない前提で、「常駐分＋これから必要な分×安全係数」が予算に
収まるかを実行前に判定する。実測値（machine.toml の [measured]）があれば見積もりより優先する。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass, field

GB = 1024**3


def system_memory() -> tuple[float, float]:
    """(総量GB, 空きGB) を返す。統合メモリなので OS 全体の値を見る。"""
    if sys.platform == "win32":

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
        return st.ullTotalPhys / GB, st.ullAvailPhys / GB
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
    # スワップは数えない
    return info["MemTotal"] / GB, info.get("MemAvailable", info["MemFree"]) / GB


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    need_gb: float = 0.0
    budget_gb: float = 0.0
    used_gb: float = 0.0
    evict: list[str] = field(default_factory=list)

    def explain(self) -> str:
        base = f"必要 {self.need_gb:.1f}GB／使用中 {self.used_gb:.1f}GB／予算 {self.budget_gb:.1f}GB"
        return f"{self.reason}（{base}）" if self.reason else base


class MemoryGovernor:
    """常駐（ロード済みモデル）と予約（実行中ジョブの作業領域）を一元管理する。"""

    def __init__(
        self,
        budget_gb: float,
        safety_factor: float = 1.15,
        min_free_system_gb: float = 8.0,
        probe=system_memory,
    ):
        self.budget_gb = budget_gb
        self.safety_factor = safety_factor
        self.min_free_system_gb = min_free_system_gb
        self._probe = probe
        self._resident: dict[str, float] = {}  # 部品名 -> GB
        self._reserved: dict[str, float] = {}  # ジョブID -> GB
        self._last_used: dict[str, int] = {}
        self._clock = 0
        self._mu = threading.Lock()

    # --- 状態 ---
    @property
    def resident_gb(self) -> float:
        return sum(self._resident.values())

    @property
    def reserved_gb(self) -> float:
        return sum(self._reserved.values())

    @property
    def used_gb(self) -> float:
        return self.resident_gb + self.reserved_gb

    def snapshot(self) -> dict:
        with self._mu:
            return {
                "budget_gb": self.budget_gb,
                "resident": dict(self._resident),
                "reserved": dict(self._reserved),
                "used_gb": round(self.used_gb, 2),
            }

    def touch(self, name: str) -> None:
        self._clock += 1
        self._last_used[name] = self._clock

    # --- 判定 ---
    def plan(self, load: dict[str, float], work_gb: float, pinned: set[str] = frozenset()) -> Decision:
        """load の部品をロードし work_gb の作業領域を使えるか。足りなければ追い出し候補を返す。

        追い出せるのは、予約を持たず pinned でもない常駐部品だけ（古い順）。
        """
        with self._mu:
            new_load = {k: v for k, v in load.items() if k not in self._resident}
            need = sum(new_load.values()) * self.safety_factor + work_gb * self.safety_factor
            used = self.used_gb
            if need > self.budget_gb:
                return Decision(False, "この依頼は予算を超えます。入力や出力の条件を小さくしてください",
                                need, self.budget_gb, used)
            evict: list[str] = []
            free = self.budget_gb - used
            if need > free:
                candidates = sorted(
                    (k for k in self._resident if k not in pinned and k not in load),
                    key=lambda k: self._last_used.get(k, 0),
                )
                for k in candidates:
                    if need <= free:
                        break
                    evict.append(k)
                    free += self._resident[k]
                if need > free:
                    return Decision(False, "実行中のジョブが使っているため、今は空きが足りません。待機します",
                                    need, self.budget_gb, used)
            # OS 全体の空きも確認（他アプリの増加を検知）
            _, sys_free = self._probe()
            freed = sum(self._resident[k] for k in evict)
            if sys_free + freed - need < self.min_free_system_gb:
                return Decision(False, "他のアプリがメモリを使っているため、OS の空きが足りません",
                                need, self.budget_gb, used, evict)
            return Decision(True, "", need, self.budget_gb, used, evict)

    # --- 記帳 ---
    def mark_loaded(self, name: str, gb: float) -> None:
        with self._mu:
            self._resident[name] = gb
            self.touch(name)

    def mark_unloaded(self, name: str) -> None:
        with self._mu:
            self._resident.pop(name, None)

    def reserve(self, job_id: str, gb: float) -> None:
        with self._mu:
            self._reserved[job_id] = gb * self.safety_factor

    def release(self, job_id: str) -> None:
        with self._mu:
            self._reserved.pop(job_id, None)


def kv_cache_gb(bytes_per_token: int | None, tokens: int) -> float:
    if not bytes_per_token:
        return 0.0
    return bytes_per_token * tokens / GB

