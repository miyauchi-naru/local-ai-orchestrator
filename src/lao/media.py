"""メディアの検査と分割（企画書 3.2 節）。

llama.cpp の動画入力は映像フレームのみで音声トラックを扱わない（2026年6月時点）。
そこで ffmpeg で動画を時間区間に分け、区間ごとに「フレーム列＋同じ区間の音声」を作り、
画像と音声の両方を受けられるモデルへ一つのプロンプトで渡す。
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class MediaError(ValueError):
    pass


@dataclass
class MediaInfo:
    path: Path
    duration: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0


@dataclass
class Segment:
    start: float
    end: float
    frames: list[Path] = field(default_factory=list)
    audio: Path | None = None

    @property
    def label(self) -> str:
        return f"{_ts(self.start)}–{_ts(self.end)}"


def _ts(sec: float) -> str:
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:05.2f}"


class Media:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"):
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe

    def probe(self, path: Path) -> MediaInfo:
        try:
            out = subprocess.run(
                [self.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                capture_output=True, text=True, check=True, timeout=60,
            ).stdout
        except subprocess.CalledProcessError as e:
            raise MediaError(f"{path.name} を読めません（壊れているか非対応の形式）：{e.stderr.strip()[:300]}") from e
        d = json.loads(out)
        streams = d.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        dur = float(d.get("format", {}).get("duration") or 0)
        fps = 0.0
        if v and v.get("avg_frame_rate", "0/0") != "0/0":
            n, dn = v["avg_frame_rate"].split("/")
            fps = float(n) / float(dn) if float(dn) else 0.0
        return MediaInfo(path, dur, v is not None, a is not None,
                         int(v.get("width", 0)) if v else 0, int(v.get("height", 0)) if v else 0, fps)

    def split_video(
        self,
        path: Path,
        work: Path,
        *,
        start: float = 0.0,
        end: float | None = None,
        segment_seconds: float = 10.0,
        frames_per_second: float = 1.0,
        max_side: int = 768,
    ) -> list[Segment]:
        info = self.probe(path)
        if not info.has_video:
            raise MediaError(f"{path.name} に映像がありません")
        end = min(end if end is not None else info.duration, info.duration)
        if end <= start:
            raise MediaError("指定区間が空です")
        work.mkdir(parents=True, exist_ok=True)
        segments: list[Segment] = []
        t = start
        i = 0
        while t < end - 1e-6:
            t1 = min(t + segment_seconds, end)
            seg = Segment(t, t1)
            fdir = work / f"seg{i:04d}"
            fdir.mkdir(exist_ok=True)
            scale = f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
            self._run([
                "-ss", f"{t:.3f}", "-to", f"{t1:.3f}", "-i", str(path),
                "-vf", f"fps={frames_per_second},{scale}", "-q:v", "3", str(fdir / "f%04d.jpg"),
            ])
            seg.frames = sorted(fdir.glob("f*.jpg"))
            if info.has_audio:
                wav = fdir / "audio.wav"
                self._run(["-ss", f"{t:.3f}", "-to", f"{t1:.3f}", "-i", str(path),
                           "-vn", "-ac", "1", "-ar", "16000", str(wav)])
                seg.audio = wav
            segments.append(seg)
            t, i = t1, i + 1
        return segments

    def to_wav(self, path: Path, out: Path, start: float = 0.0, end: float | None = None) -> Path:
        args = ["-ss", f"{start:.3f}"]
        if end is not None:
            args += ["-to", f"{end:.3f}"]
        self._run(args + ["-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(out)])
        return out

    def concat_videos(self, parts: list[Path], out: Path) -> Path:
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        self._run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
        return out

    def concat_audio(self, parts: list[Path], out: Path) -> Path:
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        self._run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "pcm_s16le", str(out)])
        return out

    def _run(self, args: list[str]) -> None:
        r = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise MediaError(f"ffmpeg が失敗しました：{r.stderr.strip()[:500]}")


def image_part(path: Path) -> dict:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def audio_part(path: Path) -> dict:
    fmt = "mp3" if path.suffix.lower() == ".mp3" else "wav"
    return {"type": "input_audio", "input_audio": {"data": base64.b64encode(path.read_bytes()).decode(), "format": fmt}}


def segment_message(seg: Segment, question: str) -> dict:
    """区間のフレームと音声を一つのユーザーメッセージにまとめる。"""
    parts: list[dict] = [{
        "type": "text",
        "text": (f"以下は動画の {seg.label} の区間です。{len(seg.frames)} 枚のフレームを時刻順に並べ、"
                 f"{'同じ区間の音声も付けています' if seg.audio else 'この動画に音声はありません'}。\n{question}\n"
                 "映像と音声のそれぞれで起きたことを、区間内のおおよその時刻付きで答えてください。"),
    }]
    parts += [image_part(f) for f in seg.frames]
    if seg.audio:
        parts.append(audio_part(seg.audio))
    return {"role": "user", "content": parts}
