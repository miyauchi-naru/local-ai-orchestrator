"""Claude のトークンの記録：lao に任せた分の見積もりと、メイン PC の Claude Code が実際に使った量。

- lao mcp の道具の呼び出しごとに、Claude に返した量・Spark が代わりに読んだ量・節約できた量の見積もりを
  root/usage/lao-mcp-YYYY-MM.jsonl に残す（数えるだけで、どこにも送らない）
- メイン PC の spark.ps1 usage が、Claude Code の会話記録から日ごとの実際の使用量を数えて
  root/usage/claude-code/YYYY-MM-DD.json に置く
- report() が月ごとにまとめる（画面のふりかえり、lao mcp の usage、lao usage）

トークンは見積もり。文字は英数字 4 文字で 1、それ以外（日本語など）は 1 文字で 1 と数える。
画像は Claude が 1 枚見ると約 1,600 トークン（長辺 1,000px 前後の画像の目安）として数える。
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .recap import MONTH_RE

IMAGE_TOKENS = 1600          # Claude が画像を 1 枚見たときの目安
VIDEO_FRAMES = 8             # Claude が動画を確かめるなら、少なくともこのくらいのコマを見る
# 道具の分類
CANNOT = {"generate_image", "edit_image", "generate_video", "generate_speech", "make_clip", "combine"}
SAVING = {"ask_local", "describe_media"}
CATEGORY_LABELS = {"cannot": "Claude にできないこと", "saved": "トークンの節約", "other": "その他"}


def est_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_n / 4) + (len(text) - ascii_n)


def thumb_tokens(width: int, height: int) -> int:
    """Claude が画像を見たときのトークン（幅×高さ÷750 が目安）。"""
    return max(1, math.ceil(width * height / 750))


def category(tool: str, media_kind: str = "") -> str:
    if tool in CANNOT:
        return "cannot"
    if tool == "describe_media" and media_kind in ("audio", "video"):
        return "cannot"  # Claude は音声を聞けない。動画も音声ごとは見られない
    if tool in SAVING:
        return "saved"
    return "other"


class UsageLog:
    def __init__(self, root: Path):
        self.root = root

    def _file(self, ts: float) -> Path:
        return self.root / f"lao-mcp-{datetime.fromtimestamp(ts).strftime('%Y-%m')}.jsonl"

    def add(self, rec: dict) -> dict:
        rec = {"ts": time.time(), **rec}
        rec["tool"] = str(rec.get("tool", ""))[:40]
        for k in ("sent_tokens", "returned_tokens", "offloaded_tokens", "saved_tokens"):
            rec[k] = max(0, int(rec.get(k) or 0))
        rec["seconds"] = round(float(rec.get("seconds") or 0), 1)
        rec["category"] = rec.get("category") if rec.get("category") in CATEGORY_LABELS else "other"
        rec["ok"] = bool(rec.get("ok", True))
        self.root.mkdir(parents=True, exist_ok=True)
        with self._file(rec["ts"]).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def records(self, month: str) -> list[dict]:
        p = self.root / f"lao-mcp-{month}.jsonl"
        out = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def claude_code_days(self, month: str) -> list[dict]:
        d = self.root / "claude-code"
        out = []
        for p in sorted(d.glob(f"{month}-*.json")) if d.is_dir() else []:
            try:
                out.append(json.loads(p.read_text(encoding="utf-8-sig")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def report(self, month: str = "") -> dict:
        month = month or datetime.now().strftime("%Y-%m")
        if not MONTH_RE.fullmatch(month):
            raise ValueError("月は YYYY-MM で書いてください")
        recs = self.records(month)
        by_tool: dict[str, Counter] = defaultdict(Counter)
        by_cat: dict[str, Counter] = defaultdict(Counter)
        per_day: dict[str, Counter] = defaultdict(Counter)
        for r in recs:
            day = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
            for c in (by_tool[r["tool"]], by_cat[r["category"]], per_day[day]):
                c["calls"] += 1
                c["returned_tokens"] += r["returned_tokens"]
                c["offloaded_tokens"] += r["offloaded_tokens"]
                c["saved_tokens"] += r["saved_tokens"]
                c["seconds"] += r["seconds"]
        cc_days = self.claude_code_days(month)
        cc = Counter()
        for d in cc_days:
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                      "messages", "lao_calls"):
                cc[k] += int(d.get(k) or 0)
            per_day[d.get("date", "")]["claude_code_tokens"] += _cc_total(d)
            per_day[d.get("date", "")]["claude_code_lao_calls"] += int(d.get("lao_calls") or 0)
        saved = sum(r["saved_tokens"] for r in recs)
        cc_total = _cc_total(cc)
        return {
            "month": month,
            "calls": len(recs),
            "saved_tokens": saved,
            "returned_tokens": sum(r["returned_tokens"] for r in recs),
            "cannot_calls": by_cat["cannot"]["calls"],
            "cannot_seconds": round(by_cat["cannot"]["seconds"]),
            "by_category": {CATEGORY_LABELS[k]: dict(v) for k, v in by_cat.items()},
            "by_tool": {k: dict(v) for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1]["calls"])},
            "claude_code": {**dict(cc), "total_tokens": cc_total, "days": len(cc_days)},
            # 節約がなかったら Claude Code が使っていたはずの量に対する、節約できた割合
            "saved_share": round(saved / (cc_total + saved), 3) if cc_total + saved else None,
            "per_day": {k: dict(v) for k, v in sorted(per_day.items()) if k},
        }


def _cc_total(d) -> int:
    """Claude Code の 1 日の使用量。キャッシュから読んだ分は安いので 1/10 として数える。"""
    return (int(d.get("input_tokens") or 0) + int(d.get("output_tokens") or 0)
            + int(d.get("cache_creation_input_tokens") or 0) + int(d.get("cache_read_input_tokens") or 0) // 10)


def summary_text(r: dict) -> str:
    lines = [f"{r['month']} の記録（トークンは見積もり）",
             f"- lao に任せた回数：{r['calls']} 回（うち Claude にできないこと {r['cannot_calls']} 回、"
             f"Spark の作業時間 {r['cannot_seconds'] // 60} 分）",
             f"- 節約できたトークン：約 {r['saved_tokens']:,}（Claude に返した量 {r['returned_tokens']:,}）"]
    cc = r["claude_code"]
    if cc.get("days"):
        lines.append(f"- メイン PC の Claude Code が使った量：約 {cc['total_tokens']:,}（{cc['days']} 日分、"
                     f"入力 {cc.get('input_tokens', 0):,}・出力 {cc.get('output_tokens', 0):,}・"
                     f"キャッシュ読み {cc.get('cache_read_input_tokens', 0):,} は 1/10 で計上）")
        if r["saved_share"] is not None:
            lines.append(f"- lao が無かったら使っていた量に対する節約の割合：約 {r['saved_share'] * 100:.0f}%")
    else:
        lines.append("- Claude Code の実際の使用量：まだ届いていません（メイン PC の spark.ps1 usage が毎日送ります）")
    for k, v in list(r["by_tool"].items())[:8]:
        lines.append(f"  - {k}：{v['calls']} 回、節約 {v.get('saved_tokens', 0):,}")
    return "\n".join(lines)
