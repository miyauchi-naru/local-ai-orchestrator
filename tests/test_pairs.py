import json
import re

import pytest

from lao.backends.fake import FakeLlama, _call, _msg, _text, fake_engine_factory
from lao.p0.pairs import (PairsError, Task, agreement, build, combine, judge_refs, load_suite, pairs_of,
                          parse_answer, rank, report_text, run, score_pair)

REF = re.compile(r"[0-9a-f]{12}@\d+")

SUITE = """
[settings]
fix_rounds = 1
tie_points = 3

[candidates]
chat = ["e2.e2-standard.chat", "e1.e1-standard.reasoner"]
image = ["e2.e2-standard.imagegen"]
video = ["e4.e4-ltx.videogen"]
judge = "e1.e1-standard.perceiver"

[[tasks]]
id = "wide"
request = "横長の海の画像"
checks = ["海がある", "横長である"]
expect = { orientation = "landscape", negative = true }

[[tasks]]
id = "edit"
kind = "edit"
request = "赤い車の画像"
checks = ["車がある"]
followup = "車を青に変えて"
followup_checks = ["車が青い"]

[[tasks]]
id = "movie"
kind = "video"
request = "灯台の画像を作って、それを動画にして"
checks = ["灯台がある"]
video_checks = ["波が動いている"]
"""


class ScriptedChat(FakeLlama):
    """課題の言い回しに合わせて、狙いどおりにツールを使う会話モデル。"""

    async def chat(self, messages, *, tools=None, max_tokens=2048, temperature=None):
        self.calls.append(messages)
        last = messages[-1]
        user = next(_text(m) for m in reversed(messages) if m["role"] == "user")
        if last["role"] == "tool":
            res = json.loads(last["content"])
            done = [m for m in messages if m.get("tool_calls")]
            if "動画" in user and len(done) == 1 and res.get("artifacts"):
                return _call("generate_video", {"image_ref": res["artifacts"][0], "prompt": "waves", "seconds": 2})
            return _msg(f"できました：{', '.join(res.get('artifacts') or [])}")
        if "完成" in user:  # 見て直す回：一度だけ作り直す
            return _call("generate_image", {"prompt": "sea, wide", "aspect": "16:9", "negative_prompt": "text"})
        if "変えて" in user:
            ref = REF.findall(json.dumps(messages, ensure_ascii=False))[-1]
            return _call("edit_image", {"ref": ref, "instruction": "make the car blue"})
        if "横長" in user:
            return _call("generate_image", {"prompt": "sea", "aspect": "16:9", "negative_prompt": "text"})
        return _call("generate_image", {"prompt": user[:100]})


def factory():
    base = fake_engine_factory()

    def f(env, conf, c):
        if c.engine == "llama" and c.role == "chat":
            return ScriptedChat(c.key)
        return base(env, conf, c)
    return f


@pytest.fixture
def suite_path(tmp_path):
    p = tmp_path / "pairs.toml"
    p.write_text(SUITE, encoding="utf-8")
    return p


def test_default_suite_loads_and_resolves(catalog):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    suite = load_suite(root / "catalog" / "shared" / "pairs.toml")
    pairs = pairs_of(suite, catalog)
    assert pairs and len({t.id for t in suite.tasks}) == len(suite.tasks)
    assert {t.kind for t in suite.tasks} == {"image", "edit", "video"}


def test_suite_rejects_bad_tasks(tmp_path, catalog):
    p = tmp_path / "bad.toml"
    p.write_text('[candidates]\nchat=["x"]\nimage=["y"]\n[[tasks]]\nid="a"\nkind="edit"\nrequest="r"\nchecks=["c"]\n',
                 encoding="utf-8")
    with pytest.raises(PairsError, match="followup"):
        load_suite(p)
    p.write_text(SUITE.replace('image = ["e2.e2-standard.imagegen"]', 'image = ["e2.e2-standard.chat"]'),
                 encoding="utf-8")
    with pytest.raises(PairsError, match="image_gen"):
        pairs_of(load_suite(p), catalog)


def test_build_renames_components_and_prefers_measured_peak(machine, catalog):
    machine.raw["measured"]["e2.chat"] = {"peak_gb": 30.0}
    suite_pair = pairs_of(load_suite_text(SUITE), catalog)[0]
    env, conf, origin = build(machine, catalog, suite_pair)
    assert set(conf.components) == {"chat", "imagegen", "videogen"}
    assert conf.components["chat"].resident_gb == 30.0
    assert origin["videogen"][1].id == "e4-ltx"
    assert env.required_outputs == ["text", "image", "video"]


def load_suite_text(text):
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "s.toml"
    p.write_text(text, encoding="utf-8")
    return load_suite(p)


def test_parse_answer():
    assert parse_answer("はい。") is True
    assert parse_answer("「いいえ」") is False
    assert parse_answer("Yes") is True
    assert parse_answer("わかりません") is None


def test_run_end_to_end_with_fake_engines(tmp_path, machine, catalog_dir, suite_path):
    asked = []

    async def no(image, question, video):  # gemma の組み合わせ自身の会話モデル：何でも「いいえ」と言う
        return False

    async def judge(image, question, video):
        asked.append((question, video))
        assert image.exists()
        return question != "横長である" or len([q for q, _ in asked if q == "横長である"]) > 1

    out = run(machine, catalog_dir, suite_path, tmp_path / "results", engine_factory=factory(),
              memory_probe=lambda: (128.0, 120.0), judge_asks={"judge-a": judge, "gemma-4-26B-A4B-it": no})
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    by = {r["id"]: r for r in data["results"]}
    gemma = by["e2-standard-chat+e2-standard-imagegen+e4-ltx-videogen"]
    oss = by["e1-standard-reasoner+e2-standard-imagegen+e4-ltx-videogen"]
    assert gemma["can_see"] and not oss["can_see"]
    wide = next(r for r in gemma["records"] if r["task"] == "wide")
    assert len(wide["images"]) == 2  # 1 回直した
    edit = next(r for r in gemma["records"] if r["task"] == "edit")
    assert edit["edited"] and edit["edited"].endswith("@2")  # 修正は同じ id の新しい版
    movie = next(r for r in gemma["records"] if r["task"] == "movie")
    assert movie["video"]
    assert ("波が動いている", True) in asked
    s = gemma["score"]
    assert s["parts"]["args"] == 100.0 and s["parts"]["fix"] == 100.0 and s["parts"]["edit"] == 100.0
    assert oss["score"]["parts"]["fix"] == 0.0  # 画像を見られない
    # 全部品の常駐は予算を超えるので、読み直しが起きる
    assert gemma["estimate_gb"] > data["budget_gb"] and gemma["reloads"] > 0
    assert set(gemma["score"]["by_judge"]) == {"judge-a"}  # 自分の会話モデルの判定は外す
    assert set(oss["score"]["by_judge"]) == {"judge-a", "gemma-4-26B-A4B-it"}
    assert data["agreement"]["all"] is not None
    assert data["ranking"]["recommended"] == gemma["id"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "推奨" in report and gemma["id"] in report and "判定役が全員同じ答えだった割合" in report
    sheet = json.loads((out / "judge-sheet.json").read_text(encoding="utf-8"))
    assert any(it["video"] and it["questions"] == ["波が動いている"] for it in sheet)


def test_no_judge_then_score_from_answers(tmp_path, machine, catalog_dir, suite_path):
    from lao.cli import main
    out = run(machine, catalog_dir, suite_path, tmp_path / "results", engine_factory=factory(), only="e2-standard-chat",
              memory_probe=lambda: (128.0, 120.0), no_judge=True)
    assert not (out / "result.json").exists()
    sheet = json.loads((out / "judge-sheet.json").read_text(encoding="utf-8"))
    (out / "answers.json").write_text(json.dumps({it["key"]: [True] * len(it["questions"]) for it in sheet}),
                                      encoding="utf-8")
    cfg = tmp_path / "machine.toml"
    from lao.p0.probe import to_toml
    cfg.write_text(to_toml(machine.raw), encoding="utf-8")
    assert main(["--config", str(cfg), "--catalog", str(catalog_dir), "p0", "pairs", "--score", str(out),
                 "--judge", "claude"]) == 0
    data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert data["judges"] == ["claude"] and data["results"][0]["score"]["parts"]["first"] == 100.0


def test_score_counts_recovered_items_and_needless_redo():
    tasks = {"a": Task("a", "image", "r", ["x", "y"]), "b": Task("b", "image", "r", ["x", "y"])}
    turn = {"invalid": 0, "errors": [], "calls": [{"tool": "generate_image", "args": {}, "ok": True, "refs": ["1"]}],
            "text": ""}
    res = {"id": "p", "can_see": True, "seconds": 10, "records": [
        {"task": "a", "repeat": 0, "images": ["1", "2"], "turns": [turn, turn]},
        {"task": "b", "repeat": 0, "images": ["3", "4"], "turns": [turn, turn]},
    ]}
    answers = {"p|a|0|r0": [True, False], "p|a|0|r1": [True, True],   # 足りない 1 項目を取り戻した
               "p|b|0|r0": [True, True], "p|b|0|r1": [True, True]}    # 合っていたのに作り直した
    s = score_pair(res, tasks, answers, {"first": 1, "args": 0, "fix": 1, "edit": 0, "video": 0})
    assert s["parts"]["fix"] == 100.0 and s["parts"]["first"] == 75.0 and s["needless_redo"] == 1
    # 直して悪くなったら 0 点（負にはしない）
    answers["p|a|0|r1"] = [False, False]
    assert score_pair(res, tasks, answers, {"first": 1})["parts"]["fix"] == 0.0


def test_rank_prefers_lighter_pair_within_tie_and_excludes_over_limit():
    results = [
        {"id": "big", "peak_gb": 90, "reloads": 0, "score": {"quality": 80, "seconds_per_task": 10}},
        {"id": "mid", "peak_gb": 60, "reloads": 0, "score": {"quality": 78, "seconds_per_task": 12}},
        {"id": "small", "peak_gb": 30, "reloads": 2, "score": {"quality": 60, "seconds_per_task": 20}},
        {"id": "broken", "peak_gb": 10, "error": "x", "score": {"quality": 0}},
    ]
    r = rank(results, 96, 0, 3)
    assert r["recommended"] == "mid"
    assert {x["id"] for x in r["rows"] if x["pareto"]} == {"big", "mid", "small"}
    # 他のために 40GB 空けておくなら、big と mid は外れる
    r = rank(results, 96, 40, 3)
    assert r["recommended"] == "small"
    assert not next(x for x in r["rows"] if x["id"] == "big")["fits"]
    text = report_text({"timestamp": "t", "tasks": [], "budget_gb": 96, "reserve_gb": 40, "ranking": r,
                        "results": results, "judge": "j"})
    assert "small ★" in text


def test_judge_refs_all_lists_each_image_model_once(catalog):
    refs = judge_refs("all", catalog)
    assert "e1.e1-standard.perceiver" in refs and "e1.e1-standard.reasoner" not in refs  # gpt-oss は画像を見られない
    # Gemma 4 E4B は E1・E4 に何度も出てくるが、判定役としては 1 回だけ
    assert sum(1 for r in refs if r.endswith((".perceiver", "e4-ltx.chat", "e4-wan.chat", "e3-standard.chat"))) == 1
    with pytest.raises(PairsError, match="画像を見られません"):
        judge_refs("e1.e1-standard.reasoner", catalog)


def test_combine_takes_median_and_skips_own_model():
    tasks = {"a": Task("a", "image", "r", ["x", "y"])}
    turn = {"invalid": 0, "errors": [], "calls": [], "text": ""}
    res = {"id": "p", "can_see": False, "seconds": 1, "models": {"chat": "own"},
           "records": [{"task": "a", "repeat": 0, "images": ["1"], "turns": [turn]}]}
    w = {"first": 1}
    answers = {"own": {"p|a|0|r0": [True, True]}, "j1": {"p|a|0|r0": [True, False]},
               "j2": {"p|a|0|r0": [False, False]}, "j3": {"p|a|0|r0": [True, True]}}
    s = combine(res, tasks, answers, w)
    assert set(s["by_judge"]) == {"j1", "j2", "j3"} and s["self_excluded"]
    assert s["quality"] == 50.0 and s["spread"] == 100.0
    # 自分しか判定役がいないときは使う
    assert combine(res, tasks, {"own": answers["own"]}, w)["quality"] == 100.0
    ag = agreement({"j1": answers["j1"], "j3": answers["j3"]})
    assert ag["all"] == 0.5 and ag["pairs"]["j1 / j3"] == 0.5
