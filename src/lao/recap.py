"""月のふりかえり。保存してある会話と成果物から、その月の使い方を数える。

数えるだけで、どこにも送らない。シークレットの会話は保存されないので数に入らない。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta

from .conversations import Conversation
from .library import Artifact

MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def month_range(month: str) -> tuple[float, float]:
    if not MONTH_RE.fullmatch(month):
        raise ValueError("月は YYYY-MM で書いてください")
    y, m = map(int, month.split("-"))
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), m % 12 + 1, 1)
    return start.timestamp(), end.timestamp()


def previous(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


def _streak(days: set[date]) -> int:
    best = run = 0
    for d in sorted(days):
        run = run + 1 if d - timedelta(days=1) in days else 1
        best = max(best, run)
    return best


def recap(conversations: list[Conversation], artifacts: list[Artifact], month: str) -> dict:
    start, end = month_range(month)
    p_start, p_end = month_range(previous(month))
    hours, weekdays, envs, commands, per_day = Counter(), Counter(), Counter(), Counter(), Counter()
    per_conv: Counter = Counter()
    titles: dict[str, str] = {}
    user_msgs = replies = typed = received = late = prev_msgs = 0
    started = 0
    for c in conversations:
        if start <= c.created_at < end:
            started += 1
        for t in c.turns:
            if p_start <= t.created_at < p_end and t.role == "user":
                prev_msgs += 1
            if not start <= t.created_at < end:
                continue
            if t.role == "assistant":
                replies += 1
                received += len(t.content)
                continue
            at = datetime.fromtimestamp(t.created_at)
            user_msgs += 1
            typed += len(t.content)
            hours[at.hour] += 1
            weekdays[at.weekday()] += 1
            per_day[at.date()] += 1
            envs[c.env_id] += 1
            per_conv[c.id] += 1
            titles[c.id] = c.title
            if t.command:
                commands[t.command] += 1
            if at.hour < 5:
                late += 1
    made = [a for a in artifacts if start <= a.created_at < end and a.configuration != "upload"]
    busiest = per_day.most_common(1)
    return {
        "month": month,
        "messages": user_msgs,
        "replies": replies,
        "previous_messages": prev_msgs,
        "conversations": len(per_conv),
        "started": started,
        "active_days": len(per_day),
        "longest_streak": _streak(set(per_day)),
        "busiest_day": {"date": busiest[0][0].isoformat(), "messages": busiest[0][1]} if busiest else None,
        "by_hour": [hours[h] for h in range(24)],
        "by_weekday": [weekdays[d] for d in range(7)],
        "by_env": dict(envs.most_common()),
        "artifacts": dict(Counter(a.kind for a in made).most_common()),
        "artifacts_by_env": dict(Counter(a.env_id for a in made).most_common()),
        "commands": dict(commands.most_common(5)),
        "top_conversations": [{"id": cid, "title": titles[cid], "messages": n} for cid, n in per_conv.most_common(5)],
        "chars_typed": typed,
        "chars_received": received,
        "late_night_messages": late,
    }
