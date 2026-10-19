"""The conversion itself: images via Pillow, video and audio via ffmpeg, documents via Office."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Callable

import imageio_ffmpeg
from PIL import Image, ImageChops, ImageOps

from . import formats, office

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # HEIC stays unavailable, everything else works
    pillow_heif = None

ProgressCallback = Callable[[float], None]
CANCELLED = "Cancelled"


class ConversionError(Exception):
    pass


def free_path(dst: Path, is_dir: bool = False) -> Path:
    """If the name is taken, append " (1)", " (2)" and so on."""
    # A folder has no extension: "report v1.2 (pages)" must not become "report v1 (1).2 (pages)"
    stem, suffix = (dst.name, "") if is_dir else (dst.stem, dst.suffix)
    base, n = dst, 1
    while dst.exists():
        dst = base.with_name(f"{stem} ({n}){suffix}")
        n += 1
    return dst


def output_path(src: Path, fmt: str) -> Path:
    """A file next to the source, with the new extension."""
    return free_path(src.with_suffix(f".{fmt}"))


def _write_via_tmp(dst: Path, write: Callable[[Path], None]) -> None:
    """Write to a temp file so a failure doesn't leave a broken result behind."""
    tmp = dst.with_name(f"{dst.stem}.converting{dst.suffix}")
    try:
        write(tmp)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def _write_dir_via_tmp(dst: Path, write: Callable[[Path], None]) -> None:
    """The same for a folder: it appears only when every file in it is ready."""
    tmp = dst.with_name(f"{dst.name}.converting")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        write(tmp)
        tmp.rename(dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def convert(
    src: Path,
    fmt: str,
    on_progress: ProgressCallback = lambda p: None,
    cancel: threading.Event | None = None,
) -> Path:
    category = formats.detect(src)
    if category is None:
        raise ConversionError("Unsupported format")
    if fmt not in formats.TARGETS[category]:
        raise ConversionError(f"Can't convert to {fmt.upper()}")

    if fmt == formats.PDF_PAGES:
        # A folder next to the original: "deck (slides)/deck - 01.pdf", …
        dst = free_path(src.with_name(f"{src.stem} ({office.split_unit([src])})"), is_dir=True)
        _write_dir_via_tmp(dst, lambda tmp: _convert_document(src, tmp, fmt, cancel or threading.Event()))
        on_progress(1.0)
        return dst

    dst = output_path(src, fmt)
    if category == formats.IMAGE:
        _write_via_tmp(dst, lambda tmp: _convert_image(src, tmp, fmt))
    elif category == formats.DOCUMENT:
        _write_via_tmp(dst, lambda tmp: _convert_document(src, tmp, fmt, cancel or threading.Event()))
    else:
        _write_via_tmp(dst, lambda tmp: _convert_media(src, tmp, fmt, on_progress, cancel or threading.Event()))
    on_progress(1.0)
    return dst


# ---------- Images ----------

_PIL_FORMATS = {
    "png": "PNG", "jpg": "JPEG", "webp": "WEBP", "heic": "HEIF", "avif": "AVIF",
    "gif": "GIF", "bmp": "BMP", "tiff": "TIFF", "ico": "ICO", "pdf": "PDF",
}
_NO_ALPHA = {"jpg", "bmp", "pdf"}
_SAVE_OPTIONS = {
    "jpg": {"quality": 92, "optimize": True},
    "webp": {"quality": 90, "method": 4},
    "heic": {"quality": 90},
    "avif": {"quality": 80},
    "png": {"optimize": True},
    "tiff": {"compression": "tiff_lzw"},
}


def _has_alpha(im: Image.Image) -> bool:
    return im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)


def _flatten(im: Image.Image) -> Image.Image:
    """Remove transparency by putting a white background underneath."""
    if not _has_alpha(im):
        return im.convert("RGB")
    rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


def _square_icon(im: Image.Image) -> tuple[Image.Image, list[tuple[int, int]]]:
    """ICO must be square: fit the image into a transparent square of up to 256px."""
    side = min(256, max(im.size))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    thumb = im.convert("RGBA")
    thumb.thumbnail((side, side), Image.Resampling.LANCZOS)
    canvas.paste(thumb, ((side - thumb.width) // 2, (side - thumb.height) // 2))
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= side] or [(side, side)]
    return canvas, sizes


def _convert_image(src: Path, dst: Path, fmt: str) -> None:
    if fmt == "heic" and pillow_heif is None:
        raise ConversionError("HEIC unavailable: pillow-heif is not installed")
    try:
        with Image.open(src) as im:
            options = dict(_SAVE_OPTIONS.get(fmt, {}))

            if getattr(im, "is_animated", False) and fmt in ("gif", "webp"):
                im.save(dst, _PIL_FORMATS[fmt], save_all=True, loop=0, **options)
                return

            _save_image(ImageOps.exif_transpose(im), dst, fmt, options)
    except ConversionError:
        raise
    except Exception as e:  # Pillow raises all kinds of exceptions
        raise ConversionError(f"Couldn't process the image: {e}") from e


def _save_image(out: Image.Image, dst: Path, fmt: str, options: dict) -> None:
    if fmt in _NO_ALPHA:
        out = _flatten(out)
    elif out.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        out = out.convert("RGBA" if _has_alpha(out) else "RGB")
    if fmt == "ico":
        out, options["sizes"] = _square_icon(out)
    out.save(dst, _PIL_FORMATS[fmt], **options)


# ---------- Documents ----------

def _convert_document(src: Path, dst: Path, fmt: str, cancel: threading.Event) -> None:
    try:
        if fmt == formats.PDF_PAGES:
            office.to_pdf_pages(src, dst, cancel)
        else:
            office.to_pdf(src, dst, cancel)
    except office.Cancelled:
        raise ConversionError(CANCELLED) from None
    except office.OfficeError as e:
        raise ConversionError(str(e)) from None


# ---------- Image tools ----------

# Appended to the copy's name: "photo (compressed).jpg"
TOOL_SUFFIXES = {
    "compress": "compressed", "strip": "no metadata", "trim": "trimmed", "half": "50%",
    "rotate": "rotated", "gray": "bw", "white_bg": "white bg",
}
_COMPRESS_OPTIONS = {
    "jpg": {"quality": 75, "optimize": True, "progressive": True},
    "webp": {"quality": 75, "method": 6},
    "heic": {"quality": 60},
    "avif": {"quality": 60},
    "png": {"optimize": True},
    "tiff": {"compression": "tiff_adobe_deflate"},
}
_EXT_ALIASES = {"jpeg": "jpg", "jpe": "jpg", "tif": "tiff", "heif": "heic"}
_EXIF_FORMATS = {"jpg", "png", "webp", "heic", "avif", "tiff"}
_ICC_MODES = {"RGB", "RGBA", "L", "LA"}


def apply_tool(src: Path, tool: str) -> Path:
    """A modified copy of the image next to the original, in the same format."""
    if formats.detect(src) != formats.IMAGE:
        raise ConversionError("Tools work only with images")
    if tool not in TOOL_SUFFIXES:
        raise ConversionError("Unknown tool")
    ext = src.suffix.lower().lstrip(".")
    fmt = _EXT_ALIASES.get(ext, ext)
    if fmt not in _PIL_FORMATS:  # Pillow can only read PSD, TGA and PPM
        fmt = "png"
    if fmt == "heic" and pillow_heif is None:
        raise ConversionError("HEIC unavailable: pillow-heif is not installed")

    dst = free_path(src.with_name(f"{src.stem} ({TOOL_SUFFIXES[tool]}).{fmt}"))

    def write(tmp: Path) -> None:
        _apply_image_tool(src, tmp, fmt, tool)
        if tool == "compress" and tmp.stat().st_size >= src.stat().st_size:
            raise ConversionError("Couldn't compress further: the file is already optimized")

    _write_via_tmp(dst, write)
    return dst


def _apply_image_tool(src: Path, dst: Path, fmt: str, tool: str) -> None:
    try:
        with Image.open(src) as im:
            if getattr(im, "n_frames", 1) > 1:
                raise ConversionError("Tools don't support animated images yet")
            icc = im.info.get("icc_profile") if im.mode in _ICC_MODES else None
            upright = im.getexif().get(0x0112, 1) == 1

            if tool == "strip" and fmt == "jpg" and im.format == "JPEG" and upright:
                # Re-encoding would degrade the image: save with the original quantization tables
                im.save(dst, "JPEG", quality="keep", subsampling="keep", icc_profile=icc)
                return

            out = ImageOps.exif_transpose(im)
            exif = out.getexif()  # rotation is already applied, no orientation in it
            options = dict(_SAVE_OPTIONS.get(fmt, {}))

            if tool == "compress":
                if fmt not in _COMPRESS_OPTIONS:
                    raise ConversionError(f"{fmt.upper()} can't be compressed: convert it to PNG or JPG")
                options = dict(_COMPRESS_OPTIONS[fmt])
                if fmt == "png":
                    out = _quantize(out)
            elif tool == "trim":
                out = _trim(out)
            elif tool == "half":
                out = out.resize((max(1, out.width // 2), max(1, out.height // 2)), Image.Resampling.LANCZOS)
            elif tool == "rotate":
                out = out.transpose(Image.Transpose.ROTATE_270)
            elif tool == "gray":
                out = out.convert("LA" if _has_alpha(out) else "L")
                icc = None  # a color profile doesn't fit a grayscale image
            elif tool == "white_bg":
                if not _has_alpha(out):
                    raise ConversionError("The image has no transparency")
                out = _flatten(out)

            if icc and out.mode in _ICC_MODES:
                options["icc_profile"] = icc
            if tool != "strip" and exif and fmt in _EXIF_FORMATS:
                options["exif"] = exif.tobytes()
            if fmt in ("heic", "avif") and out.mode in ("L", "LA"):
                out = out.convert("RGBA" if out.mode == "LA" else "RGB")
            _save_image(out, dst, fmt, options)
    except ConversionError:
        raise
    except Exception as e:
        raise ConversionError(f"Couldn't process the image: {e}") from e


def _quantize(im: Image.Image) -> Image.Image:
    """Compress PNG like TinyPNG does: a 256-color palette with dithering."""
    if _has_alpha(im):
        return im.convert("RGBA").quantize(256, method=Image.Quantize.FASTOCTREE)
    return im.convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT)


def _trim(im: Image.Image) -> Image.Image:
    """Trims transparent borders or, for opaque images, borders the color of the top-left pixel."""
    full = (0, 0, *im.size)
    box = None
    if _has_alpha(im):
        box = im.convert("RGBA").getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if box is None or box == full:
        rgb = im.convert("RGB")
        bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
        box = ImageChops.difference(rgb, bg).convert("L").point(lambda v: 255 if v > 16 else 0).getbbox()
    if box is None:
        raise ConversionError("The image is a solid color, nothing to trim")
    if box == full:
        raise ConversionError("No borders found")
    return im.crop(box)


# ---------- Video and audio ----------

_EVEN_SIZE = ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
_H264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p", *_EVEN_SIZE]

FFMPEG_ARGS = {
    "mp4": [*_H264, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-sn"],
    "mov": [*_H264, "-c:a", "aac", "-b:a", "192k", "-sn"],
    "mkv": [*_H264, "-c:a", "aac", "-b:a", "192k", "-sn"],
    "webm": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1",
             "-c:a", "libopus", "-b:a", "128k", "-sn"],
    "avi": ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame", "-q:a", "2", "-sn"],
    "gif": ["-an", "-sn", "-vf",
            "fps=12,scale='min(480,iw)':-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse",
            "-loop", "0"],
    "mp3": ["-vn", "-sn", "-c:a", "libmp3lame", "-q:a", "2"],
    "wav": ["-vn", "-sn", "-c:a", "pcm_s16le"],
    "flac": ["-vn", "-sn", "-c:a", "flac"],
    "ogg": ["-vn", "-sn", "-c:a", "libvorbis", "-q:a", "5"],
    "m4a": ["-vn", "-sn", "-c:a", "aac", "-b:a", "192k"],
    "opus": ["-vn", "-sn", "-c:a", "libopus", "-b:a", "128k"],
}

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_PROGRESS_LINE_RE = re.compile(r"^[a-z_0-9]+=")


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _convert_media(
    src: Path, dst: Path, fmt: str, on_progress: ProgressCallback, cancel: threading.Event
) -> None:
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-i", str(src),
        *FFMPEG_ARGS[fmt], "-progress", "pipe:1", "-nostats", str(dst),
    ]
    # Without this flag a black console window flashes on Windows
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", creationflags=flags,
    )

    def watch_cancel() -> None:
        while proc.poll() is None:
            if cancel.wait(0.2):
                proc.kill()
                return

    threading.Thread(target=watch_cancel, daemon=True).start()

    duration = None
    log_tail: deque[str] = deque(maxlen=8)
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if duration is None and (m := _DURATION_RE.search(line)):
            h, mnt, s = m.groups()
            duration = int(h) * 3600 + int(mnt) * 60 + float(s)
        if line.startswith("out_time_us=") and duration:
            try:
                on_progress(min(int(line.split("=", 1)[1]) / 1e6 / duration, 0.99))
            except ValueError:  # can be "N/A"
                pass
        elif line and not _PROGRESS_LINE_RE.match(line):
            log_tail.append(line)
    proc.wait()

    if cancel.is_set():
        raise ConversionError(CANCELLED)
    if proc.returncode != 0:
        raise ConversionError(log_tail[-1] if log_tail else f"ffmpeg exited with code {proc.returncode}")
