"""メディアの検査と分割（企画書 3.2 節）。

llama.cpp の動画入力（input_video）で音声トラックまで聞けるのは一部のモデル（LFM3-Omni）だけ
（2026年10月時点）。それ以外のモデルでは ffmpeg で動画を時間区間に分け、区間ごとに「フレーム列＋同じ区間の音声」を作り、
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

    def concat_videos(self, parts: list[Path], out: Path, reencode: bool = False) -> Path:
        """動画をつなぐ。形式が揃っていなければ（大きさ・音声の有無が違う等）作り直してつなぐ。"""
        if not reencode:
            lst = out.with_suffix(".txt")
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
            try:
                self._run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
                return out
            except ValueError:
                pass
        infos = [self.probe(p) for p in parts]
        w, h = (infos[0].width or 1280), (infos[0].height or 720)
        args, chains = [], []
        for p in parts:
            args += ["-i", str(p)]
        for i, info in enumerate(infos):
            chains.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                          f"setsar=1,fps=24,format=yuv420p[v{i}]")
            if info.has_audio:
                chains.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
            else:
                chains.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={info.duration:.3f}[a{i}]")
        joined = "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
        chains.append(f"{joined}concat=n={len(parts)}:v=1:a=1[v][a]")
        self._run(args + ["-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                          "-preset", "veryfast", "-c:a", "aac", str(out)])
        return out

    def mux_audio(self, video: Path, audio: Path, out: Path, mix: bool = False) -> Path:
        """動画に音声を付ける。mix なら元の音と混ぜ、そうでなければ置き換える。長さは動画に合わせる。"""
        info = self.probe(video)
        if mix and info.has_audio:
            fc = "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]"
        else:
            fc = f"[1:a]apad,atrim=duration={info.duration:.3f}[a]"
        self._run(["-i", str(video), "-i", str(audio), "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
                   "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)])
        return out

    def contact_sheet(self, images: list[Path], out: Path, size: int = 256) -> Path:
        """画像を縮小して横に並べた 1 枚にする（4 枚を超えると 2 段以上）。"""
        cols = min(4, len(images))
        rows = -(-len(images) // cols)
        args, chains = [], []
        for i, p in enumerate(images):
            args += ["-i", str(p)]
            chains.append(f"[{i}:v]scale={size}:{size}:force_original_aspect_ratio=decrease,"
                          f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color=white[t{i}]")
        layout = "|".join(f"{(i % cols) * size}_{(i // cols) * size}" for i in range(len(images)))
        if len(images) == 1:
            chains.append("[t0]copy[o]")
        else:
            chains.append("".join(f"[t{i}]" for i in range(len(images))) +
                          f"xstack=inputs={len(images)}:layout={layout}:fill=white[o]")
        self._run(args + ["-filter_complex", ";".join(chains), "-map", "[o]", "-frames:v", "1", "-q:v", "4", str(out)])
        return out

    def trim(self, path: Path, out: Path, start: float = 0.0, end: float | None = None) -> Path:
        args = ["-ss", f"{start:.3f}"] + (["-to", f"{end:.3f}"] if end is not None else [])
        self._run(args + ["-i", str(path), "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(out)])
        return out

    def thumbnail(self, path: Path, out: Path, size: int = 256, tiles: int = 4) -> Path:
        """長辺 size の縮小 JPEG を作る。動画は等間隔の tiles コマを横に並べた 1 枚にする。"""
        scale = f"scale='if(gt(iw,ih),{size},-2)':'if(gt(iw,ih),-2,{size})'"
        info = self.probe(path)
        if not info.has_video:
            raise MediaError(f"{path.name} に映像がありません")
        if info.duration > 0.5 and path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            vf = f"fps={tiles / info.duration:.6f},{scale},tile={tiles}x1"
        else:
            vf = scale
        self._run(["-i", str(path), "-vf", vf, "-frames:v", "1", "-q:v", "5", str(out)])
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


def video_part(path: Path) -> dict:
    """llama-server の input_video（音声トラックも聞けるモデルではサウンドトラックも渡る）。"""
    return {"type": "input_video", "input_video": {"data": base64.b64encode(path.read_bytes()).decode()}}


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
