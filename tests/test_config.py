from pathlib import Path

import pytest

from lao.config import ConfigError, check_policy, load_catalog, load_machine, missing_capabilities

ROOT = Path(__file__).resolve().parents[1]


def test_real_catalog_loads_and_meets_required_io():
    envs = load_catalog(ROOT / "catalog")
    assert set(envs) == {"e1", "e2", "e3", "e4"}
    for env in envs.values():
        for conf in env.configurations.values():
            assert missing_capabilities(env, conf) == [], (env.id, conf.id)


def test_example_machine_config_loads(tmp_path):
    m = load_machine(ROOT / "config" / "machine.example.toml")
    assert m.budget_gb == 96


def test_gpu_cap_lowers_budget(machine):
    machine.raw["memory"]["gpu_allocatable_gb"] = 80
    assert machine.budget_gb == 80


def test_network_egress_cannot_be_enabled(tmp_path):
    text = (ROOT / "config" / "machine.example.toml").read_text(encoding="utf-8")
    p = tmp_path / "m.toml"
    p.write_text(text.replace("allow_network_egress = false", "allow_network_egress = true"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_machine(p)


def test_third_party_quantized_blocked_by_default(machine, catalog):
    conf = catalog["e1"].configurations["e1-standard"]
    conf.components["reasoner"].quantization = "third_party"
    assert check_policy(machine, conf)
    machine.raw["policy"]["allow_third_party_quantized"] = True
    assert check_policy(machine, conf) == []


def test_missing_capability_detected(catalog):
    env = catalog["e4"]
    conf = env.configurations["e4-wan"]
    conf.components["videogen"].capabilities.remove("image_to_video")
    assert "output:image_to_video" in missing_capabilities(env, conf)
