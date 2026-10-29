"""実機設定（config/machine.toml）と構成カタログ（catalog/*.toml）の読み込み。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROLES = {"chat", "perceive", "image_gen", "tts", "video_gen", "embed"}
ENGINES = {"llama", "comfyui", "tts", "fake"}
QUANTIZATION = {"none", "publisher", "third_party"}


class ConfigError(ValueError):
    pass


@dataclass
class Component:
    key: str
    role: str
    engine: str
    model: str
    quantization: str
    license: str
    size_gb: float
    weights_gb: float
    overhead_gb: float
    capabilities: list[str]
    raw: dict[str, Any]

    @property
    def resident_gb(self) -> float:
        return self.weights_gb + self.overhead_gb

    def kv_bytes_per_token(self) -> int | None:
        kv = self.raw.get("kv")
        if not kv:
            return None
        # K と V の 2 つ分
        return 2 * kv["n_layers"] * kv["n_kv_heads"] * kv["head_dim"] * kv["bytes_per_elem"]


@dataclass
class Configuration:
    env_id: str
    id: str
    label: str
    status: str
    chat_component: str
    components: dict[str, Component]

    @property
    def chat(self) -> Component:
        return self.components[self.chat_component]

    def capabilities(self) -> set[str]:
        caps: set[str] = set()
        for c in self.components.values():
            caps.update(c.capabilities)
        return caps

    def by_role(self, role: str) -> list[Component]:
        return [c for c in self.components.values() if c.role == role]


@dataclass
class Environment:
    id: str
    name: str
    required_inputs: list[str]
    required_outputs: list[str]
    configurations: dict[str, Configuration] = field(default_factory=dict)


# 必須の入出力と、それを満たす能力の対応
_INPUT_CAPS = {
    "text": {"text"},
    "image": {"image"},
    "audio": {"audio"},
    "video": {"video_frames", "video_audio"},
}
_OUTPUT_CAPS = {
    "text": {"text"},
    "image": {"image_generate"},
    "audio": {"speech_generate"},
    "video": {"video_generate"},
}


def missing_capabilities(env: Environment, conf: Configuration) -> list[str]:
    """環境の必須入出力のうち、構成が満たせないものを返す（R12）。"""
    caps = conf.capabilities()
    missing = []
    for i in env.required_inputs:
        if not _INPUT_CAPS[i] & caps:
            missing.append(f"input:{i}")
    for o in env.required_outputs:
        if not _OUTPUT_CAPS[o] & caps:
            missing.append(f"output:{o}")
    if env.id == "e4" and "image_to_video" not in caps:
        missing.append("output:image_to_video")
    return missing


def _component(key: str, d: dict[str, Any], where: str) -> Component:
    for k in ("role", "engine", "model", "quantization", "mem", "capabilities"):
        if k not in d:
            raise ConfigError(f"{where}.{key}: '{k}' がありません")
    if d["role"] not in ROLES:
        raise ConfigError(f"{where}.{key}: 不明な role {d['role']}")
    if d["engine"] not in ENGINES:
        raise ConfigError(f"{where}.{key}: 不明な engine {d['engine']}")
    if d["quantization"] not in QUANTIZATION:
        raise ConfigError(f"{where}.{key}: 不明な quantization {d['quantization']}")
    return Component(
        key=key,
        role=d["role"],
        engine=d["engine"],
        model=d["model"],
        quantization=d["quantization"],
        license=d.get("license", ""),
        size_gb=float(d.get("size_gb", 0)),
        weights_gb=float(d["mem"]["weights_gb"]),
        overhead_gb=float(d["mem"].get("overhead_gb", 0)),
        capabilities=list(d["capabilities"]),
        raw=d,
    )


def load_embedding(directory: Path) -> Configuration | None:
    """意味検索の埋め込みモデル（catalog/shared/embedding.toml）。環境とは別に一つだけ持つ。"""
    path = directory / "shared" / "embedding.toml"
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    c = data["configuration"]
    comp = _component("embed", data["component"], f"{path.name}")
    return Configuration(env_id="shared", id=c["id"], label=c.get("label", c["id"]),
                         status=c.get("status", "candidate"), chat_component="embed", components={"embed": comp})


def load_catalog(directory: Path) -> dict[str, Environment]:
    envs: dict[str, Environment] = {}
    for path in sorted(directory.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        e = data["environment"]
        env = Environment(e["id"], e["name"], e["required_inputs"], e["required_outputs"])
        for c in data.get("configurations", []):
            where = f"{path.name}:{c['id']}"
            comps = {k: _component(k, v, where) for k, v in c["components"].items()}
            if c["chat_component"] not in comps:
                raise ConfigError(f"{where}: chat_component {c['chat_component']} がありません")
            env.configurations[c["id"]] = Configuration(
                env_id=env.id,
                id=c["id"],
                label=c.get("label", c["id"]),
                status=c.get("status", "candidate"),
                chat_component=c["chat_component"],
                components=comps,
            )
        envs[env.id] = env
    return envs


@dataclass
class MachineConfig:
    raw: dict[str, Any]
    path: Path | None = None

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def budget_gb(self) -> float:
        b = float(self.raw["memory"]["budget_gb"])
        emb = self.raw.get("embedding", {})
        if emb.get("enabled"):
            b -= float(emb.get("mem_gb", 2))  # 意味検索のモデルは常駐させるので、その分を先に引く
        cap = float(self.raw["memory"].get("gpu_allocatable_gb") or 0)
        # 実測した GPU 確保上限が予算より小さければ、そちらに合わせる
        return min(b, cap) if cap > 0 else b

    @property
    def safety_factor(self) -> float:
        return float(self.raw["memory"].get("safety_factor", 1.15))

    def measured(self, env_id: str, component_key: str) -> dict[str, Any] | None:
        return self.raw.get("measured", {}).get(f"{env_id}.{component_key}")

    def path_of(self, name: str) -> Path:
        return Path(self.raw["paths"][name])


def load_machine(path: Path) -> MachineConfig:
    if not path.exists():
        raise ConfigError(
            f"{path} がありません。config/machine.example.toml をコピーして作成してください"
        )
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    for section in ("machine", "paths", "memory", "policy", "server"):
        if section not in raw:
            raise ConfigError(f"{path}: [{section}] がありません")
    if raw["policy"].get("allow_network_egress"):
        raise ConfigError("allow_network_egress は true にできません（外部送信は禁止）")
    return MachineConfig(raw, path)


def check_policy(machine: MachineConfig, conf: Configuration) -> list[str]:
    """量子化ルール（R14）に反する部品を返す。"""
    problems = []
    pol = machine["policy"]
    for c in conf.components.values():
        if c.quantization == "third_party" and not pol.get("allow_third_party_quantized"):
            problems.append(f"{c.key}: 第三者の量子化版は未許可（policy.allow_third_party_quantized）")
        if c.quantization == "publisher" and not pol.get("allow_publisher_quantized", True):
            problems.append(f"{c.key}: 配布元の量子化形式は未許可（policy.allow_publisher_quantized）")
    return problems
