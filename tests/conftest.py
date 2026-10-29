import subprocess
from pathlib import Path

import pytest

from lao.config import MachineConfig, load_catalog
from lao.demo import demo_machine, make_demo_catalog

ROOT = Path(__file__).resolve().parents[1]


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def catalog_dir(tmp_path):
    return make_demo_catalog(ROOT / "catalog", tmp_path / "catalog")


@pytest.fixture
def catalog(catalog_dir):
    return load_catalog(catalog_dir)


@pytest.fixture
def machine(tmp_path):
    return MachineConfig(demo_machine(tmp_path / "lao"))


@pytest.fixture
def sample_video(tmp_path):
    p = tmp_path / "sample.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=24", "-f", "lavfi", "-i", "sine=frequency=300",
           "-t", "12", "-pix_fmt", "yuv420p", "-shortest", str(p))
    return p


@pytest.fixture
def sample_image(tmp_path):
    p = tmp_path / "sample.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=256x256:rate=1", "-frames:v", "1", str(p))
    return p


@pytest.fixture
def sample_audio(tmp_path):
    p = tmp_path / "voice.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=200:duration=3", str(p))
    return p
