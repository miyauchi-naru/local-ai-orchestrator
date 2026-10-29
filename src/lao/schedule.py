"""決まった時刻に依頼を実行する（Claude の定期実行に相当）。

PC とサーバーが動いている間だけ動く。結果は新しい会話として残し、画面に知らせる。
実行するのは、作業を登録した環境がいま選ばれているときだけ（勝手に環境を切り替えない）。
"""

from __future__ import annotations

import json
import re
from time import time as _now
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

KINDS = ("once", "daily", "weekdays", "weekly", "interval")
WEEKDAYS = "月火水木金土日"


class ScheduleError(ValueError):
    pass


@dataclass
class Task:
    id: str
    name: str
    prompt: str
    env_id: str
    configuration: str
    kind: str = "daily"  # once / daily / weekdays / weekly / interval
    time: str = "09:00"  # HH:MM
    weekday: int = 0  # 0=月 … 6=日（weekly）
    date: str = ""  # YYYY-MM-DD（once）
    every_minutes: int = 60  # interval
    project_id: str = ""
    enabled: bool = True
    next_run: float = 0.0
    last_run: float = 0.0
    last_status: str = ""
    last_conversation: str = ""
    unread: bool = False
    created_at: float = field(default_factory=_now)

    def label(self) -> str:
        if self.kind == "once":
            return f"{self.date} {self.time} に 1 回"
        if self.kind == "daily":
            return f"毎日 {self.time}"
        if self.kind == "weekdays":
            return f"平日 {self.time}"
        if self.kind == "weekly":
            return f"毎週{WEEKDAYS[self.weekday]}曜 {self.time}"
        return f"{self.every_minutes} 分ごと"


def check(t: Task) -> None:
    if not t.name.strip() or len(t.name) > 80:
        raise ScheduleError("名前は 1〜80 文字です")
    if not t.prompt.strip() or len(t.prompt) > 20000:
        raise ScheduleError("依頼文は 1〜20000 文字です")
    if t.kind not in KINDS:
        raise ScheduleError(f"繰り返しは {', '.join(KINDS)} のいずれかです")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t.time):
        raise ScheduleError("時刻は HH:MM で書いてください")
    if t.kind == "weekly" and not 0 <= t.weekday <= 6:
        raise ScheduleError("曜日は 0（月）〜 6（日）です")
    if t.kind == "once":
        try:
            datetime.strptime(t.date, "%Y-%m-%d")
        except ValueError as e:
            raise ScheduleError("日付は YYYY-MM-DD で書いてください") from e
    if t.kind == "interval" and not 15 <= t.every_minutes <= 7 * 24 * 60:
        raise ScheduleError("間隔は 15 分から 1 週間までです")


def next_run(t: Task, after: datetime) -> float:
    """after より後の次の実行時刻（UNIX 秒）。もうない場合は 0。"""
    hh, mm = map(int, t.time.split(":"))
    if t.kind == "interval":
        return (after + timedelta(minutes=t.every_minutes)).timestamp()
    if t.kind == "once":
        at = datetime.strptime(f"{t.date} {t.time}", "%Y-%m-%d %H:%M")
        return at.timestamp() if at > after else 0.0
    day = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for _ in range(8):
        ok = (t.kind == "daily" or (t.kind == "weekdays" and day.weekday() < 5)
              or (t.kind == "weekly" and day.weekday() == t.weekday))
        if ok and day > after:
            return day.timestamp()
        day += timedelta(days=1)
    return 0.0


class TaskStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[Task]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for d in raw:
            try:
                out.append(Task(**{k: v for k, v in d.items() if k in Task.__dataclass_fields__}))
            except TypeError:
                continue
        return out

    def _write(self, tasks: list[Task]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(t) for t in tasks], ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, tid: str) -> Task:
        for t in self.list():
            if t.id == tid:
                return t
        raise KeyError(tid)

    def create(self, **kw) -> Task:
        t = Task(id=uuid.uuid4().hex[:12], **kw)
        return self.save(t)

    def save(self, t: Task, *, reschedule: bool = True) -> Task:
        check(t)
        if reschedule:
            t.next_run = next_run(t, datetime.now()) if t.enabled else 0.0
        tasks = [x for x in self.list() if x.id != t.id] + [t]
        self._write(sorted(tasks, key=lambda x: x.created_at))
        return t

    def delete(self, tid: str) -> None:
        self._write([t for t in self.list() if t.id != tid])

    def due(self, now: float) -> list[Task]:
        return [t for t in self.list() if t.enabled and t.next_run and t.next_run <= now]
