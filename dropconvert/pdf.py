"""PDF pages: reading ranges like "1-3, 5, 8-" and saving the chosen pages as a new PDF.

A range is typed by a person, so the parser is forgiving: spaces anywhere, commas or
semicolons between parts, en and em dashes as well as hyphens. "8-" runs to the last
page, "-3" starts from the first. Pages keep the order they were typed in; repeats are dropped.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from pypdf import PdfReader, PdfWriter


class PdfError(Exception):
    pass


class PageRangeError(PdfError):
    pass


_PART = re.compile(r"(\d+)?-(\d+)?|(\d+)")
EXAMPLE = "1-3, 5, 8-"


def plural_pages(n: int) -> str:
    return f"{n} page" if n == 1 else f"{n} pages"


def parse_ranges(spec: str) -> list[tuple[int, int | None]]:
    """"1-3, 5, 8-" -> [(1, 3), (5, 5), (8, None)], pages from 1; None is "to the end"."""
    spec = re.sub(r"\s*[-–—]\s*", "-", spec.strip())
    parts = [part for part in re.split(r"[,;\s]+", spec) if part]
    if not parts:
        raise PageRangeError(f"Enter pages, e.g. {EXAMPLE}")
    ranges = []
    for part in parts:
        m = _PART.fullmatch(part)
        if not m or part == "-":
            raise PageRangeError(f'Can\'t read "{part}": use numbers like {EXAMPLE}')
        if m.group(3):
            first = last = int(m.group(3))
        else:
            first = int(m.group(1)) if m.group(1) else 1
            last = int(m.group(2)) if m.group(2) else None
        if first == 0 or last == 0:
            raise PageRangeError("Pages are numbered from 1")
        if last is not None and last < first:
            raise PageRangeError(f"{first}-{last} goes backwards: write {last}-{first}")
        ranges.append((first, last))
    return ranges


def resolve(ranges: list[tuple[int, int | None]], count: int) -> list[int]:
    """Page indices from 0 for a document of count pages, in the order given, without repeats."""
    if count == 0:
        raise PageRangeError("The PDF has no pages")
    pages: dict[int, None] = {}
    for first, last in ranges:
        last = count if last is None else last
        if last > count or first > count:
            missing = first if first > count else last
            raise PageRangeError(f"There's no page {missing}: the PDF has {plural_pages(count)}")
        pages.update(dict.fromkeys(range(first - 1, last)))
    return list(pages)


def describe(pages: list[int]) -> str:
    """For the file name: "pages 1-3, 5", "page 4"; a long list becomes "12 pages"."""
    if len(pages) == 1:
        return f"page {pages[0] + 1}"
    runs: list[list[int]] = []
    for page in pages:
        if runs and page == runs[-1][1] + 1:
            runs[-1][1] = page
        else:
            runs.append([page, page])
    text = ", ".join(f"{a + 1}" if a == b else f"{a + 1}-{b + 1}" for a, b in runs)
    return f"pages {text}" if len(text) <= 40 else plural_pages(len(pages))


def open_pdf(path: Path) -> PdfReader:
    # Real-world PDFs have small defects pypdf works around; its warnings about them are noise
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):  # an empty password opens view-only PDFs
            raise PdfError("The PDF is password-protected")
        len(reader.pages)  # a broken page tree fails here rather than halfway through writing
    except PdfError:
        raise
    except Exception as e:  # pypdf raises its own errors as well as OSError
        raise PdfError(f"Couldn't read the PDF: {e}") from e
    return reader


def page_count(path: Path) -> int | None:
    """None if the file can't be read: the error comes later, when converting."""
    try:
        return len(open_pdf(path).pages)
    except PdfError:
        return None


def write_pages(reader: PdfReader, pages: list[int], dst: Path) -> None:
    """A new PDF with only these pages; bookmarks pointing to them and the metadata are kept."""
    try:
        writer = PdfWriter()
        writer.append(reader, pages=pages)
        if reader.metadata:
            writer.add_metadata(reader.metadata)
        with dst.open("wb") as f:
            writer.write(f)
    except Exception as e:
        raise PdfError(f"Couldn't save the pages: {e}") from e
