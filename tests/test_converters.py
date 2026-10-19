import subprocess

import pytest
from PIL import Image

from dropconvert import formats
from dropconvert.converters import ConversionError, convert, ffmpeg_exe, output_path


@pytest.fixture
def rgba_png(tmp_path):
    path = tmp_path / "picture.png"
    Image.new("RGBA", (64, 48), (255, 0, 0, 128)).save(path)
    return path


@pytest.fixture
def sample_video(tmp_path):
    path = tmp_path / "clip.mov"
    subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=161x121:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


@pytest.mark.parametrize("fmt", formats.TARGETS[formats.IMAGE])
def test_image_targets(rgba_png, fmt):
    out = convert(rgba_png, fmt)
    assert out.suffix == f".{fmt}" and out.stat().st_size > 0
    if fmt != "pdf":
        with Image.open(out) as im:
            assert im.size == ((64, 64) if fmt == "ico" else (64, 48))


@pytest.mark.parametrize("fmt", formats.TARGETS[formats.VIDEO])
def test_video_targets(sample_video, fmt):
    progress = []
    out = convert(sample_video, fmt, on_progress=progress.append)
    assert out.exists() and out.stat().st_size > 0
    assert progress[-1] == 1.0
    assert not list(out.parent.glob("*.converting.*"))


@pytest.mark.parametrize("fmt", formats.TARGETS[formats.AUDIO])
def test_audio_targets(tmp_path, sample_video, fmt):
    audio = convert(sample_video, "wav")
    out = convert(audio, fmt)
    assert out.exists() and out.stat().st_size > 0


def test_output_path_does_not_overwrite(rgba_png):
    assert output_path(rgba_png, "png").name == "picture (1).png"


def test_unsupported_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hi")
    with pytest.raises(ConversionError):
        convert(path, "png")


def test_broken_video_reports_error(tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"not a video")
    with pytest.raises(ConversionError):
        convert(path, "webm")
    assert not (tmp_path / "broken.webm").exists()
