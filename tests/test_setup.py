import tomllib
from pathlib import Path

from lao.cli import main
from lao.setup import auto_values, proposed_values, set_value

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "config" / "machine.example.toml").read_text(encoding="utf-8")


def test_set_value_keeps_comments_and_other_tables():
    text = set_value(EXAMPLE, "engines.ffmpeg", "binary", "C:/ffmpeg/bin/ffmpeg.exe")
    data = tomllib.loads(text)
    assert data["engines"]["ffmpeg"]["binary"] == "C:/ffmpeg/bin/ffmpeg.exe"
    assert data["engines"]["llama"]["binary"] == "C:/lao/engines/llama.cpp/llama-server.exe"
    assert "# 実機ごとの設定" in text
    # 行末のコメントは残す
    text = set_value(EXAMPLE, "machine", "runtime", "wsl2")
    assert 'runtime = "wsl2"' in text and tomllib.loads(text)["machine"]["runtime"] == "wsl2"


def test_set_value_adds_keys_and_tables():
    text = set_value(EXAMPLE, "measured", "e1.reasoner", {"peak_gb": 68.2, "load_seconds": 41.0})
    text = set_value(text, "newtable", "x", 1)
    data = tomllib.loads(text)
    assert data["measured"]["e1.reasoner"] == {"peak_gb": 68.2, "load_seconds": 41.0}
    assert data["sandbox"]["enabled"] is False  # 次の表に入り込まない
    assert data["newtable"]["x"] == 1


def test_set_value_refuses_multiline_values():
    import pytest
    with pytest.raises(ValueError):
        set_value(EXAMPLE, "engines.comfyui", "command", ["x"])


def test_auto_values_finds_tools_and_fixes_key():
    raw = tomllib.loads(EXAMPLE)
    found = {"ffmpeg": "C:\\ff\\bin\\ffmpeg.exe", "ffprobe": "C:\\ff\\bin\\ffprobe.exe"}
    exists = lambda p: p == "C:/lao/engines/llama.cpp/llama-server.exe"  # noqa: E731
    vals = {(s, k): v for s, k, v, _ in auto_values(raw, which=found.get, exists=exists)}
    assert vals[("engines.ffmpeg", "binary")].endswith("ffmpeg.exe")
    assert ("engines.llama", "binary") not in vals  # 既にある
    assert len(vals[("server", "api_key")]) >= 24
    raw["server"]["api_key"] = "fixed"
    assert ("server", "api_key") not in {(s, k) for s, k, *_ in auto_values(raw, which=found.get, exists=exists)}


def test_proposed_values():
    vals = proposed_values({"memory": {"gpu_allocatable_gb": 100}, "gpu": {"watchdog_seconds": 1200},
                            "measured": {"e1.reasoner": {"peak_gb": 70.0}}})
    assert [(s, k) for s, k, *_ in vals] == [("memory", "gpu_allocatable_gb"), ("gpu", "watchdog_seconds"),
                                             ("measured", "e1.reasoner")]


def test_configure_command(tmp_path, capsys):
    cfg = tmp_path / "machine.toml"
    cfg.write_text(EXAMPLE, encoding="utf-8")
    (tmp_path / "machine.proposed.toml").write_text(
        '[memory]\ngpu_allocatable_gb = 101\n[measured]\n"e1.reasoner" = { peak_gb = 70.0 }\n', encoding="utf-8")
    assert main(["--config", str(cfg), "configure", "--set", "memory.budget_gb=90", "--apply-proposed"]) == 0
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["memory"]["budget_gb"] == 90 and data["memory"]["gpu_allocatable_gb"] == 101
    assert data["measured"]["e1.reasoner"]["peak_gb"] == 70.0
    before = cfg.read_text(encoding="utf-8")
    assert main(["--config", str(cfg), "configure", "--set", "server.api_key=abc", "--dry-run"]) == 0
    assert cfg.read_text(encoding="utf-8") == before
    assert "abc" not in capsys.readouterr().out
