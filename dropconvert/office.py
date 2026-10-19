"""Office documents to PDF via Microsoft Office or, if it isn't installed, LibreOffice.

Rendering Word, Excel and PowerPoint files correctly takes the real application, so
DropConvert drives one: AppleScript on macOS, COM on Windows, headless soffice elsewhere.

Two modes: one PDF for the whole document, or a PDF per page (Word), slide (PowerPoint)
or sheet (Excel). A sheet can span several pages, so sheets are exported one by one
instead of cutting the whole PDF.

Excel sheets print with "Fit All Columns on One Page" unless the author already chose
a "Fit to" scaling: a wide table shouldn't come out as a stack of column strips.
"""
from __future__ import annotations

import html
import itertools
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

WORD, EXCEL, POWERPOINT = "word", "excel", "powerpoint"

KINDS = {
    WORD: {"doc", "docx", "docm", "dot", "dotx", "rtf", "odt"},
    EXCEL: {"xls", "xlsx", "xlsm", "xlsb", "ods"},
    POWERPOINT: {"ppt", "pptx", "pptm", "pps", "ppsx", "odp"},
}

# What "PDF by pages" splits a document into
UNITS = {WORD: "pages", EXCEL: "sheets", POWERPOINT: "slides"}

TIMEOUT = 300  # seconds; a dialog left open in Office (a password, say) mustn't hang the queue forever

NO_BACKEND = "Install Microsoft Office or LibreOffice to convert documents"


class OfficeError(Exception):
    pass


class Cancelled(OfficeError):
    pass


def kind(path: Path) -> str | None:
    ext = path.suffix.lower().lstrip(".")
    return next((k for k, exts in KINDS.items() if ext in exts), None)


def split_unit(paths) -> str:
    """"pages", "slides" or "sheets": what the files split into; "pages" for a mix."""
    units = {UNITS.get(kind(Path(p)), "pages") for p in paths}
    return units.pop() if len(units) == 1 else "pages"


def to_pdf(src: Path, dst: Path, cancel: threading.Event) -> None:
    k = _kind(src)
    backend = _backend(k)
    if backend == "mac":
        with _mac_office(k, src, _MAC_EXPORT[k], cancel) as (work, _):
            _move_pdf(work / "out.pdf", dst)
    elif backend == "win":
        _windows_office(k, src, dst)
    else:
        _libreoffice(backend, src, dst, cancel)


def to_pdf_pages(src: Path, out_dir: Path, cancel: threading.Event) -> list[Path]:
    """Separate PDFs in out_dir: "report - 01.pdf", … or "book - 1 Sheet name.pdf" for Excel."""
    k = _kind(src)
    if k == EXCEL:
        parts = _export_sheets(src, out_dir, cancel)
        if not parts:
            raise OfficeError("Nothing to export: every sheet is empty or hidden")
        width = len(str(len(parts)))
        names = [f"{src.stem} - {i:0{width}} {safe_filename(sheet)}.pdf"
                 for i, (sheet, _) in enumerate(parts, 1)]
        files = [path for _, path in parts]
    else:
        whole = out_dir / ".whole.pdf"
        to_pdf(src, whole, cancel)
        files = split_pdf(whole, out_dir)
        whole.unlink()
        width = max(2, len(str(len(files))))  # "01" sorts right next to "10" in Finder
        names = [f"{src.stem} - {i:0{width}}.pdf" for i in range(1, len(files) + 1)]
    result = []
    for path, name in zip(files, names):
        result.append(path.rename(out_dir / name))
    return result


def _kind(src: Path) -> str:
    k = kind(src)
    if k is None:
        raise OfficeError("Not an Office document")
    return k


def _backend(k: str) -> str:
    """"mac", "win" or the path to soffice."""
    if _ms_office_installed(k):
        return "mac" if sys.platform == "darwin" else "win"
    if soffice := find_libreoffice():
        return soffice
    raise OfficeError(NO_BACKEND)


def _export_sheets(src: Path, out_dir: Path, cancel: threading.Event) -> list[tuple[str, Path]]:
    """(sheet name, PDF) for each visible, non-empty sheet, in workbook order."""
    backend = _backend(EXCEL)
    if backend == "mac":
        parts = []
        with _mac_office(EXCEL, src, _MAC_EXCEL_SHEETS, cancel) as (work, lines):
            for line in lines:
                index, name = line.split("\t", 1)
                parts.append((name, _move_pdf(work / f"sheet-{index}.pdf", out_dir / f".sheet-{index}.pdf")))
        return parts
    if backend == "win":
        return _windows_sheets(src, out_dir)
    return _libreoffice_sheets(backend, src, out_dir, cancel)


def _move_pdf(pdf: Path, dst: Path) -> Path:
    if not pdf.exists() or pdf.stat().st_size == 0:
        raise OfficeError("The application didn't produce a PDF")
    shutil.move(pdf, dst)
    return dst


def split_pdf(pdf: Path, out_dir: Path) -> list[Path]:
    """One file per page: .page-1.pdf, .page-2.pdf, …"""
    from pypdf import PdfReader, PdfWriter

    # Office's PDFs have small quirks pypdf works around; its warnings about them are noise
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(pdf)
        files = []
        for i, page in enumerate(reader.pages, 1):
            writer = PdfWriter()
            writer.add_page(page)
            path = out_dir / f".page-{i}.pdf"
            with path.open("wb") as f:
                writer.write(f)
            files.append(path)
    except Exception as e:  # pypdf raises its own errors as well as OSError
        raise OfficeError(f"Couldn't split the PDF: {e}") from e
    if not files:
        raise OfficeError("The document has no pages")
    return files


_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def safe_filename(name: str) -> str:
    """A sheet name as a file name: Excel allows " < > | in names, Windows doesn't."""
    return _UNSAFE.sub("_", name).strip().rstrip(".") or "sheet"


# ---------- Running a process with cancel and timeout ----------

def _run(cmd: list[str], cancel: threading.Event) -> str:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", creationflags=flags,
    )
    deadline = time.monotonic() + TIMEOUT + 30  # osascript has its own timeout, give it a head start
    timed_out = threading.Event()

    def watch() -> None:
        while proc.poll() is None:
            if cancel.wait(0.2):
                proc.kill()
                return
            if time.monotonic() > deadline:
                timed_out.set()
                proc.kill()
                return

    threading.Thread(target=watch, daemon=True).start()
    out, err = proc.communicate()

    if cancel.is_set():
        raise Cancelled()
    if timed_out.is_set():
        raise OfficeError(f"{Path(cmd[0]).name} didn't finish in {TIMEOUT // 60} minutes")
    if proc.returncode != 0:
        lines = [line for line in err.splitlines() if line.strip()]
        raise OfficeError(lines[-1] if lines else f"{Path(cmd[0]).name} exited with code {proc.returncode}")
    return out.strip("\n")


# ---------- Microsoft Office ----------

_MAC_APPS = {WORD: "Microsoft Word", EXCEL: "Microsoft Excel", POWERPOINT: "Microsoft PowerPoint"}
_WIN_PROGIDS = {WORD: "Word.Application", EXCEL: "Excel.Application", POWERPOINT: "PowerPoint.Application"}
_WIN_COLLECTIONS = {WORD: "Documents", EXCEL: "Workbooks", POWERPOINT: "Presentations"}
_WIN_NO_ALERTS = {WORD: 0, EXCEL: False, POWERPOINT: 1}  # wdAlertsNone, False, ppAlertsNone


def _ms_office_installed(k: str) -> bool:
    if sys.platform == "darwin":
        name = f"{_MAC_APPS[k]}.app"
        return any((root / name).exists() for root in (Path("/Applications"), Path.home() / "Applications"))
    if sys.platform == "win32":
        import winreg

        try:
            winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{_WIN_PROGIDS[k]}\CLSID").Close()
            return True
        except OSError:
            return False
    return False


# Office for Mac is sandboxed: a file outside its containers needs a "Grant access" click.
# The shared Office group container is readable and writable without asking.
_MAC_OFFICE_DIR = Path.home() / "Library" / "Group Containers" / "UBF8T346G9.Office" / "DropConvert"

# "Fit All Columns on One Page" for worksheet ws, unless it already uses "Fit to" (zoom is false).
# Excel refuses "automatic" (false or 0) for the height here, so it gets a page count no sheet reaches.
_MAC_FIT_COLUMNS = """
                    set ps to page setup object of ws
                    if zoom of ps is not false then
                        set zoom of ps to false
                        set fit to pages wide of ps to 1
                        set fit to pages tall of ps to 32767
                    end if
"""

# Script bodies. They get src, outDir and mayQuit, and may add lines to out (separated by lineSep,
# fields by fieldSep).
_MAC_EXPORT = {
    WORD: """
        tell application "Microsoft Word"
            open (POSIX file src)
            set doc to active document
            try
                save as doc file name (outDir & "/out.pdf") file format format PDF
            on error e
                close doc saving no
                error e
            end try
            close doc saving no
            if mayQuit and (count of documents) = 0 then quit
        end tell
    """,
    EXCEL: """
        tell application "Microsoft Excel"
            open (POSIX file src)
            set wb to active workbook
            try
                repeat with i from 1 to count of worksheets of wb
                    set ws to worksheet i of wb
                    if visible of ws is sheet visible then
""" + _MAC_FIT_COLUMNS + """
                    end if
                end repeat
                save workbook as wb filename (outDir & "/out.pdf") file format PDF file format
            on error e
                close wb saving no
                error e
            end try
            close wb saving no
            if mayQuit and (count of workbooks) = 0 then quit
        end tell
    """,
    POWERPOINT: """
        tell application "Microsoft PowerPoint"
            open (POSIX file src)
            set p to active presentation
            try
                save p in (POSIX file (outDir & "/out.pdf")) as save as PDF
            on error e
                close active presentation saving no
                error e
            end try
            close active presentation saving no
            if mayQuit and (count of presentations) = 0 then quit
        end tell
    """,
}

# Exporting a single sheet still exports the whole workbook on Mac, but hidden sheets are left
# out: hide all but one and export, once per sheet. The file is a copy, so hiding is harmless.
# An empty sheet would become a blank page, so it's skipped, as are sheets hidden by the author.
_MAC_EXCEL_SHEETS = """
        tell application "Microsoft Excel"
            open (POSIX file src)
            set wb to active workbook
            try
                set n to count of worksheets of wb
                set wanted to {}
                repeat with i from 1 to n
                    set ws to worksheet i of wb
                    if visible of ws is sheet visible then
                        set ur to used range of ws
                        set isEmpty to (count of cells of ur) is 1 and (value of ur as text) is "" ¬
                            and (count of shapes of ws) is 0
                        if not isEmpty then set end of wanted to i
                    end if
                end repeat
                repeat with w in wanted
                    set i to contents of w
                    set ws to worksheet i of wb
""" + _MAC_FIT_COLUMNS + """
                    set visible of ws to sheet visible
                    repeat with j from 1 to n
                        if j is not i then set visible of worksheet j of wb to sheet hidden
                    end repeat
                    save workbook as wb filename (outDir & "/sheet-" & i & ".pdf") file format PDF file format
                    set out to out & lineSep & i & fieldSep & (name of worksheet i of wb)
                end repeat
            on error e
                close wb saving no
                error e
            end try
            close wb saving no
            if mayQuit and (count of workbooks) = 0 then quit
        end tell
"""

# Conversions go one at a time: the scripts rely on "the active document".
# The queue counter has its own lock so files can join the queue while one converts.
_mac_lock = threading.Lock()
_mac_queue_lock = threading.Lock()
_mac_waiting: Counter[str] = Counter()
_mac_launched: set[str] = set()  # apps DropConvert started and should quit when the queue empties


def _mac_script(k: str, body: str) -> str:
    # argv: source, output folder, "last" if no more files wait for this app, "ours" if DropConvert
    # launched it earlier. The first output line is "launched" if the app wasn't running.
    # Not "tab" and "linefeed": AppleScript ignores case, and inside "tell" Excel's own "tab" term wins.
    app = _MAC_APPS[k]
    return f"""
on run argv
    set src to item 1 of argv
    set outDir to item 2 of argv
    set fieldSep to character id 9
    set lineSep to character id 10
    set out to ""
    set wasRunning to application "{app}" is running
    -- Quit only an app DropConvert started, and only after the last file in the queue
    set mayQuit to (item 3 of argv) is "last" and ((item 4 of argv) is "ours" or not wasRunning)
    with timeout of {TIMEOUT} seconds
        {body}
    end timeout
    if wasRunning then return "running" & out
    return "launched" & out
end run
"""


@contextmanager
def _mac_office(k: str, src: Path, body: str, cancel: threading.Event) -> Iterator[tuple[Path, list[str]]]:
    """Runs a script on a copy of src; yields the folder with its output and the lines it printed."""
    with _mac_queue_lock:
        _mac_waiting[k] += 1
    try:
        with _mac_lock:
            if cancel.is_set():
                raise Cancelled()
            work = _MAC_OFFICE_DIR / uuid.uuid4().hex
            work.mkdir(parents=True)
            try:
                copy = work / src.name
                shutil.copy2(src, copy)
                with _mac_queue_lock:
                    last = _mac_waiting[k] == 1
                try:
                    out = _run(["osascript", "-e", _mac_script(k, body), str(copy), str(work),
                                "last" if last else "more", "ours" if k in _mac_launched else ""], cancel)
                except Cancelled:
                    raise
                except OfficeError as e:
                    raise OfficeError(_mac_error(k, str(e))) from None
                state, *lines = out.split("\n")
                if last:
                    _mac_launched.discard(k)
                elif state == "launched":
                    _mac_launched.add(k)
                yield work, [line for line in lines if line]
            finally:
                shutil.rmtree(work, ignore_errors=True)
    finally:
        with _mac_queue_lock:
            _mac_waiting[k] -= 1


def _mac_error(k: str, message: str) -> str:
    if "-1743" in message:  # errAEEventNotPermitted
        return (f"No access to {_MAC_APPS[k]}. Allow it in System Settings → "
                "Privacy & Security → Automation")
    if "-1712" in message:  # errAETimeout
        return f"{_MAC_APPS[k]} didn't respond: is a dialog open in it?"
    # osascript: "123:456: execution error: <message> (-1234)"
    return message.split("execution error:", 1)[-1].strip()


@contextmanager
def _com_app(k: str):
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        # DispatchEx starts a separate instance, so the user's open windows aren't touched
        app = win32com.client.DispatchEx(_WIN_PROGIDS[k])
        try:
            app.DisplayAlerts = _WIN_NO_ALERTS[k]
            if k == WORD:
                app.Visible = False
            yield app
        except pythoncom.com_error as e:
            raise OfficeError(_com_message(e)) from None
        finally:
            # PowerPoint is single-instance: quit only if nobody else has files open in it
            try:
                if getattr(app, _WIN_COLLECTIONS[k]).Count == 0:
                    app.Quit()
            except pythoncom.com_error:
                pass
    finally:
        pythoncom.CoUninitialize()


def _fit_columns(ws) -> None:
    """"Fit All Columns on One Page", unless the sheet already uses "Fit to" scaling."""
    import pythoncom

    try:
        setup = ws.PageSetup
        if setup.Zoom is not False:
            setup.Zoom = False
            setup.FitToPagesWide = 1
            setup.FitToPagesTall = False  # as many pages as it takes
    except pythoncom.com_error:
        pass  # PageSetup needs a printer driver; without one the sheet prints as is


def _open_workbook(app, src: Path):
    # A dummy password makes a protected file fail instead of waiting in a dialog
    return app.Workbooks.Open(str(src), UpdateLinks=0, ReadOnly=True, Password="-", AddToMru=False)


def _windows_office(k: str, src: Path, dst: Path) -> None:
    with _com_app(k) as app:
        if k == WORD:
            doc = app.Documents.Open(str(src), ConfirmConversions=False, ReadOnly=True,
                                     AddToRecentFiles=False, PasswordDocument="-", Visible=False)
            try:
                doc.ExportAsFixedFormat(str(dst), 17)  # wdExportFormatPDF
            finally:
                doc.Close(0)  # wdDoNotSaveChanges
        elif k == EXCEL:
            wb = _open_workbook(app, src)
            try:
                for ws in wb.Worksheets:
                    if ws.Visible == -1:  # xlSheetVisible
                        _fit_columns(ws)
                wb.ExportAsFixedFormat(0, str(dst))  # xlTypePDF
            finally:
                wb.Close(False)
        else:
            pres = app.Presentations.Open(str(src), ReadOnly=True, Untitled=False, WithWindow=False)
            try:
                pres.SaveAs(str(dst), 32)  # ppSaveAsPDF
            finally:
                pres.Close()


def _windows_sheets(src: Path, out_dir: Path) -> list[tuple[str, Path]]:
    parts = []
    with _com_app(EXCEL) as app:
        wb = _open_workbook(app, src)
        try:
            for i in range(1, wb.Worksheets.Count + 1):
                ws = wb.Worksheets(i)
                if ws.Visible != -1:  # xlSheetVisible
                    continue
                if app.WorksheetFunction.CountA(ws.UsedRange) == 0 and ws.Shapes.Count == 0:
                    continue  # would be a blank page
                path = out_dir / f".sheet-{i}.pdf"
                _fit_columns(ws)
                ws.ExportAsFixedFormat(0, str(path))
                parts.append((ws.Name, path))
        finally:
            wb.Close(False)
    return parts


def _com_message(e) -> str:
    excepinfo = e.excepinfo if len(e.args) > 2 else None
    if excepinfo and excepinfo[2]:
        return str(excepinfo[2]).strip()
    return str(e.args[1] if len(e.args) > 1 else e)


# ---------- LibreOffice ----------

def find_libreoffice() -> str | None:
    if found := shutil.which("soffice") or shutil.which("libreoffice"):
        return found
    candidates = []
    if sys.platform == "darwin":
        candidates.append(Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"))
    elif sys.platform == "win32":
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            if base := os.environ.get(var):
                candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    return next((str(p) for p in candidates if p.exists()), None)


_lo_lock = threading.Lock()


def _lo_convert(soffice: str, src: Path, out_dir: Path, fmt: str, cancel: threading.Event) -> Path:
    # Our own profile: otherwise soffice hands the job to a LibreOffice the user has open and exits
    profile = Path(tempfile.gettempdir()) / "dropconvert-libreoffice"
    with _lo_lock:  # one profile can't be used twice at once
        _run([soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
              "--nolockcheck", "--convert-to", fmt, "--outdir", str(out_dir), str(src)], cancel)
    out = out_dir / f"{src.stem}.{fmt}"
    if not out.exists():
        raise OfficeError("LibreOffice couldn't open the document")
    return out


def _libreoffice(soffice: str, src: Path, dst: Path, cancel: threading.Event) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        if kind(src) == EXCEL:
            src = _lo_fit_columns(soffice, src, Path(tmp), cancel)
        _move_pdf(_lo_convert(soffice, src, Path(tmp), "pdf", cancel), dst)


def _lo_fit_columns(soffice: str, src: Path, tmp: Path, cancel: threading.Event) -> Path:
    """An XLSX copy of the workbook with "Fit All Columns on One Page" (XLS and ODS go through
    XLSX first: page setup is edited in its XML)."""
    book = src
    if src.suffix.lower() not in (".xlsx", ".xlsm"):
        book = _lo_convert(soffice, src, tmp / "xlsx", "xlsx", cancel)
    fit = tmp / f"fit{book.suffix}"
    xlsx_fit_columns(book, fit)
    return fit


def _libreoffice_sheets(soffice: str, src: Path, out_dir: Path, cancel: threading.Event) -> list[tuple[str, Path]]:
    """soffice has no "export one sheet" switch: make a copy of the workbook with only that sheet
    visible and convert the copy."""
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        book = _lo_fit_columns(soffice, src, tmp, cancel)
        for i, (name, visible) in enumerate(xlsx_sheets(book)):
            if not visible:
                continue
            one = tmp / f"sheet-{i}{book.suffix}"
            xlsx_show_only(book, i, one)
            pdf = _lo_convert(soffice, one, tmp, "pdf", cancel)
            if _page_count(pdf):  # LibreOffice prints nothing for an empty sheet
                parts.append((name, _move_pdf(pdf, out_dir / f".sheet-{i}.pdf")))
    return parts


def _page_count(pdf: Path) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(pdf).pages)
    except Exception:
        return 0


# xl/workbook.xml lists sheets as <sheet name="…" sheetId="…" state="hidden" r:id="…"/>,
# with or without a namespace prefix. Excel leaves ">" unescaped inside values ("<draft>"),
# so the tag ends at the first ">" outside quotes.
_SHEET_TAG = re.compile(r"""<(?:\w+:)?sheet\b(?:[^>"']|"[^"]*"|'[^']*')*>""")
_STATE_ATTR = re.compile(r'\sstate="([^"]*)"')
_WORKBOOK_XML = "xl/workbook.xml"


def xlsx_sheets(path: Path) -> list[tuple[str, bool]]:
    """(name, visible) for each sheet of an XLSX, in order."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read(_WORKBOOK_XML).decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as e:
        raise OfficeError(f"Couldn't read the workbook: {e}") from e
    sheets = []
    for tag in _SHEET_TAG.findall(xml):
        name = re.search(r'\sname="([^"]*)"', tag)
        state = _STATE_ATTR.search(tag)
        sheets.append((html.unescape(name.group(1)) if name else "sheet",
                       not state or state.group(1) == "visible"))
    return sheets


def xlsx_show_only(src: Path, index: int, dst: Path) -> None:
    """A copy of the workbook where only sheet number index (from 0) is visible and active."""
    counter = itertools.count()

    def fix(m: re.Match) -> str:
        tag = _STATE_ATTR.sub("", m.group(0))
        if next(counter) != index:
            tag = re.sub(r"\s*(/?>)$", r' state="hidden"\1', tag)
        return tag

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename == _WORKBOOK_XML:
                xml = _SHEET_TAG.sub(fix, data.decode("utf-8"))
                # The active tab can't be a hidden sheet
                xml = re.sub(r'\s(?:activeTab|firstSheet)="\d+"', "", xml)
                xml = re.sub(r"<((?:\w+:)?workbookView)\b", rf'<\1 activeTab="{index}" firstSheet="{index}"', xml)
                data = xml.encode("utf-8")
            zout.writestr(info, data)


# Worksheet XML: <sheetPr><pageSetUpPr fitToPage="1"/></sheetPr> turns "Fit to" on and
# <pageSetup fitToWidth="1" fitToHeight="0"/> sets it to 1 page wide, any number tall.
# Elements of <worksheet> have a fixed order: pageSetup goes after pageMargins, sheetPr first.
_FIT_TO_PAGE = re.compile(r'<(?:\w+:)?pageSetUpPr\b[^>]*\sfitToPage="(?:1|true)"')
_AFTER_PAGE_SETUP = ("headerFooter", "rowBreaks", "colBreaks", "customProperties", "cellWatches",
                     "ignoredErrors", "smartTags", "drawing", "legacyDrawing", "legacyDrawingHF",
                     "drawingHF", "picture", "oleObjects", "controls", "webPublishItems",
                     "tableParts", "extLst")


def xlsx_fit_columns(src: Path, dst: Path) -> None:
    """A copy of the workbook where every worksheet prints all columns on one page wide,
    except sheets that already use "Fit to" scaling."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if re.fullmatch(r"xl/worksheets/[^/]+\.xml", info.filename):
                data = _fit_columns_xml(data.decode("utf-8")).encode("utf-8")
            zout.writestr(info, data)


def _fit_columns_xml(xml: str) -> str:
    if _FIT_TO_PAGE.search(xml):
        return xml
    root = re.search(r"<(\w+:)?worksheet\b", xml)
    if not root:
        return xml
    p = root.group(1) or ""  # namespace prefix, "" or "x:"

    # <pageSetUpPr> is the last child of <sheetPr>; <sheetPr> is the first child of <worksheet>
    if m := re.search(rf"<{p}pageSetUpPr\b[^>]*>", xml):
        tag = re.sub(r'\sfitToPage="[^"]*"', "", m.group(0))
        tag = re.sub(r"\s*(/?>)$", r' fitToPage="1"\1', tag)
        xml = xml[:m.start()] + tag + xml[m.end():]
    elif m := re.search(rf"<{p}sheetPr\b[^>]*?/>", xml):
        xml = xml[:m.start()] + m.group(0)[:-2].rstrip() + f'><{p}pageSetUpPr fitToPage="1"/></{p}sheetPr>' + xml[m.end():]
    elif m := re.search(rf"</{p}sheetPr>", xml):
        xml = xml[:m.start()] + f'<{p}pageSetUpPr fitToPage="1"/>' + xml[m.start():]
    else:
        end = xml.index(">", root.end()) + 1
        xml = xml[:end] + f'<{p}sheetPr><{p}pageSetUpPr fitToPage="1"/></{p}sheetPr>' + xml[end:]

    if m := re.search(rf"<{p}pageSetup\b[^>]*?/?>", xml):
        tag = re.sub(r'\s(?:fitToWidth|fitToHeight)="[^"]*"', "", m.group(0))
        tag = re.sub(r"\s*(/?>)$", r' fitToWidth="1" fitToHeight="0"\1', tag)
        return xml[:m.start()] + tag + xml[m.end():]
    setup = f'<{p}pageSetup fitToWidth="1" fitToHeight="0"/>'
    # pageMargins comes after printOptions: insert after whichever of them is last
    if before := list(re.finditer(rf"<{p}(?:printOptions|pageMargins)\b[^>]*/>", xml)):
        end = before[-1].end()
        return xml[:end] + setup + xml[end:]
    after = "|".join(_AFTER_PAGE_SETUP)
    if m := re.search(rf"<{p}(?:{after})\b", xml):
        return xml[:m.start()] + setup + xml[m.start():]
    end = xml.rindex(f"</{p}worksheet>")
    return xml[:end] + setup + xml[end:]
