"""Сама конвертация: изображения через Pillow, видео и аудио через ffmpeg."""
from __future__ import annotations

import re
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Callable

import imageio_ffmpeg
from PIL import Image, ImageOps

from . import formats

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # HEIC останется недоступен, остальное работает
    pillow_heif = None

ProgressCallback = Callable[[float], None]


class ConversionError(Exception):
    pass


def output_path(src: Path, fmt: str) -> Path:
    """Файл рядом с исходным; если имя занято, добавляем « (1)», « (2)» и т.д."""
    dst = src.with_suffix(f".{fmt}")
    n = 1
    while dst.exists():
        dst = src.with_name(f"{src.stem} ({n}).{fmt}")
        n += 1
    return dst


def convert(
    src: Path,
    fmt: str,
    on_progress: ProgressCallback = lambda p: None,
    cancel: threading.Event | None = None,
) -> Path:
    category = formats.detect(src)
    if category is None:
        raise ConversionError("Неподдерживаемый формат")
    if fmt not in formats.TARGETS[category]:
        raise ConversionError(f"Нельзя конвертировать в {fmt.upper()}")

    dst = output_path(src, fmt)
    # Пишем во временный файл, чтобы при ошибке не оставлять битый результат
    tmp = dst.with_name(f"{dst.stem}.converting.{fmt}")
    try:
        if category == formats.IMAGE:
            _convert_image(src, tmp, fmt)
        else:
            _convert_media(src, tmp, fmt, on_progress, cancel or threading.Event())
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    on_progress(1.0)
    return dst


# ---------- Изображения ----------

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
    """Убираем прозрачность, подкладывая белый фон."""
    if not _has_alpha(im):
        return im.convert("RGB")
    rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


def _square_icon(im: Image.Image) -> tuple[Image.Image, list[tuple[int, int]]]:
    """ICO должен быть квадратным: вписываем картинку в прозрачный квадрат до 256px."""
    side = min(256, max(im.size))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    thumb = im.convert("RGBA")
    thumb.thumbnail((side, side), Image.Resampling.LANCZOS)
    canvas.paste(thumb, ((side - thumb.width) // 2, (side - thumb.height) // 2))
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= side] or [(side, side)]
    return canvas, sizes


def _convert_image(src: Path, dst: Path, fmt: str) -> None:
    if fmt == "heic" and pillow_heif is None:
        raise ConversionError("HEIC недоступен: не установлен pillow-heif")
    try:
        with Image.open(src) as im:
            pil_format = _PIL_FORMATS[fmt]
            options = dict(_SAVE_OPTIONS.get(fmt, {}))

            if getattr(im, "is_animated", False) and fmt in ("gif", "webp"):
                im.save(dst, pil_format, save_all=True, loop=0, **options)
                return

            out = ImageOps.exif_transpose(im)
            if fmt in _NO_ALPHA:
                out = _flatten(out)
            elif out.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                out = out.convert("RGBA" if _has_alpha(out) else "RGB")
            if fmt == "ico":
                out, options["sizes"] = _square_icon(out)
            out.save(dst, pil_format, **options)
    except ConversionError:
        raise
    except Exception as e:  # Pillow бросает самые разные исключения
        raise ConversionError(f"Не удалось обработать изображение: {e}") from e


# ---------- Видео и аудио ----------

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
    # Без этого флага на Windows на мгновение появляется чёрное окно консоли
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
            except ValueError:  # бывает "N/A"
                pass
        elif line and not _PROGRESS_LINE_RE.match(line):
            log_tail.append(line)
    proc.wait()

    if cancel.is_set():
        raise ConversionError("Отменено")
    if proc.returncode != 0:
        raise ConversionError(log_tail[-1] if log_tail else f"ffmpeg завершился с кодом {proc.returncode}")
