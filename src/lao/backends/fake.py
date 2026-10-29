"""テスト・画面確認用のダミーエンジン（lao serve --fake）。

本物のモデルなしで、ロード・ロック・ジョブ・成果物・画面の流れを確かめるためのもの。
ffmpeg でそれらしい画像・音声・動画を作って返す。
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

REF = re.compile(r"[0-9a-f]{12}@\d+")


class FakeLlama:
    def __init__(self, name: str, delay: float = 0.0):
        self.name, self.delay = name, delay
        self._running = False
        self.calls: list[list[dict]] = []

    @property
    def running(self) -> bool:
        return self._running

    async def load(self) -> None:
        await asyncio.sleep(self.delay)
        self._running = True

    async def unload(self) -> None:
        self._running = False

    async def count_tokens(self, messages, tools=None) -> int:
        text = json.dumps(messages, ensure_ascii=False) + json.dumps(tools or [], ensure_ascii=False)
        return len(text) // 3

    async def chat(self, messages, *, tools=None, max_tokens=2048, temperature=None) -> dict:
        assert self._running, "not loaded"
        self.calls.append(messages)
        await asyncio.sleep(self.delay)
        last = messages[-1]
        if last["role"] == "tool":
            res = json.loads(last["content"])
            if "result" in res:
                return _msg(f"サブエージェントの結果：{res['result'][:200]}")
            if res.get("skill"):
                return _msg(f"スキル「{res['skill']}」の手順に従って答えます。")
            if res.get("ok"):
                text = f"できました：{', '.join(res.get('artifacts', []))}"
                if res.get("output"):
                    text += f"\n{res['output']}"
            else:
                text = f"できませんでした：{res.get('error')}"
            return _msg(text)
        if not tools:
            # 理解モデルとしての応答
            kinds = [p.get("type") for p in last.get("content", []) if isinstance(p, dict)]
            return _msg(f"（ダミー理解）受け取った入力：{', '.join(kinds)}")
        names = {t["function"]["name"] for t in tools}
        user = _text(last)
        system = messages[0]["content"]
        refs = REF.findall(user) or REF.findall(system)
        if "任せて" in user and "delegate_task" in names:
            return _call("delegate_task", {"task": user[:200]})
        skill = re.search(r"^- (\S+)：", system, re.M) if "スキル" in user else None
        if skill and "use_skill" in names:
            return _call("use_skill", {"name": skill.group(1)})
        if "動画" in user and "generate_video" in names and refs:
            return _call("generate_video", {"image_ref": refs[-1], "prompt": "a gentle camera pan", "seconds": 2})
        if "画像" in user and "generate_image" in names:
            return _call("generate_image", {"prompt": user[:200], "width": 512, "height": 512, "seed": 1})
        if "読み上げ" in user and "generate_speech" in names:
            return _call("generate_speech", {"text": user})
        if refs and "describe_media" in names:
            return _call("describe_media", {"ref": refs[-1], "question": user or "内容を説明して"})
        return _msg(f"（ダミー応答）{user[:200]}")


    async def chat_stream(self, messages, *, tools=None, max_tokens=2048):
        msg = (await self.chat(messages, tools=tools, max_tokens=max_tokens))["choices"][0]["message"]
        if "考えて" in _text(messages[-1]):
            msg["reasoning_content"] = "（ダミー思考）順に考えます。"
            yield {"reasoning": msg["reasoning_content"]}
        text = msg.get("content") or ""
        for i in range(0, len(text), 8):
            await asyncio.sleep(self.delay / 20)
            yield {"content": text[i:i + 8]}
        yield {"message": msg}


def _text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c if p.get("type") == "text")
    return c or ""


def _msg(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _call(name: str, args: dict) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
    ]}}]}


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


class FakeComfy:
    def __init__(self, out_kind: str, delay: float = 0.0):
        self.out_kind, self.delay = out_kind, delay
        self.running = True
        self.interrupted = False
        self.workflows: list[dict] = []

    async def upload_image(self, path: Path) -> str:
        return path.name

    async def interrupt(self) -> None:
        self.interrupted = True

    async def run(self, workflow, out_dir: Path, on_progress=None, poll=1.0) -> list[Path]:
        self.workflows.append(workflow)
        out_dir.mkdir(parents=True, exist_ok=True)
        steps = 4
        for i in range(steps):
            if self.interrupted:
                raise RuntimeError("interrupted")
            await asyncio.sleep(self.delay / steps)
            if on_progress:
                on_progress(None, "生成中")
        if self.out_kind == "image":
            out = out_dir / "fake.png"
            _ffmpeg(["-f", "lavfi", "-i", "testsrc=size=256x256:rate=1", "-frames:v", "1", str(out)])
        else:
            out = out_dir / "fake.mp4"
            _ffmpeg(["-f", "lavfi", "-i", "testsrc=size=256x256:rate=24", "-t", "2", "-pix_fmt", "yuv420p", str(out)])
        return [out]


class FakeComfyComponent:
    def __init__(self, comfy: FakeComfy):
        self.comfy = comfy
        self._loaded = False

    @property
    def running(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        self._loaded = True

    async def unload(self) -> None:
        self._loaded = False


class FakeTTS:
    async def synthesize(self, out: Path, **params) -> Path:
        _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", str(out)])
        return out


class FakeTTSComponent:
    def __init__(self):
        self.tts = FakeTTS()
        self._loaded = False

    @property
    def running(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        self._loaded = True

    async def unload(self) -> None:
        self._loaded = False


def fake_engine_factory(delay: float = 0.0):
    def factory(env, conf, c):
        if c.engine == "llama":
            return FakeLlama(f"{env.id}-{c.key}", delay)
        if c.engine == "comfyui":
            kind = "video" if c.role == "video_gen" else "image"
            return FakeComfyComponent(FakeComfy(kind, delay))
        if c.engine == "tts":
            return FakeTTSComponent()
        raise ValueError(c.engine)
    return factory
