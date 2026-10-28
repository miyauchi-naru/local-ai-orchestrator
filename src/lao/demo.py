"""ダミーエンジン用のカタログを作る（テストと lao serve --fake 用）。

本番のカタログをコピーし、未設定の ComfyUI 差し込み先と最小のワークフローを補う。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

_WORKFLOW = {
    "1": {"class_type": "Prompt", "inputs": {}},
    "2": {"class_type": "Sampler", "inputs": {}},
    "3": {"class_type": "Image", "inputs": {}},
}
_TARGETS = {
    "prompt": "1.inputs.text", "negative": "1.inputs.negative", "seed": "2.inputs.seed",
    "width": "2.inputs.width", "height": "2.inputs.height", "image": "3.inputs.image",
    "seconds": "2.inputs.seconds", "fps": "2.inputs.fps", "frames": "2.inputs.frames",
}


def make_demo_catalog(src: Path, dst: Path) -> Path:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    for f in dst.glob("*.toml"):
        text = f.read_text(encoding="utf-8")

        def fill(m: re.Match) -> str:
            body = m.group(1)
            for k, v in _TARGETS.items():
                body = re.sub(rf'\b{k} = ""', f'{k} = "{v}"', body)
            return "bindings = {" + body + "}"

        text = re.sub(r"bindings = \{(.*?)\}", fill, text)
        f.write_text(text, encoding="utf-8")
        for wf in re.findall(r'workflow = "([^"]+)"', text):
            p = dst / wf
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_WORKFLOW), encoding="utf-8")
    return dst


def demo_machine(root: Path) -> dict:
    return {
        "machine": {"name": "demo", "expected_total_memory_gb": 128, "runtime": "native"},
        "paths": {k: str(root / k) for k in ("root", "models", "engines", "library", "jobs")},
        "memory": {"budget_gb": 96, "gpu_allocatable_gb": 0, "safety_factor": 1.15, "min_free_system_gb": 0},
        "policy": {"allow_publisher_quantized": True, "allow_third_party_quantized": False,
                   "allow_network_egress": False},
        "server": {"host": "127.0.0.1", "port": 8710, "api_key": ""},
        "engines": {"llama": {"binary": "", "port_base": 8720, "extra_args": []},
                    "ffmpeg": {"binary": "ffmpeg", "ffprobe": "ffprobe"}},
        "gpu": {"video_segment_seconds": 5, "tts_chunk_chars": 200, "watchdog_seconds": 60},
        "measured": {},
    }
