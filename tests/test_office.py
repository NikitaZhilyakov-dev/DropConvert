import os
import shutil
import threading
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from dropconvert import formats, office
from dropconvert.converters import CANCELLED, ConversionError, convert, free_path

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES = ["note.docx", "book.xlsx", "slides.pptx"]


@pytest.mark.parametrize("name, kind", [
    ("a.docx", office.WORD), ("a.DOC", office.WORD), ("a.rtf", office.WORD), ("a.odt", office.WORD),
    ("a.xlsx", office.EXCEL), ("a.ods", office.EXCEL),
    ("a.pptx", office.POWERPOINT), ("a.odp", office.POWERPOINT),
    ("a.txt", None),
])
def test_kind(name, kind):
    assert office.kind(Path(name)) == kind
    assert formats.detect(Path(name)) == (formats.DOCUMENT if kind else None)


def test_no_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "_ms_office_installed", lambda k: False)
    monkeypatch.setattr(office, "find_libreoffice", lambda: None)
    src = tmp_path / "note.docx"
    shutil.copy(FIXTURES / "note.docx", src)
    with pytest.raises(ConversionError, match="LibreOffice"):
        convert(src, "pdf")
    assert [p.name for p in tmp_path.iterdir()] == ["note.docx"]


def test_documents_convert_only_to_pdf(tmp_path):
    src = tmp_path / "note.docx"
    shutil.copy(FIXTURES / "note.docx", src)
    with pytest.raises(ConversionError):
        convert(src, "png")


def test_split_unit():
    assert office.split_unit([Path("a.docx")]) == "pages"
    assert office.split_unit([Path("a.pptx"), Path("b.ppt")]) == "slides"
    assert office.split_unit([Path("a.xlsx")]) == "sheets"
    assert office.split_unit([Path("a.xlsx"), Path("b.pptx")]) == "pages"


def test_labels():
    assert formats.label(formats.DOCUMENT, "pdf") == "Single PDF"
    assert formats.label(formats.DOCUMENT, formats.PDF_PAGES, [Path("a.pptx")]) == "PDF by slides"
    assert formats.label(formats.IMAGE, "pdf") == "PDF"


def test_safe_filename():
    assert office.safe_filename('Итоги "Q1" <draft>') == "Итоги _Q1_ _draft_"
    assert office.safe_filename("a|b?.") == "a_b_"
    assert office.safe_filename(" ") == "sheet"


def test_free_path_for_folder(tmp_path):
    (tmp_path / "report v1.2 (pages)").mkdir()
    assert free_path(tmp_path / "report v1.2 (pages)", is_dir=True).name == "report v1.2 (pages) (1)"


def test_split_pdf(tmp_path):
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(100, 100)
    whole = tmp_path / "whole.pdf"
    with whole.open("wb") as f:
        writer.write(f)
    parts = office.split_pdf(whole, tmp_path)
    assert [len(PdfReader(p).pages) for p in parts] == [1, 1, 1]


def test_xlsx_sheets():
    assert office.xlsx_sheets(FIXTURES / "sheets.xlsx") == [
        ('Итоги "Q1" <draft>', True), ("Empty", True), ("Long", True), ("Secret", False),
    ]


def test_xlsx_show_only(tmp_path):
    out = tmp_path / "one.xlsx"
    office.xlsx_show_only(FIXTURES / "sheets.xlsx", 2, out)
    assert [visible for _, visible in office.xlsx_sheets(out)] == [False, False, True, False]
    import zipfile
    with zipfile.ZipFile(out) as z:
        xml = z.read("xl/workbook.xml").decode()
        assert 'activeTab="2"' in xml and xml.count("activeTab=") == 1
        # Everything else in the package is untouched
        assert z.namelist() == zipfile.ZipFile(FIXTURES / "sheets.xlsx").namelist()



def test_xlsx_fit_columns(tmp_path):
    import zipfile
    from xml.etree import ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    out = tmp_path / "fit.xlsx"
    office.xlsx_fit_columns(FIXTURES / "sheets.xlsx", out)
    with zipfile.ZipFile(out) as z:
        sheets = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]
        assert len(sheets) == 4
        for name in sheets:
            root = ET.fromstring(z.read(name))
            children = [c.tag for c in root]
            assert children[0] == f"{ns}sheetPr"
            assert root.find(f"{ns}sheetPr/{ns}pageSetUpPr").get("fitToPage") == "1"
            setup = root.find(f"{ns}pageSetup")
            assert (setup.get("fitToWidth"), setup.get("fitToHeight")) == ("1", "0")
            assert children.index(f"{ns}pageSetup") == children.index(f"{ns}pageMargins") + 1


@pytest.mark.parametrize("before, after", [
    # No sheetPr, no pageSetup
    ('<worksheet a="1"><sheetData/><pageMargins left="1"/><drawing r:id="x"/></worksheet>',
     '<worksheet a="1"><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr><sheetData/><pageMargins left="1"/>'
     '<pageSetup fitToWidth="1" fitToHeight="0"/><drawing r:id="x"/></worksheet>'),
    # Empty sheetPr, pageSetup with its own settings
    ('<worksheet><sheetPr codeName="S"/><sheetData/><pageSetup orientation="landscape" fitToHeight="5"/></worksheet>',
     '<worksheet><sheetPr codeName="S"><pageSetUpPr fitToPage="1"/></sheetPr><sheetData/>'
     '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>'),
    # A namespace prefix
    ('<x:worksheet><x:sheetPr><x:tabColor rgb="FF"/></x:sheetPr><x:sheetData/></x:worksheet>',
     '<x:worksheet><x:sheetPr><x:tabColor rgb="FF"/><x:pageSetUpPr fitToPage="1"/></x:sheetPr><x:sheetData/>'
     '<x:pageSetup fitToWidth="1" fitToHeight="0"/></x:worksheet>'),
    # The author already chose "Fit to": left alone
    ('<worksheet><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr><pageSetup fitToWidth="2" fitToHeight="1"/></worksheet>',
     '<worksheet><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr><pageSetup fitToWidth="2" fitToHeight="1"/></worksheet>'),
])
def test_fit_columns_xml(before, after):
    assert office._fit_columns_xml(before) == after

# These drive the real Word, Excel and PowerPoint (or LibreOffice), so they run only on request
office_tests = pytest.mark.skipif(
    not os.environ.get("DROPCONVERT_OFFICE_TESTS"), reason="set DROPCONVERT_OFFICE_TESTS=1 to run"
)


@office_tests
@pytest.mark.parametrize("name", SAMPLES)
def test_to_pdf(tmp_path, name):
    src = tmp_path / name
    shutil.copy(FIXTURES / name, src)
    out = convert(src, "pdf")
    assert out == src.with_suffix(".pdf")
    assert out.read_bytes().startswith(b"%PDF")
    assert not list(tmp_path.glob("*.converting*"))


@office_tests
def test_cancel(tmp_path):
    src = tmp_path / "note.docx"
    shutil.copy(FIXTURES / "note.docx", src)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ConversionError, match=CANCELLED):
        convert(src, "pdf", cancel=cancel)
    assert not src.with_suffix(".pdf").exists()


@office_tests
@pytest.mark.parametrize("name, files", [
    ("long.docx", [f"long - {i:02}.pdf" for i in range(1, 7)]),
    ("slides.pptx", ["slides - 01.pdf", "slides - 02.pdf", "slides - 03.pdf"]),
    # "Empty" is a blank sheet and "Secret" is hidden: both are skipped
    ("sheets.xlsx", ["sheets - 1 Итоги _Q1_ _draft_.pdf", "sheets - 2 Long.pdf"]),
])
def test_to_pdf_pages(tmp_path, name, files):
    src = tmp_path / name
    shutil.copy(FIXTURES / name, src)
    out = convert(src, formats.PDF_PAGES)
    assert out.is_dir() and out.name == f"{src.stem} ({office.split_unit([src])})"
    assert sorted(p.name for p in out.iterdir()) == files
    if name == "sheets.xlsx":
        assert len(PdfReader(out / files[1]).pages) > 1  # a long sheet stays one file
    assert not list(tmp_path.glob("*.converting*"))



@office_tests
@pytest.mark.parametrize("fmt", ["pdf", formats.PDF_PAGES])
def test_excel_fits_all_columns(tmp_path, fmt):
    # 40 columns × 200 rows: dozens of pages at 100%, one page with all columns fitted
    src = tmp_path / "wide.xlsx"
    shutil.copy(FIXTURES / "wide.xlsx", src)
    out = convert(src, fmt)
    pdf = out if fmt == "pdf" else next(out.iterdir())
    pages = PdfReader(pdf).pages
    assert len(pages) <= 2
    cells = pages[0].extract_text().split()
    assert "1001" in cells and "1040" in cells  # the first and last column of row 1
