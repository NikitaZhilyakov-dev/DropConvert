"""Background utility: menu bar (tray) icon and an action wheel at the cursor.

The wheel opens with two gestures: ⌘ / Alt + right-click on selected files, or
Tab while dragging a file (the file is then dropped right onto a sector). After a
choice the wheel collapses into a progress ring at the cursor.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QPointF, QSettings, Qt, QThreadPool, QTimer
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import formats, system
from .app import ACCENT, App, ConvertTask, Item, MainWindow, expand, reveal_in_file_manager
from .converters import CANCELLED, TOOL_SUFFIXES
from .trigger import DRAG_GESTURE, GESTURE, MouseTrigger
from .wheel import FormatWheel, ProgressRing, WheelItem

FILE_MANAGER = "Finder" if sys.platform == "darwin" else "File Explorer"

# Extension aliases: a .jpeg file is already a JPG, no point converting it to JPG
_SAME_FORMAT = {"jpeg": "jpg", "jpe": "jpg", "tif": "tiff", "heif": "heic", "oga": "ogg", "aif": "aiff"}

# Hints for sectors whose meaning isn't obvious from the extension alone
_HINTS = {
    formats.VIDEO: {"gif": "animation", "mp3": "audio only", "wav": "audio only", "m4a": "audio only"},
    formats.IMAGE: {"pdf": "document", "ico": "icon"},
}

# Image tools: sector label, small hint, caption under the wheel
_TOOLS = {
    "compress": ("Compress", "smaller file", "Compress without changing the format"),
    "strip": ("Strip", "metadata", "Remove EXIF, location and other metadata"),
    "trim": ("Trim", "empty borders", "Trim solid-color or transparent borders"),
    "half": ("Shrink", "by half", "Halve the width and height"),
    "rotate": ("Rotate", "90°", "Rotate 90° clockwise"),
    "gray": ("B&W", "grayscale", "Convert to black and white"),
    "white_bg": ("Fill", "white", "Replace transparency with white"),
}
CONVERT_PAGE, TOOLS_PAGE = "Convert", "Tools"


def plural_files(n: int) -> str:
    return f"{n} file" if n == 1 else f"{n} files"


def tray_icon() -> QIcon:
    """Two back-and-forth arrows. On macOS it is a template image the system tints for the theme."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    template = sys.platform == "darwin"
    if not template:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(2, 2, size - 4, size - 4, 14, 14)
    p.setPen(QPen(QColor("black" if template else "white"), 6, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    path = QPainterPath()
    for y, x_from, x_to in ((22, 14, 50), (42, 50, 14)):
        head = 9 if x_to > x_from else -9
        path.moveTo(QPointF(x_from, y))
        path.lineTo(QPointF(x_to, y))
        path.moveTo(QPointF(x_to - head, y - 9))
        path.lineTo(QPointF(x_to, y))
        path.lineTo(QPointF(x_to - head, y + 9))
    p.drawPath(path)
    p.end()
    icon = QIcon(pixmap)
    icon.setIsMask(template)
    return icon


@dataclass
class Batch:
    """Files sent by one menu choice: they get a single notification."""

    label: str  # what was done: "→ PNG" or "(compressed)"
    total: int
    done: int = 0
    errors: list[str] = field(default_factory=list)
    last_output: Path | None = None


class WheelSession:
    """What the wheel shows for a set of files: pages, type choice, formats, tools."""

    def __init__(self, wheel: FormatWheel) -> None:
        self.wheel = wheel
        self.selected: list[Path] | None = None  # None: no files yet (the wheel waits for a drag)
        self.by_category: dict[str, list[Path]] = {}
        self.files: list[Path] = []
        self.pages = [CONVERT_PAGE]
        self.page = 0

    def set_files(self, selected: list[Path] | None) -> None:
        if selected == self.selected and selected is not None:
            return
        self.selected = selected
        self.by_category = {}
        for path in expand(selected or []):
            if category := formats.detect(path):
                self.by_category.setdefault(category, []).append(path)
        self.files = [p for paths in self.by_category.values() for p in paths]
        self.pages = [CONVERT_PAGE] + ([TOOLS_PAGE] if formats.IMAGE in self.by_category else [])
        self.page = 0
        self.show()

    def run(self, timeout_ms: int = 0) -> object:
        """Waits for a choice, switching pages and file types. Returns (category, format),
        ("tool", tool), "window" or None."""
        while True:
            data = self.wheel.choose(timeout_ms)
            if isinstance(data, tuple) and data[0] == "page":
                self.page = data[1]
                self.show()
            elif isinstance(data, tuple) and data[0] == "category":
                self._show_formats(data[1])
            else:
                return data

    def show(self) -> None:
        files = self.files
        self.wheel.title = (files[0].name if len(files) == 1 else plural_files(len(files)) if files
                            else "DropConvert")
        self.wheel.set_pages(self.pages, self.page)
        if not self.by_category:
            if self.selected is None:
                self.wheel.set_items([], "Hover", idle_caption="Drop a file on the wheel")
            elif not self.selected:
                self.wheel.set_items([], "Click a file", idle_caption=f"Select files in {FILE_MANAGER}")
            else:
                self.wheel.set_items([], "Unsupported", idle_caption="DropConvert can't convert these files")
        elif self.pages[self.page] == TOOLS_PAGE:
            self._show_tools()
        elif len(self.by_category) > 1:
            self._show_categories()
        else:
            self._show_formats(next(iter(self.by_category)))

    def _center_label(self, paths: list[Path]) -> str:
        sources = {_SAME_FORMAT.get(e, e) for e in (p.suffix.lower().lstrip(".") for p in paths)}
        return next(iter(sources)).upper() if len(sources) == 1 else plural_files(len(paths))

    def _show_categories(self) -> None:
        items = [
            WheelItem(formats.CATEGORY_NAMES[c], ("category", c), plural_files(len(self.by_category[c])),
                      f"{formats.CATEGORY_NAMES[c]}: choose a format", opens=True)
            for c in formats.CATEGORY_ORDER if c in self.by_category
        ]
        self.wheel.set_items(items, self.wheel.title, "window", "Open in the DropConvert window", "What to convert?")

    def _show_formats(self, category: str) -> None:
        paths = self.by_category[category]
        sources = {_SAME_FORMAT.get(e, e) for e in (p.suffix.lower().lstrip(".") for p in paths)}
        # Skip the sector if all files are already in that format
        targets = [f for f in formats.TARGETS[category] if sources != {f}]
        items = [
            WheelItem(fmt.upper(), (category, fmt), _HINTS.get(category, {}).get(fmt, ""),
                      f"Convert to {fmt.upper()}")
            for fmt in targets
        ]
        self.wheel.set_items(items, self._center_label(paths), "window", "Open in the DropConvert window",
                             "Choose a format")

    def _show_tools(self) -> None:
        images = self.by_category[formats.IMAGE]
        items = [WheelItem(label, ("tool", tool), hint, caption)
                 for tool, (label, hint, caption) in _TOOLS.items()]
        what = "the image" if len(images) == 1 else f"{len(images)} images"
        self.wheel.set_items(items, self._center_label(images), "window", "Open in the DropConvert window",
                             f"What to do with {what}?")


class Agent(QObject):
    def __init__(self, app: App) -> None:
        super().__init__()
        self.app = app
        self.settings = QSettings("DropConvert", "DropConvert")
        self.window: MainWindow | None = None
        self.batches: dict[int, Batch] = {}
        self.tasks: dict[int, tuple[ConvertTask, Item]] = {}
        self.progress: dict[int, float] = {}
        self.notified_output: Path | None = None
        self.menu_open = False
        self.drag_wheel: FormatWheel | None = None
        self.ring: ProgressRing | None = None
        self.ring_failed = False

        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(2, (os.cpu_count() or 2) // 2))

        self.trigger = MouseTrigger(self)
        self.trigger.activated.connect(lambda x, y: self.show_action_menu(x, y, allow_click=True))
        # Drag signals come from the tap thread (macOS) or from inside the hook (Windows)
        self.trigger.drag_key.connect(self.on_drag_key, Qt.ConnectionType.QueuedConnection)
        self.trigger.drag_ended.connect(self.on_drag_ended, Qt.ConnectionType.QueuedConnection)
        self.trigger.ready.connect(
            lambda: self.notify("DropConvert is ready", f"{GESTURE} on files in {FILE_MANAGER} opens the convert menu.")
        )

        self.tray = QSystemTrayIcon(tray_icon(), self)
        self.tray.setToolTip("DropConvert")
        self.tray_menu = QMenu()
        self.tray_menu.aboutToShow.connect(self._fill_tray_menu)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(
            lambda: self.notified_output and reveal_in_file_manager(self.notified_output)
        )
        self.tray.show()

        # Without Accessibility access macOS won't let us intercept the click; the system shows the prompt itself
        self.trigger.start()

    # ----- Menu bar / tray icon -----

    def gesture_hint(self) -> str:
        return f"{GESTURE} on files in {FILE_MANAGER}"

    def request_permission(self) -> None:
        self.trigger.ask_permission()
        system.open_accessibility_settings()

    def _fill_tray_menu(self) -> None:
        m = self.tray_menu
        m.clear()
        if self.tasks:
            m.addAction(f"Converting: {plural_files(len(self.tasks))} ({self._total_progress()}%)").setEnabled(False)
            m.addAction("Stop all", self.cancel_all)
            m.addSeparator()
        if self.trigger.active:
            m.addAction(self.gesture_hint()).setEnabled(False)
            m.addAction(DRAG_GESTURE).setEnabled(False)
        elif self.trigger.supported:
            m.addAction(f"{GESTURE} is off: no access").setEnabled(False)
            m.addAction("Allow Accessibility access…", self.request_permission)
        m.addAction("Open DropConvert window", self.show_window)
        m.addSeparator()
        if system.autostart_supported():
            autostart = QAction("Launch at login", m)
            autostart.setCheckable(True)
            autostart.setChecked(system.autostart_enabled())
            autostart.toggled.connect(self._set_autostart)
            m.addAction(autostart)
        m.addSeparator()
        m.addAction("Quit DropConvert", self.quit)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # On macOS clicking the icon already opens the menu; on Windows a click opens the window
        if sys.platform != "darwin" and reason in (
            QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick
        ):
            self.show_window()

    def _set_autostart(self, on: bool) -> None:
        try:
            system.set_autostart(on)
        except OSError as e:
            self.notify("Couldn't change launch at login", str(e), QSystemTrayIcon.MessageIcon.Warning)

    def notify(self, title: str, text: str,
               icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information) -> None:
        self.tray.showMessage(title, text, icon, 5000)

    # ----- Action menu at the cursor -----

    def show_action_menu(self, x: float | None = None, y: float | None = None,
                         allow_click: bool = False) -> None:
        if self.menu_open:
            return
        try:
            selected = system.selected_files()
        except system.SelectionError as e:
            self.notify("DropConvert", str(e), QSystemTrayIcon.MessageIcon.Warning)
            return
        except Exception as e:  # COM or AppleScript can fail in any way; the utility must keep running
            self.notify("DropConvert", f"Couldn't get the selected files: {e}",
                        QSystemTrayIcon.MessageIcon.Warning)
            return

        if not selected and allow_click and x is not None:
            # Nothing selected or the window was inactive: click the file under the cursor,
            # as Finder would on a plain right-click, and ask for the selection again
            self.trigger.click(x, y)
            QTimer.singleShot(250, lambda: self.show_action_menu(x, y))
            return

        wheel = FormatWheel("DropConvert")
        session = WheelSession(wheel)
        session.set_files(selected)
        self.menu_open = True
        try:
            with system.FocusKeeper():
                wheel.popup(QCursor.pos())
                data = session.run(timeout_ms=1800 if not session.by_category else 0)
                center = wheel.center_global()
                wheel.close_animated(collapse=self._is_job(data))
        finally:
            self.menu_open = False
        self._apply(session, data, center)

    # ----- Wheel while dragging -----

    def on_drag_key(self, step: int) -> None:
        """Tab while dragging: the first opens the wheel, the next ones switch pages."""
        if self.drag_wheel is not None:
            self.drag_wheel.switch_page(step)
        elif not self.menu_open:
            self.show_drag_wheel()

    def on_drag_ended(self) -> None:
        # The button is released, but the drop on the wheel arrives a bit later:
        # give it time, or we'd close before learning which sector it landed on
        if wheel := self.drag_wheel:
            QTimer.singleShot(400, wheel, wheel.cancel)  # with a context: the wheel may be gone by then

    def show_drag_wheel(self) -> None:
        wheel = FormatWheel("DropConvert")
        session = WheelSession(wheel)
        session.set_files(None)  # what is being dragged becomes known once the file is over the wheel
        wheel.accept_files(session.set_files)
        self.menu_open = True
        self.drag_wheel = wheel
        try:
            wheel.popup(QCursor.pos(), activate=False)  # don't take focus, or the drag gets interrupted
            data = session.run()
            center = wheel.center_global()
            wheel.close_animated(collapse=self._is_job(data))
        finally:
            self.menu_open = False
            self.drag_wheel = None
        self._apply(session, data, center)

    # ----- Wheel choice -----

    @staticmethod
    def _is_job(data: object) -> bool:
        return isinstance(data, tuple)

    def _apply(self, session: WheelSession, data: object, center: QPoint) -> None:
        if data == "window":
            self.show_window(session.files)
        elif isinstance(data, tuple) and data[0] == "tool":
            tool = data[1]
            self.convert(session.by_category[formats.IMAGE], formats.IMAGE, tool,
                         f"({TOOL_SUFFIXES[tool]})", center)
        elif isinstance(data, tuple):
            category, fmt = data
            self.convert(session.by_category[category], category, fmt, f"→ {fmt.upper()}", center)

    # ----- Conversion -----

    def convert(self, paths: list[Path], category: str, fmt: str, label: str,
                at: QPoint | None = None) -> None:
        """fmt is a format or a tool from formats.TOOLS; at is where the progress ring appears."""
        batch = Batch(label, len(paths))
        for path in paths:
            item = Item(path, category)
            task = ConvertTask(item, fmt)
            task.setAutoDelete(False)
            task.signals.progress.connect(self._on_progress)
            task.signals.finished.connect(self._on_finished)
            task.signals.failed.connect(self._on_failed)
            self.tasks[item.uid] = (task, item)
            self.batches[item.uid] = batch
            self.progress[item.uid] = 0.0
            self.pool.start(task)
        if self.ring is None:
            self.ring = ProgressRing()
        if not self.ring.running:
            self.ring_failed = False
        self.ring.start(at or QCursor.pos())
        self._update_progress()

    def _total_progress(self) -> int:
        return int(100 * sum(self.progress.values()) / len(self.progress)) if self.progress else 0

    def _update_progress(self) -> None:
        if self.ring is not None:
            if self.tasks:
                self.ring.set_value(self._total_progress() / 100)
            else:
                self.ring.finish(ok=not self.ring_failed)
        if self.tasks:
            self.tray.setToolTip(f"DropConvert: {plural_files(len(self.tasks))}, {self._total_progress()}%")
        else:
            self.tray.setToolTip("DropConvert")

    def _on_progress(self, uid: int, value: float) -> None:
        if uid in self.progress:
            self.progress[uid] = value
            self._update_progress()

    def _on_finished(self, uid: int, output: str) -> None:
        if batch := self._finish(uid):
            batch.last_output = Path(output)
            self._maybe_notify(batch)

    def _on_failed(self, uid: int, message: str) -> None:
        if message != CANCELLED:
            self.ring_failed = True
        if batch := self._finish(uid):
            if message != CANCELLED:
                batch.errors.append(message)
            self._maybe_notify(batch)

    def _finish(self, uid: int) -> Batch | None:
        self.tasks.pop(uid, None)
        self.progress.pop(uid, None)
        self._update_progress()
        batch = self.batches.pop(uid, None)
        if batch:
            batch.done += 1
        return batch

    def _maybe_notify(self, batch: Batch) -> None:
        if batch.done < batch.total:
            return
        ok = batch.total - len(batch.errors)
        if batch.errors:
            self.notify(
                f"Couldn't convert {len(batch.errors)} of {batch.total}",
                batch.errors[0], QSystemTrayIcon.MessageIcon.Warning,
            )
        elif ok:
            self.notified_output = batch.last_output
            name = batch.last_output.name if ok == 1 and batch.last_output else (
                f"{plural_files(ok)} {batch.label}")
            self.notify("Done", f"{name}\nClick to show in {FILE_MANAGER}")

    def cancel_all(self) -> None:
        for _, item in self.tasks.values():
            item.cancel.set()

    # ----- Window -----

    def show_window(self, files: list[Path] | None = None) -> None:
        if self.window is None:
            self.window = MainWindow()
            self.window.hidden.connect(self._on_window_hidden)
        if files:
            self.window.add_files(files)
        system.set_dock_icon_visible(True)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        system.activate_app()

    def _on_window_hidden(self) -> None:
        system.set_dock_icon_visible(False)
        if not self.settings.value("trayHintShown", False, type=bool):
            self.settings.setValue("trayHintShown", True)
            where = "in the menu bar" if sys.platform == "darwin" else "in the tray"
            hint = f"{self.gesture_hint()} opens the convert menu." if self.trigger.active else ""
            self.notify("DropConvert keeps running", f"The icon is {where}. {hint}".strip())

    def quit(self) -> None:
        self.cancel_all()
        if self.window is not None:
            self.window.shutdown()
        self.pool.waitForDone(3000)
        self.trigger.stop()
        self.tray.hide()
        self.app.quit()


# ---------- Single instance ----------

_SERVER_NAME = f"DropConvert-{getpass.getuser()}"


def forward_to_running(files: list[Path]) -> bool:
    """If DropConvert is already running, hand it the files and exit. True if handed over."""
    socket = QLocalSocket()
    socket.connectToServer(_SERVER_NAME)
    if not socket.waitForConnected(500):
        return False
    socket.write(json.dumps([str(f) for f in files]).encode())
    socket.waitForBytesWritten(1000)
    socket.disconnectFromServer()
    return True


def listen_for_instances(agent: Agent) -> QLocalServer:
    server = QLocalServer(agent)
    if not server.listen(_SERVER_NAME):
        QLocalServer.removeServer(_SERVER_NAME)  # left over from a crashed process
        server.listen(_SERVER_NAME)

    def on_connection() -> None:
        socket = server.nextPendingConnection()
        buffer = bytearray()

        def read() -> None:
            buffer.extend(socket.readAll().data())

        def done() -> None:
            read()
            try:
                files = [Path(f) for f in json.loads(buffer.decode() or "[]")]
            except ValueError:
                files = []
            agent.show_window(files)
            socket.deleteLater()

        socket.readyRead.connect(read)
        socket.disconnected.connect(done)

    server.newConnection.connect(on_connection)
    return server


def run(argv: list[str]) -> int:
    app = App(argv)
    app.setApplicationName("DropConvert")
    app.setQuitOnLastWindowClosed(False)

    background = "--background" in argv[1:]
    # Windows: files dropped on the .exe or a shortcut arrive as arguments
    files = [Path(a) for a in argv[1:] if not a.startswith("--") and Path(a).exists()]
    if forward_to_running(files):
        return 0

    agent = Agent(app)
    server = listen_for_instances(agent)  # noqa: F841 — lives as long as agent
    # macOS: files dropped on the Dock icon arrive as a FileOpen event
    app.fileOpened.connect(lambda f: agent.show_window([Path(f)]))

    if files or not background:
        agent.show_window(files)
    else:
        system.set_dock_icon_visible(False)
    return app.exec()
