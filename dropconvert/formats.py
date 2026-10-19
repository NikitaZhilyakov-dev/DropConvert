"""Supported formats: what we can read and what we can convert to."""
from __future__ import annotations

from pathlib import Path

from PIL import features

from . import office

IMAGE = "image"
VIDEO = "video"
AUDIO = "audio"
DOCUMENT = "document"

CATEGORY_ORDER = (IMAGE, VIDEO, AUDIO, DOCUMENT)
CATEGORY_NAMES = {IMAGE: "Images", VIDEO: "Video", AUDIO: "Audio", DOCUMENT: "Documents"}

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
    # Word, Excel, PowerPoint and their OpenDocument counterparts
    DOCUMENT: set().union(*office.KINDS.values()),
}

_IMAGE_TARGETS = ["png", "jpg", "webp", "heic", "avif", "gif", "bmp", "tiff", "ico", "pdf"]

TARGETS = {
    IMAGE: [f for f in _IMAGE_TARGETS if f != "avif" or features.check("avif")],
    VIDEO: ["mp4", "mov", "mkv", "webm", "avi", "gif", "mp3", "wav", "m4a"],
    AUDIO: ["mp3", "wav", "flac", "ogg", "m4a", "opus"],
    DOCUMENT: ["pdf", "pdf_pages"],
}

# Not an extension: a folder with a PDF per page, slide or sheet
PDF_PAGES = "pdf_pages"


def label(category: str, fmt: str, paths=()) -> str:
    """Button and sector text for a target; paths decide whether documents split by pages,
    slides or sheets."""
    if category == DOCUMENT:
        return "Single PDF" if fmt == "pdf" else f"PDF by {office.split_unit(paths)}"
    return fmt.upper()


# Image tools: the format stays the same, a modified copy appears next to the original
TOOLS = ["compress", "strip", "trim", "half", "rotate", "gray", "white_bg"]


def detect(path: Path) -> str | None:
    """The file's category by extension, or None if the format isn't supported."""
    ext = path.suffix.lower().lstrip(".")
    for category in CATEGORY_ORDER:
        if ext in INPUT_EXTENSIONS[category]:
            return category
    return None
