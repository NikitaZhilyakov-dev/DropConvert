"""Поддерживаемые форматы: что умеем читать и во что умеем конвертировать."""
from __future__ import annotations

from pathlib import Path

from PIL import features

IMAGE = "image"
VIDEO = "video"
AUDIO = "audio"

CATEGORY_ORDER = (IMAGE, VIDEO, AUDIO)
CATEGORY_NAMES = {IMAGE: "Изображения", VIDEO: "Видео", AUDIO: "Аудио"}

INPUT_EXTENSIONS = {
    IMAGE: {
        "png", "jpg", "jpeg", "jpe", "webp", "bmp", "tif", "tiff", "gif", "ico",
        "heic", "heif", "avif", "tga", "ppm", "psd",
    },
    VIDEO: {
        "mp4", "mov", "m4v", "mkv", "avi", "webm", "wmv", "flv", "mpg", "mpeg",
        "3gp", "ts", "mts", "m2ts",
    },
    AUDIO: {
        "mp3", "wav", "flac", "ogg", "oga", "m4a", "aac", "wma", "opus", "aiff",
        "aif", "amr", "caf",
    },
}

_IMAGE_TARGETS = ["png", "jpg", "webp", "heic", "avif", "gif", "bmp", "tiff", "ico", "pdf"]

TARGETS = {
    IMAGE: [f for f in _IMAGE_TARGETS if f != "avif" or features.check("avif")],
    VIDEO: ["mp4", "mov", "mkv", "webm", "avi", "gif", "mp3", "wav", "m4a"],
    AUDIO: ["mp3", "wav", "flac", "ogg", "m4a", "opus"],
}


def detect(path: Path) -> str | None:
    """Категория файла по расширению или None, если формат не поддерживается."""
    ext = path.suffix.lower().lstrip(".")
    for category in CATEGORY_ORDER:
        if ext in INPUT_EXTENSIONS[category]:
            return category
    return None
