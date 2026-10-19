import subprocess

import pytest
from PIL import Image

from dropconvert import formats
from dropconvert.converters import ConversionError, apply_tool, convert, ffmpeg_exe, output_path


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


# ---------- Инструменты ----------

@pytest.fixture
def photo(tmp_path):
    """JPEG с EXIF (камера, GPS, поворот на 90°) и белыми полями вокруг шума."""
    path = tmp_path / "photo.jpg"
    im = Image.new("RGB", (200, 100), "white")
    im.paste(Image.effect_noise((160, 60), 80).convert("RGB"), (20, 20))
    exif = Image.Exif()
    exif[0x010F] = "Camera"
    exif[0x0112] = 6  # повернуть на 90° по часовой
    exif.get_ifd(0x8825)[2] = (55.0, 45.0, 0.0)  # широта
    im.save(path, quality=95, exif=exif)
    return path


@pytest.mark.parametrize("tool", formats.TOOLS)
@pytest.mark.parametrize("fmt", ["png", "jpg", "webp", "heic", "tiff"])
def test_tools_keep_format(tmp_path, tool, fmt):
    src = tmp_path / f"pic.{fmt}"
    im = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    im.paste(Image.effect_noise((80, 40), 60).convert("RGBA"), (20, 20))
    if fmt == "jpg":
        im = im.convert("RGB")
    im.save(src, **({"quality": 98} if fmt in ("jpg", "webp", "heic") else {}))
    if tool == "white_bg" and fmt == "jpg":
        with pytest.raises(ConversionError):
            apply_tool(src, tool)
        return
    try:
        out = apply_tool(src, tool)
    except ConversionError as e:  # сжатие может честно отказаться, если выигрыша нет
        assert tool == "compress" and "оптимизирован" in str(e)
        return
    assert out.suffix == f".{fmt}" and out.parent == tmp_path
    assert not list(tmp_path.glob("*.converting*"))
    with Image.open(out) as res:
        w, h = res.size
    expected = {"half": (60, 40), "rotate": (80, 120)}.get(tool)
    if expected:
        assert (w, h) == expected
    if tool == "trim" and fmt != "jpg":
        assert (w, h) == (80, 40)


def test_strip_removes_metadata_and_keeps_orientation(photo):
    out = apply_tool(photo, "strip")
    assert out.name == "photo (без метаданных).jpg"
    with Image.open(out) as im:
        assert not im.getexif()
        assert im.size == (100, 200)  # поворот из EXIF применён к пикселям


def test_strip_upright_jpeg_is_lossless(tmp_path):
    src = tmp_path / "upright.jpg"
    exif = Image.Exif()
    exif[0x010F] = "Camera"
    Image.effect_noise((64, 64), 50).convert("RGB").save(src, quality=90, exif=exif)
    out = apply_tool(src, "strip")
    with Image.open(src) as a, Image.open(out) as b:
        assert a.quantization == b.quantization
        assert not b.getexif()


def test_other_tools_keep_exif(photo):
    out = apply_tool(photo, "half")
    with Image.open(out) as im:
        exif = im.getexif()
        assert exif[0x010F] == "Camera" and exif.get(0x0112, 1) == 1
        assert im.size == (50, 100)


def test_compress_makes_jpeg_smaller(photo):
    out = apply_tool(photo, "compress")
    assert out.stat().st_size < photo.stat().st_size


def test_trim_cuts_white_margins(photo):
    out = apply_tool(photo, "trim")
    with Image.open(out) as im:
        assert abs(im.width - 60) <= 2 and abs(im.height - 160) <= 2  # после поворота


def test_tool_output_does_not_overwrite(photo):
    first = apply_tool(photo, "rotate")
    second = apply_tool(photo, "rotate")
    assert first != second and second.name == "photo (повёрнуто) (1).jpg"


def test_tools_reject_video(sample_video):
    with pytest.raises(ConversionError):
        apply_tool(sample_video, "compress")
