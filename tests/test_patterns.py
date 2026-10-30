import json
from pathlib import Path

import pytest

from lao.backends.fake import FakeLlama, _call, _msg, _text, fake_engine_factory
from lao.jobs import JobManager
from lao.library import Library
from lao.media import Media
from lao.memory import MemoryGovernor
from lao.orchestrator import Orchestrator, TurnContext
from lao.p0.pairs import _args_checks, Task
from lao.patterns import (MAX_STEPS, PatternError, approve, check, load_allowlist, load_pattern, patterns_for,
                          scaffold, shortlist, validate_recipe)
from lao.runtime import Runtime

UPSCALE = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
    "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": "4x-ultrasharp.pth"}},
    "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
    "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "lao"}},
}
SHEET = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "qwen-image.safetensors"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
    "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
    "5": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 30, "cfg": 4.0, "denoise": 1.0, "model": ["1", 0],
                                               "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
    "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
    "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "lao"}},
}


def make_pattern(folder: Path, pid: str, wf: dict, does: str, good_for: list[str], work_gb: float = 4) -> Path:
    src = folder.parent / f"{pid}-export.json"
    folder.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps(wf), encoding="utf-8")
    toml = scaffold(src, folder, pid)
    text = toml.read_text(encoding="utf-8")
    text = text.replace('does = ""', f'does = "{does}"', 1)
    text = text.replace("good_for = []", f"good_for = {json.dumps(good_for, ensure_ascii=False)}", 1)
    text = text.replace("work_gb = 0", f"work_gb = {work_gb}", 1)
    if "4" in wf and wf["4"]["class_type"] == "EmptyLatentImage":
        text = text.replace("[bindings]", '[bindings]\nwidth = "4.inputs.width"\nheight = "4.inputs.height"', 1)
    toml.write_text(text, encoding="utf-8")
    return toml


@pytest.fixture
def pat_dir(catalog_dir):
    return catalog_dir / "patterns" / "e2-qwen-image"


@pytest.fixture
def approved(catalog_dir, pat_dir):
    allowed = load_allowlist(catalog_dir)
    paths = [make_pattern(pat_dir, "upscale-2x", UPSCALE, "2 倍に拡大して細部を足す", ["ポスター用に大きく", "細部をくっきり"]),
             make_pattern(pat_dir, "character-sheet", SHEET, "同じキャラクターの三面図を描く", ["三面図", "キャラクターの設定画"])]
    for p in paths:
        assert approve(load_pattern(p), allowed) == []
    return paths


def test_scaffold_finds_bindings_and_approve_records_hash(catalog_dir, pat_dir, approved):
    p = load_pattern(approved[1], "imagegen")
    assert p.bindings["prompt"] == "2.inputs.text" and p.bindings["negative"] == "3.inputs.text"
    assert p.bindings["seed"] == "5.inputs.seed" and set(p.params) == {"steps", "cfg", "denoise"}
    assert p.models == ["qwen-image.safetensors"] and p.usable
    up = load_pattern(approved[0])
    assert up.images == ["image"] and up.bindings["image"] == "1.inputs.image"
    # 承認した後にワークフローが変わったら、承認し直すまで使えない
    wf = json.loads(up.workflow_path.read_text())
    wf["2"]["inputs"]["model_name"] = "other.pth"
    up.workflow_path.write_text(json.dumps(wf))
    assert not load_pattern(approved[0]).usable


def test_check_rejects_unsafe_workflows(catalog_dir, pat_dir):
    allowed = load_allowlist(catalog_dir)
    bad = dict(UPSCALE)
    bad["5"] = {"class_type": "LoadImageFromUrl", "inputs": {"url": "https://example.com/a.png"}}
    bad["2"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": "../../secret.pth"}}
    bad["4"] = {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "C:/Users/x/out"}}
    toml = make_pattern(pat_dir, "bad", bad, "危ない", [])
    text = toml.read_text().replace('image = "1.inputs.image"\n', "").replace('images = ["image"]', "images = []")
    toml.write_text(text)
    problems = check(load_pattern(toml), allowed)
    joined = "\n".join(problems)
    assert "LoadImageFromUrl" in joined and "URL" in joined
    assert "パスが含まれて" in joined and "保存先" in joined
    assert "lao が差し込む" in joined  # LoadImage を固定のファイル名で読ませない
    assert approve(load_pattern(toml), allowed) and 'approved = ""' in toml.read_text()


def test_shortlist_filters_by_image_and_ranks_by_request(catalog, catalog_dir, approved):
    pats = patterns_for(catalog_dir, catalog["e2"].configurations["e2-standard"])
    assert {p.id for p in pats if p.usable} == {"upscale-2x", "character-sheet"}
    # 画像が無ければ、画像の要るパターンは見せない
    assert [p.id for p in shortlist(pats, "ポスター用に大きく", has_image=False)] == ["character-sheet"]
    assert [p.id for p in shortlist(pats, "ポスター用に大きくして", has_image=True)][0] == "upscale-2x"
    assert [p.id for p in shortlist(pats, "キャラクターの三面図", has_image=True)][0] == "character-sheet"
    assert len(shortlist(pats, "x", True, limit=1)) == 1


def test_validate_recipe(catalog, catalog_dir, approved, tmp_path, sample_image):
    pats = patterns_for(catalog_dir, catalog["e2"].configurations["e2-standard"])
    lib = Library(tmp_path / "lib")
    ref = lib.add_file(sample_image, env_id="e2", configuration="c").ref
    with pytest.raises(PatternError, match="画像が要ります"):
        validate_recipe({"steps": [{"pattern": "upscale-2x"}]}, pats, lib, 4_194_304)
    with pytest.raises(PatternError, match="使えません"):
        validate_recipe({"steps": [{"pattern": "nope"}]}, pats, lib, 4_194_304)
    with pytest.raises(PatternError, match="手までです"):
        validate_recipe({"steps": [{"pattern": "character-sheet"}] * (MAX_STEPS + 1)}, pats, lib, 4_194_304)
    with pytest.raises(PatternError, match="steps は"):
        validate_recipe({"steps": [{"pattern": "character-sheet", "steps": 500}]}, pats, lib, 4_194_304)
    with pytest.raises(PatternError, match="無い値"):
        validate_recipe({"steps": [{"pattern": "upscale-2x", "image_ref": ref, "cfg": 3}]}, pats, lib, 4_194_304)
    steps = validate_recipe(json.dumps({"steps": [{"pattern": "character-sheet", "prompt": "a knight", "aspect": "16:9"},
                                                  {"pattern": "upscale-2x"}]}), pats, lib, 4_194_304)
    assert steps[0]["steps"] == 30 and steps[1]["output"] == "image"  # 省いた値は既定値


class RecipeChat(FakeLlama):
    async def chat(self, messages, *, tools=None, max_tokens=2048, temperature=None):
        self.calls.append(messages)
        self.tools = tools
        if messages[-1]["role"] == "tool":
            res = json.loads(messages[-1]["content"])
            return _msg(f"できました：{res.get('final')}" if res.get("ok") else f"だめでした：{res.get('error')}")
        user = _text(messages[-1])
        if "設定画" in user:
            return _call("run_recipe", {"steps": [{"pattern": "character-sheet", "prompt": "a knight", "seed": 3},
                                                  {"pattern": "upscale-2x"}]})
        return _call("generate_image", {"prompt": "x", "steps": 20, "cfg": 3.5})


def make_orch(tmp_path, machine, catalog, catalog_dir, chat_cls=RecipeChat):
    e = catalog["e2"]
    c = e.configurations["e2-standard"]
    base = fake_engine_factory()

    def factory(env, conf, comp):
        return chat_cls(comp.key) if comp.role == "chat" else base(env, conf, comp)

    gov = MemoryGovernor(96, 1.0, 0, probe=lambda: (128, 128))
    rt = Runtime(machine, e, c, factory, governor=gov)
    lib = Library(tmp_path / "lib")
    jobs = JobManager(tmp_path / "jobs")
    return rt, lib, jobs, Orchestrator(rt, lib, jobs, Media(), tmp_path / "work", catalog_dir)


async def test_recipe_runs_steps_and_chains_outputs(tmp_path, machine, catalog, catalog_dir, approved):
    rt, lib, jobs, orch = make_orch(tmp_path, machine, catalog, catalog_dir)
    events = [ev async for ev in orch.respond([{"role": "user", "content": "騎士の設定画を作って大きくして"}])]
    recipe = next(t for t in rt.engine("chat").tools if t["function"]["name"] == "run_recipe")
    # 画像が無い依頼なので、画像の要る upscale-2x は候補に出ない（それでも手順の 2 手目には使える）
    assert recipe["function"]["parameters"]["properties"]["steps"]["items"]["properties"]["pattern"]["enum"] == [
        "character-sheet"]
    job = next(j for j in jobs.list() if j.kind == "run_recipe")
    assert job.state == "done" and len(job.result) == 2, job.error
    first, final = (lib.get(r) for r in job.result)
    assert final.sources == [first.ref] and final.generation["pattern"] == "upscale-2x"
    assert first.generation["seed"] == 3 and first.generation["steps"] == 30
    comfy = rt.engine("imagegen").comfy
    assert comfy.workflows[0]["2"]["inputs"]["text"] == "a knight"
    assert comfy.workflows[1]["1"]["inputs"]["image"].endswith(".png")  # 前の手の出力を渡した
    assert "できました" in "".join(e.text for e in events if e.type == "text")


async def test_generate_image_knobs(tmp_path, machine, catalog, catalog_dir):
    rt, lib, jobs, orch = make_orch(tmp_path, machine, catalog, catalog_dir)
    _, tools = orch._build([{"role": "user", "content": "x"}], [], TurnContext(), 0)
    assert not any(t["function"]["name"] == "run_recipe" for t in tools)  # 承認したパターンが無ければ出さない
    [ev async for ev in orch.respond([{"role": "user", "content": "猫"}])]
    job = jobs.list()[0]
    assert job.state == "done", job.error
    wf = rt.engine("imagegen").comfy.workflows[0]
    assert wf["2"]["inputs"]["steps"] == 20 and wf["2"]["inputs"]["cfg"] == 3.5
    with pytest.raises(Exception, match="lora"):
        orch._knobs("imagegen", {"lora": "anime.safetensors"})


def test_pairs_checks_expected_pattern():
    task = Task("t", "edit", "r", ["c"], {"pattern": "upscale-2x"}, "f", ["c"])
    turn0 = {"invalid": 0, "errors": [], "text": "", "calls": [{"tool": "generate_image", "args": {}, "ok": True,
                                                                  "refs": ["a"]}]}
    used = {"invalid": 0, "errors": [], "text": "", "calls": [{"tool": "run_recipe", "ok": True, "refs": ["b"],
                                                                 "args": {"steps": [{"pattern": "upscale-2x"}]}}]}
    other = {"invalid": 0, "errors": [], "text": "", "calls": [{"tool": "edit_image", "args": {}, "ok": True,
                                                                  "refs": ["b"]}]}
    rec = {"turns": [turn0, used], "images": ["a"]}
    assert all(_args_checks(rec, task, ["upscale-2x"]))
    assert not all(_args_checks({"turns": [turn0, other], "images": ["a"]}, task, ["upscale-2x"]))
    # その生成モデルにパターンが無ければ、選び方は問わない
    assert all(_args_checks({"turns": [turn0, other], "images": ["a"]}, task, []))


def test_remote_api_lists_and_runs_patterns(machine, catalog_dir, approved):
    import time

    from fastapi.testclient import TestClient

    from lao.server import create_app
    from lao.service import Service

    svc = Service(machine, catalog_dir, fake_engine_factory(), memory_probe=lambda: (128.0, 128.0))
    with TestClient(create_app(svc, "k")) as c:
        c.headers["Authorization"] = "Bearer k"
        c.post("/api/environment", json={"env": "e2"})
        assert "run_recipe" in c.get("/api/status").json()["tools"]
        items = {p["id"]: p for p in c.get("/api/patterns").json()["patterns"]}
        assert items["upscale-2x"]["needs_image"] and items["character-sheet"]["params"]["steps"]["default"] == 30
        bad = c.post("/api/run/run_recipe", json={"steps": [{"pattern": "upscale-2x"}]})
        assert bad.status_code == 400 and "画像が要ります" in bad.json()["detail"]
        j = c.post("/api/run/run_recipe", json={"steps": [{"pattern": "character-sheet", "prompt": "a cat"},
                                                          {"pattern": "upscale-2x"}]}).json()
        for _ in range(100):
            j = c.get(f"/api/jobs/{j['id']}").json()
            if j["state"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert j["state"] == "done" and len(j["result"]) == 2, j
