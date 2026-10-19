from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from dropconvert import formats, pdf
from dropconvert.converters import ConversionError, convert


@pytest.fixture
def ten_pages(tmp_path):
    """Page i is 100 + i points wide, so the pages can be told apart."""
    path = tmp_path / "report.pdf"
    writer = PdfWriter()
    for i in range(1, 11):
        writer.add_blank_page(width=100 + i, height=200)
    writer.add_metadata({"/Title": "Report"})
    with path.open("wb") as f:
        writer.write(f)
    return path


def widths(path):
    return [int(page.mediabox.width) - 100 for page in PdfReader(path).pages]


def test_detect():
    assert formats.detect(Path("a.PDF")) == formats.PDF
    assert formats.TARGETS[formats.PDF] == [formats.PDF_EXTRACT]


@pytest.mark.parametrize("spec, expected", [
    ("1-3, 5, 8-", [(1, 3), (5, 5), (8, None)]),
    (" 2 – 4 ;7 ", [(2, 4), (7, 7)]),
    ("-3", [(1, 3)]),
    ("4 6", [(4, 4), (6, 6)]),
])
def test_parse_ranges(spec, expected):
    assert pdf.parse_ranges(spec) == expected


@pytest.mark.parametrize("spec", ["", " , ", "-", "a", "1-2-3", "0", "0-2", "5-3"])
def test_parse_ranges_rejects(spec):
    with pytest.raises(pdf.PageRangeError):
        pdf.parse_ranges(spec)


def test_resolve_keeps_order_and_drops_repeats():
    assert pdf.resolve(pdf.parse_ranges("5, 1-3, 2, 9-"), 10) == [4, 0, 1, 2, 8, 9]


@pytest.mark.parametrize("spec", ["11", "8-12", "11-"])
def test_resolve_rejects_missing_pages(spec):
    with pytest.raises(pdf.PageRangeError, match="10 pages"):
        pdf.resolve(pdf.parse_ranges(spec), 10)


def test_describe():
    assert pdf.describe([3]) == "page 4"
    assert pdf.describe([0, 1, 2, 4, 7, 8]) == "pages 1-3, 5, 8-9"
    assert pdf.describe(list(range(0, 60, 2))) == "30 pages"


def test_extract(ten_pages):
    out = convert(ten_pages, formats.PDF_EXTRACT, pages="1-3, 5, 8-")
    assert out.name == "report (pages 1-3, 5, 8-10).pdf"
    assert widths(out) == [1, 2, 3, 5, 8, 9, 10]
    assert PdfReader(out).metadata.title == "Report"
    assert widths(ten_pages) == list(range(1, 11))  # the original is untouched


def test_extract_twice_doesnt_overwrite(ten_pages):
    first = convert(ten_pages, formats.PDF_EXTRACT, pages="2")
    second = convert(ten_pages, formats.PDF_EXTRACT, pages="2")
    assert first.name == "report (page 2).pdf" and second.name == "report (page 2) (1).pdf"


def test_extract_bad_range_leaves_nothing(ten_pages):
    with pytest.raises(ConversionError, match="no page 12"):
        convert(ten_pages, formats.PDF_EXTRACT, pages="3-12")
    assert [p.name for p in ten_pages.parent.iterdir()] == ["report.pdf"]


def test_extract_encrypted(tmp_path, ten_pages):
    locked = tmp_path / "locked.pdf"
    writer = PdfWriter(clone_from=ten_pages)
    writer.encrypt("secret", algorithm="RC4-128")
    with locked.open("wb") as f:
        writer.write(f)
    with pytest.raises(ConversionError, match="password"):
        convert(locked, formats.PDF_EXTRACT, pages="1")
    assert pdf.page_count(locked) is None


def test_extract_broken_file(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    with pytest.raises(ConversionError, match="Couldn't read"):
        convert(broken, formats.PDF_EXTRACT, pages="1")
