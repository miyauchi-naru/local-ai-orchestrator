import pytest

from lao.media import Media, MediaError, segment_message


def test_split_video_into_frames_and_audio(tmp_path, sample_video):
    m = Media()
    segs = m.split_video(sample_video, tmp_path / "w", segment_seconds=5, frames_per_second=1)
    assert [round(s.end - s.start) for s in segs] == [5, 5, 2]
    assert all(s.audio and s.audio.exists() for s in segs)
    assert 4 <= len(segs[0].frames) <= 6
    msg = segment_message(segs[0], "何が起きた？")
    types = [p["type"] for p in msg["content"]]
    assert types[0] == "text" and "input_audio" in types and types.count("image_url") == len(segs[0].frames)


def test_section_range(tmp_path, sample_video):
    segs = Media().split_video(sample_video, tmp_path / "w", start=3, end=7, segment_seconds=10)
    assert len(segs) == 1 and segs[0].start == 3 and segs[0].end == 7


def test_corrupt_file_reported(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(MediaError, match="読めません"):
        Media().probe(bad)
