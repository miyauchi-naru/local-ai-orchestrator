import hashlib
import json
import tomllib

import httpx

from lao.installer import build_plan, download
from lao.p0.probe import propose, to_toml

DATA = b"x" * 1000
SHA = hashlib.sha256(DATA).hexdigest()


def hf_transport(fail_first=False):
    state = {"n": 0}

    def handler(req: httpx.Request):
        if "/api/models/" in req.url.path:
            return httpx.Response(200, json=[
                {"type": "file", "path": "model-Q.gguf", "size": 10, "lfs": {"oid": SHA, "size": len(DATA)}},
                {"type": "file", "path": "mmproj-model.gguf", "size": 10, "lfs": {"oid": SHA, "size": len(DATA)}},
                {"type": "file", "path": "README.md", "size": 5},
            ])
        if "/resolve/" in req.url.path:
            rng = req.headers.get("range")
            start = int(rng.split("=")[1].rstrip("-")) if rng else 0
            state["n"] += 1
            body = DATA[start:]
            if fail_first and state["n"] == 1:
                body = body[:400]
            return httpx.Response(206 if rng else 200, content=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_plan_and_resumable_download(machine, catalog, tmp_path):
    conf = catalog["e1"].configurations["e1-standard"]
    client = httpx.Client(transport=hf_transport(fail_first=True))
    plan = build_plan(machine, conf, client)
    assert {t.path for t in plan.tasks} == {"model-Q.gguf", "mmproj-model.gguf"}  # 2 部品 × 2 ファイル
    assert len(plan.tasks) == 4 and not plan.blocked
    t = plan.tasks[0]
    try:
        download(t, client)
    except IOError:
        pass  # 途中で切れた
    download(t, client)  # 続きから
    assert t.dest.read_bytes() == DATA


def test_plan_reports_missing_sources(machine, catalog):
    conf = catalog["e3"].configurations["e3-standard"]
    plan = build_plan(machine, conf, httpx.Client(transport=hf_transport()))
    assert any("tts" in x for x in plan.todo)


def test_propose_from_results(machine, tmp_path):
    res = tmp_path / "res"
    res.mkdir()
    (res / "p0-20261201-000000.json").write_text(json.dumps({
        "gpu": {"cuda": True, "allocatable_gb": 100},
        "models": {"e1.reasoner": {"peak_gb": 66.5, "load_seconds": 40, "tokens_per_second": 45,
                                   "first_answer_seconds": 120}},
    }), encoding="utf-8")
    out = tmp_path / "proposed.toml"
    r = propose(machine, res, out)
    data = tomllib.loads(out.read_text(encoding="utf-8"))
    assert data["memory"]["gpu_allocatable_gb"] == 95
    assert data["measured"]["e1.reasoner"]["peak_gb"] == 66.5
    assert data["gpu"]["watchdog_seconds"] == 1200
    assert r["notes"]


def test_toml_roundtrip(machine):
    assert tomllib.loads(to_toml(machine.raw)) == machine.raw
