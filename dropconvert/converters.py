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
from PIL import Image, ImageChops, ImageOps

from . import formats

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # HEIC останется недоступен, остальное работает
    pillow_heif = None

ProgressCallback = Callable[[float], None]


class ConversionError(Exception):
    pass


def free_path(dst: Path) -> Path:
    """Если имя занято, добавляем « (1)», « (2)» и т.д."""
    base, n = dst, 1
    while dst.exists():
        dst = base.with_name(f"{base.stem} ({n}){base.suffix}")
        n += 1
    return dst


def output_path(src: Path, fmt: str) -> Path:
    """Файл рядом с исходным, с новым расширением."""
    return free_path(src.with_suffix(f".{fmt}"))


def _write_via_tmp(dst: Path, write: Callable[[Path], None]) -> None:
    """Пишем во временный файл, чтобы при ошибке не оставлять битый результат."""
    tmp = dst.with_name(f"{dst.stem}.converting{dst.suffix}")
    try:
        write(tmp)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


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
    if category == formats.IMAGE:
        _write_via_tmp(dst, lambda tmp: _convert_image(src, tmp, fmt))
    else:
        _write_via_tmp(dst, lambda tmp: _convert_media(src, tmp, fmt, on_progress, cancel or threading.Event()))
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
            options = dict(_SAVE_OPTIONS.get(fmt, {}))

            if getattr(im, "is_animated", False) and fmt in ("gif", "webp"):
                im.save(dst, _PIL_FORMATS[fmt], save_all=True, loop=0, **options)
                return

            _save_image(ImageOps.exif_transpose(im), dst, fmt, options)
    except ConversionError:
        raise
    except Exception as e:  # Pillow бросает самые разные исключения
        raise ConversionError(f"Не удалось обработать изображение: {e}") from e


def _save_image(out: Image.Image, dst: Path, fmt: str, options: dict) -> None:
    if fmt in _NO_ALPHA:
        out = _flatten(out)
    elif out.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        out = out.convert("RGBA" if _has_alpha(out) else "RGB")
    if fmt == "ico":
        out, options["sizes"] = _square_icon(out)
    out.save(dst, _PIL_FORMATS[fmt], **options)


# ---------- Инструменты для изображений ----------

# Что дописываем к имени копии: «фото (сжато).jpg»
TOOL_SUFFIXES = {
    "compress": "сжато", "strip": "без метаданных", "trim": "обрезано", "half": "50%",
    "rotate": "повёрнуто", "gray": "ч-б", "white_bg": "белый фон",
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
    """Изменённая копия картинки рядом с оригиналом, в том же формате."""
    if formats.detect(src) != formats.IMAGE:
        raise ConversionError("Инструменты работают только с изображениями")
    if tool not in TOOL_SUFFIXES:
        raise ConversionError("Неизвестный инструмент")
    ext = src.suffix.lower().lstrip(".")
    fmt = _EXT_ALIASES.get(ext, ext)
    if fmt not in _PIL_FORMATS:  # PSD, TGA и PPM Pillow умеет только читать
        fmt = "png"
    if fmt == "heic" and pillow_heif is None:
        raise ConversionError("HEIC недоступен: не установлен pillow-heif")

    dst = free_path(src.with_name(f"{src.stem} ({TOOL_SUFFIXES[tool]}).{fmt}"))

    def write(tmp: Path) -> None:
        _apply_image_tool(src, tmp, fmt, tool)
        if tool == "compress" and tmp.stat().st_size >= src.stat().st_size:
            raise ConversionError("Сильнее сжать не получилось: файл уже оптимизирован")

    _write_via_tmp(dst, write)
    return dst


def _apply_image_tool(src: Path, dst: Path, fmt: str, tool: str) -> None:
    try:
        with Image.open(src) as im:
            if getattr(im, "n_frames", 1) > 1:
                raise ConversionError("Анимированные картинки инструменты пока не обрабатывают")
            icc = im.info.get("icc_profile") if im.mode in _ICC_MODES else None
            upright = im.getexif().get(0x0112, 1) == 1

            if tool == "strip" and fmt == "jpg" and im.format == "JPEG" and upright:
                # Пересжатие испортило бы картинку: сохраняем с исходными таблицами квантования
                im.save(dst, "JPEG", quality="keep", subsampling="keep", icc_profile=icc)
                return

            out = ImageOps.exif_transpose(im)
            exif = out.getexif()  # поворот уже применён, ориентации в нём нет
            options = dict(_SAVE_OPTIONS.get(fmt, {}))

            if tool == "compress":
                if fmt not in _COMPRESS_OPTIONS:
                    raise ConversionError(f"{fmt.upper()} не сжимается: конвертируйте в PNG или JPG")
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
                icc = None  # цветной профиль к серой картинке не подходит
            elif tool == "white_bg":
                if not _has_alpha(out):
                    raise ConversionError("В картинке нет прозрачности")
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
        raise ConversionError(f"Не удалось обработать изображение: {e}") from e


def _quantize(im: Image.Image) -> Image.Image:
    """PNG сжимаем как TinyPNG: палитра из 256 цветов с дизерингом."""
    if _has_alpha(im):
        return im.convert("RGBA").quantize(256, method=Image.Quantize.FASTOCTREE)
    return im.convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT)


def _trim(im: Image.Image) -> Image.Image:
    """Обрезает прозрачные поля, а у непрозрачных картинок — поля цвета левого верхнего пикселя."""
    full = (0, 0, *im.size)
    box = None
    if _has_alpha(im):
        box = im.convert("RGBA").getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if box is None or box == full:
        rgb = im.convert("RGB")
        bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
        box = ImageChops.difference(rgb, bg).convert("L").point(lambda v: 255 if v > 16 else 0).getbbox()
    if box is None:
        raise ConversionError("Картинка однотонная, обрезать нечего")
    if box == full:
        raise ConversionError("Полей не найдено")
    return im.crop(box)


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
