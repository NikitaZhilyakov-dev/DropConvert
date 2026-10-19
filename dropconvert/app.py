"""App window: drag & drop zone, file list, format buttons."""
from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QRunnable, QSettings, Qt, QThreadPool, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import formats, pdf, system
from .converters import apply_tool, convert
from .wheel import RangeInput

ACCENT = "#FF8A1F"

STYLE = f"""
QWidget#root {{ font-size: 13px; }}
QLabel#title {{ font-size: 18px; font-weight: 600; }}
QFrame#dropZone {{
    border: 2px dashed palette(mid); border-radius: 14px;
}}
QFrame#dropZone[hover="true"] {{
    border-color: {ACCENT}; background: rgba(255, 138, 31, 0.10);
}}
QLabel#dropIcon {{ font-size: 34px; color: {ACCENT}; }}
QLabel#dropText {{ color: palette(placeholder-text); }}
QLabel#section {{ font-weight: 600; margin-top: 6px; }}
QPushButton#format {{
    border: 1px solid palette(mid); border-radius: 8px; padding: 6px 4px;
    font-weight: 600; min-width: 52px;
}}
QPushButton#format:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton#format:pressed {{ background: {ACCENT}; color: white; }}
"""

_ids = itertools.count()


@dataclass
class Item:
    path: Path
    category: str
    uid: int = field(default_factory=lambda: next(_ids))
    running: bool = False
    output: Path | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


class TaskSignals(QObject):
    progress = Signal(int, float)
    finished = Signal(int, str)
    failed = Signal(int, str)


class ConvertTask(QRunnable):
    """Converts to format fmt or, if fmt is in formats.TOOLS, applies an image tool.
    pages is the range for formats.PDF_EXTRACT, box the frame for formats.CROP."""

    def __init__(self, item: Item, fmt: str, pages: str | None = None,
                 box: tuple[int, int, int, int] | None = None):
        super().__init__()
        self.item, self.fmt, self.pages, self.box = item, fmt, pages, box
        self.signals = TaskSignals()

    def run(self) -> None:
        uid = self.item.uid
        try:
            if self.fmt in formats.TOOLS:
                out = apply_tool(self.item.path, self.fmt, self.box)
            else:
                out = convert(
                    self.item.path, self.fmt,
                    on_progress=lambda p: self.signals.progress.emit(uid, p),
                    cancel=self.item.cancel,
                    pages=self.pages,
                )
            self.signals.finished.emit(uid, str(out))
        except Exception as e:
            self.signals.failed.emit(uid, str(e))


def expand(paths: list[Path]):
    """Expand folders recursively, skip hidden files."""
    for path in paths:
        if path.is_dir():
            yield from sorted(
                p for p in path.rglob("*") if p.is_file() and not p.name.startswith(".")
            )
        elif path.is_file():
            yield path


def ask_page_range(at: QPoint, paths: list[Path]) -> str | None:
    """Asks which PDF pages to extract. With one file the range is checked against its page count."""
    count = pdf.page_count(paths[0]) if len(paths) == 1 else None
    hint = (f"The PDF has {pdf.plural_pages(count)} · Enter to save" if count
            else f"e.g. {pdf.EXAMPLE} · Enter to save")

    def validate(spec: str) -> str | None:
        try:
            ranges = pdf.parse_ranges(spec)
            if count:
                pdf.resolve(ranges, count)
        except pdf.PageRangeError as e:
            return str(e)
        return None

    title = "Pages to save" if len(paths) == 1 else f"Pages to save from {len(paths)} PDFs"
    with system.FocusKeeper():  # the field needs the keyboard, even when opened mid-drag
        return RangeInput(title, pdf.EXAMPLE).ask(at, hint, validate)


def reveal_in_file_manager(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


class DropZone(QFrame):
    """The visual zone. The whole window handles the drop itself; a click opens a dialog."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("dropZone")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel("⇩")
        icon.setObjectName("dropIcon")
        text = QLabel("Drop files here\nor click to choose")
        text.setObjectName("dropText")
        for w in (icon, text):
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(w)
        self.set_hover(False)

    def set_hover(self, on: bool) -> None:
        self.setProperty("hover", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class MainWindow(QWidget):
    hidden = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("DropConvert")
        self.setAcceptDrops(True)
        self.resize(400, 560)
        self.setStyleSheet(STYLE)

        self.settings = QSettings("DropConvert", "DropConvert")
        self.items: dict[int, Item] = {}
        self.rows: dict[int, QTreeWidgetItem] = {}
        self.tasks: dict[int, ConvertTask] = {}
        self.last_output: Path | None = None

        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(2, (os.cpu_count() or 2) // 2))

        title = QLabel("DropConvert")
        title.setObjectName("title")

        self.drop_zone = DropZone()
        self.drop_zone.setMinimumHeight(150)
        self.drop_zone.clicked.connect(self.pick_files)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._on_double_click)

        self.formats_box = QWidget()
        self.formats_layout = QVBoxLayout(self.formats_box)
        self.formats_layout.setContentsMargins(0, 0, 0, 0)

        self.open_btn = QPushButton("Show result")
        self.open_btn.clicked.connect(lambda: self.last_output and reveal_in_file_manager(self.last_output))
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear)
        self.on_top = QCheckBox("Stay on top")
        self.on_top.toggled.connect(self.set_on_top)

        bottom = QHBoxLayout()
        bottom.addWidget(self.on_top)
        bottom.addStretch()
        bottom.addWidget(clear_btn)
        bottom.addWidget(self.open_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(title)
        layout.addWidget(self.drop_zone, 1)
        layout.addWidget(self.tree, 2)
        layout.addWidget(self.formats_box)
        layout.addLayout(bottom)

        self.on_top.setChecked(self.settings.value("onTop", False, type=bool))
        self._refresh()

    # ----- Drag & drop onto the whole window -----

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_zone.set_hover(True)

    def dragLeaveEvent(self, event) -> None:
        self.drop_zone.set_hover(False)

    def dropEvent(self, event) -> None:
        self.drop_zone.set_hover(False)
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        event.acceptProposedAction()
        self.add_files(paths)

    # ----- Files -----

    def pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Choose files")
        self.add_files([Path(f) for f in files])

    def add_files(self, paths: list[Path]) -> None:
        known = {item.path for item in self.items.values()}
        for path in expand(paths):
            category = formats.detect(path)
            if category is None or path in known:
                continue
            known.add(path)
            item = Item(path, category)
            row = QTreeWidgetItem([path.name, formats.CATEGORY_NAMES[category]])
            row.setToolTip(0, str(path))
            self.items[item.uid] = item
            self.rows[item.uid] = row
            self.tree.addTopLevelItem(row)
        self._refresh()

    def clear(self) -> None:
        for item in self.items.values():
            item.cancel.set()
        self.items.clear()
        self.rows.clear()
        self.tree.clear()
        self._refresh()

    # ----- Format buttons -----

    def _refresh(self) -> None:
        has_items = bool(self.items)
        self.tree.setVisible(has_items)
        self.drop_zone.setMinimumHeight(90 if has_items else 260)
        self.open_btn.setEnabled(self.last_output is not None)

        while self.formats_layout.count():
            child = self.formats_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        present = {item.category for item in self.items.values()}
        for category in formats.CATEGORY_ORDER:
            if category not in present:
                continue
            label = QLabel(f"{formats.CATEGORY_NAMES[category]} → convert to:")
            label.setObjectName("section")
            grid_box = QWidget()
            grid = QGridLayout(grid_box)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(6)
            paths = [item.path for item in self.items.values() if item.category == category]
            for i, fmt in enumerate(formats.TARGETS[category]):
                btn = QPushButton(formats.label(category, fmt, paths))
                btn.setObjectName("format")
                btn.clicked.connect(lambda _=False, c=category, f=fmt: self.start(c, f))
                grid.addWidget(btn, i // 5, i % 5)
            self.formats_layout.addWidget(label)
            self.formats_layout.addWidget(grid_box)

    # ----- Conversion -----

    def start(self, category: str, fmt: str) -> None:
        waiting = [item for item in self.items.values() if item.category == category and not item.running]
        pages = None
        if fmt == formats.PDF_EXTRACT and waiting:
            pages = ask_page_range(QCursor.pos(), [item.path for item in waiting])
            if pages is None:
                return
        for item in waiting:
            item.running = True
            item.cancel.clear()
            self._set_status(item.uid, "Queued…")
            task = ConvertTask(item, fmt, pages)
            task.setAutoDelete(False)
            task.signals.progress.connect(self._on_progress)
            task.signals.finished.connect(self._on_finished)
            task.signals.failed.connect(self._on_failed)
            self.tasks[item.uid] = task
            self.pool.start(task)

    def _set_status(self, uid: int, text: str, tooltip: str = "") -> None:
        if row := self.rows.get(uid):
            row.setText(1, text)
            row.setToolTip(1, tooltip or text)

    def _on_progress(self, uid: int, value: float) -> None:
        self._set_status(uid, f"{int(value * 100)}%")

    def _on_finished(self, uid: int, output: str) -> None:
        self.tasks.pop(uid, None)
        if item := self.items.get(uid):
            item.running = False
            item.output = Path(output)
            self.last_output = item.output
            self._set_status(uid, f"✓ {item.output.name}", output)
            self.open_btn.setEnabled(True)

    def _on_failed(self, uid: int, message: str) -> None:
        self.tasks.pop(uid, None)
        if item := self.items.get(uid):
            item.running = False
            self._set_status(uid, "✗ Error", message)

    def _on_double_click(self, row: QTreeWidgetItem) -> None:
        for uid, r in self.rows.items():
            if r is row:
                item = self.items[uid]
                reveal_in_file_manager(item.output or item.path)

    # ----- Misc -----

    def set_on_top(self, on: bool) -> None:
        self.settings.setValue("onTop", on)
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if visible:  # changing the flag hides the window
            self.show()

    def closeEvent(self, event) -> None:
        # The app lives in the menu bar: only hide the window, conversions keep going
        event.ignore()
        self.hide()
        self.hidden.emit()

    def shutdown(self) -> None:
        for item in self.items.values():
            item.cancel.set()
        self.pool.waitForDone(3000)


class App(QApplication):
    """On macOS files dropped on the Dock icon arrive as a FileOpen event."""

    fileOpened = Signal(str)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.FileOpen:
            self.fileOpened.emit(event.file())
            return True
        return super().event(event)

