import pytest

from lao.library import Library
from lao.tools import ToolError, tools_for, validate


def test_tools_per_environment(catalog):
    names = lambda env, conf: {t.name for t in tools_for(catalog[env].configurations[conf])}
    assert names("e1", "e1-standard") == {"describe_media"}
    assert names("e2", "e2-standard") == {"describe_media", "generate_image", "edit_image"}
    assert names("e3", "e3-standard") == {"describe_media", "generate_speech"}
    assert names("e4", "e4-ltx") == {"describe_media", "generate_video"}


def test_validation_rejects_bad_calls(tmp_path, catalog, sample_image):
    lib = Library(tmp_path / "lib")
    img = lib.add_file(sample_image, env_id="e2", configuration="c")
    e2 = catalog["e2"].configurations["e2-standard"]
    e1 = catalog["e1"].configurations["e1-standard"]
    with pytest.raises(ToolError, match="未登録"):
        validate("run_shell", "{}", e2, lib)
    with pytest.raises(ToolError, match="この環境では"):
        validate("generate_image", '{"prompt":"x"}', e1, lib)
    with pytest.raises(ToolError, match="id@版"):
        validate("edit_image", {"ref": "../../etc/passwd", "instruction": "x"}, e2, lib)
    with pytest.raises(ToolError, match="見つからない"):
        validate("edit_image", {"ref": "0123456789ab@1", "instruction": "x"}, e2, lib)
    with pytest.raises(ToolError, match="不明な引数"):
        validate("generate_image", {"prompt": "x", "path": "/tmp"}, e2, lib)
    with pytest.raises(ToolError, match="以下"):
        validate("generate_image", {"prompt": "x", "width": 99999}, e2, lib)
    with pytest.raises(ToolError, match="JSON"):
        validate("generate_image", "{not json", e2, lib)
    ok = validate("edit_image", {"ref": img.ref, "instruction": "明るく"}, e2, lib)
    assert ok["ref"] == img.ref


def test_ref_kind_checked(tmp_path, catalog, sample_audio):
    lib = Library(tmp_path / "lib")
    au = lib.add_file(sample_audio, env_id="e3", configuration="c")
    e2 = catalog["e2"].configurations["e2-standard"]
    with pytest.raises(ToolError, match="image"):
        validate("edit_image", {"ref": au.ref, "instruction": "x"}, e2, lib)
