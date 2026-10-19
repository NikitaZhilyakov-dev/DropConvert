"""Фоновая утилита: значок в строке меню (трее) и колесо действий у курсора.

Колесо открывается двумя жестами: ⌘ / Alt + правый щелчок по выделенным файлам или
Tab во время перетаскивания файла (тогда файл бросают прямо на сектор). После выбора
колесо сворачивается в кольцо прогресса у курсора.
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
from .converters import TOOL_SUFFIXES
from .trigger import DRAG_GESTURE, GESTURE, MouseTrigger
from .wheel import FormatWheel, ProgressRing, WheelItem

FILE_MANAGER = "Finder" if sys.platform == "darwin" else "Проводнике"

# Синонимы расширений: файл .jpeg уже «в JPG», конвертировать его в JPG незачем
_SAME_FORMAT = {"jpeg": "jpg", "jpe": "jpg", "tif": "tiff", "heif": "heic", "oga": "ogg", "aif": "aiff"}

# Подписи для секторов, смысл которых не очевиден по одному расширению
_HINTS = {
    formats.VIDEO: {"gif": "анимация", "mp3": "только звук", "wav": "только звук", "m4a": "только звук"},
    formats.IMAGE: {"pdf": "документ", "ico": "иконка"},
}

# Инструменты для картинок: подпись в секторе, мелкая подпись, подпись под колесом
_TOOLS = {
    "compress": ("Сжать", "меньше вес", "Сжать, не меняя формат"),
    "strip": ("Очистить", "метаданные", "Удалить EXIF, геопозицию и прочие метаданные"),
    "trim": ("Обрезать", "пустые поля", "Обрезать однотонные или прозрачные поля"),
    "half": ("Уменьшить", "вдвое", "Уменьшить вдвое по каждой стороне"),
    "rotate": ("Повернуть", "на 90°", "Повернуть на 90° по часовой стрелке"),
    "gray": ("Ч/Б", "без цвета", "Сделать чёрно-белой"),
    "white_bg": ("Фон", "белый", "Заменить прозрачность белым фоном"),
}
CONVERT_PAGE, TOOLS_PAGE = "Конвертация", "Инструменты"


def plural_files(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = "файл"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "файла"
    else:
        word = "файлов"
    return f"{n} {word}"


def tray_icon() -> QIcon:
    """Две стрелки «туда-обратно». На macOS это шаблон, который система сама красит под тему."""
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
    """Файлы, отправленные одним выбором в меню: по ним показываем одно уведомление."""

    label: str  # что сделали: «→ PNG» или «(сжато)»
    total: int
    done: int = 0
    errors: list[str] = field(default_factory=list)
    last_output: Path | None = None


class WheelSession:
    """Что показывает колесо для набора файлов: страницы, выбор типа, форматы, инструменты."""

    def __init__(self, wheel: FormatWheel) -> None:
        self.wheel = wheel
        self.selected: list[Path] | None = None  # None — файлов ещё нет (колесо ждёт перетаскивания)
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
        """Ждёт выбора, листая страницы и типы файлов. Возвращает (категория, формат),
        ("tool", инструмент), "window" или None."""
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
                self.wheel.set_items([], "Наведите", idle_caption="Перетащите файл на колесо")
            elif not self.selected:
                self.wheel.set_items([], "Щёлкните по файлу", idle_caption=f"Выделите файлы в {FILE_MANAGER}")
            else:
                self.wheel.set_items([], "Не поддерживается", idle_caption="Эти файлы DropConvert не конвертирует")
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
                      f"{formats.CATEGORY_NAMES[c]}: выбрать формат", opens=True)
            for c in formats.CATEGORY_ORDER if c in self.by_category
        ]
        self.wheel.set_items(items, self.wheel.title, "window", "Открыть в окне DropConvert", "Что конвертируем?")

    def _show_formats(self, category: str) -> None:
        paths = self.by_category[category]
        sources = {_SAME_FORMAT.get(e, e) for e in (p.suffix.lower().lstrip(".") for p in paths)}
        # Если все файлы уже в этом формате, сектор лишний
        targets = [f for f in formats.TARGETS[category] if sources != {f}]
        items = [
            WheelItem(fmt.upper(), (category, fmt), _HINTS.get(category, {}).get(fmt, ""),
                      f"Конвертировать в {fmt.upper()}")
            for fmt in targets
        ]
        self.wheel.set_items(items, self._center_label(paths), "window", "Открыть в окне DropConvert",
                             "Выберите формат")

    def _show_tools(self) -> None:
        images = self.by_category[formats.IMAGE]
        items = [WheelItem(label, ("tool", tool), hint, caption)
                 for tool, (label, hint, caption) in _TOOLS.items()]
        what = "с картинкой" if len(images) == 1 else f"с картинками ({len(images)})"
        self.wheel.set_items(items, self._center_label(images), "window", "Открыть в окне DropConvert",
                             f"Что сделать {what}?")


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
        # Сигналы перетаскивания приходят из потока перехватчика (macOS) или изнутри хука (Windows)
        self.trigger.drag_key.connect(self.on_drag_key, Qt.ConnectionType.QueuedConnection)
        self.trigger.drag_ended.connect(self.on_drag_ended, Qt.ConnectionType.QueuedConnection)
        self.trigger.ready.connect(
            lambda: self.notify("DropConvert готов", f"{GESTURE} по файлам в {FILE_MANAGER} открывает меню конвертации.")
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

        # Без «Универсального доступа» macOS не даст перехватить щелчок; запрос покажет сама система
        self.trigger.start()

    # ----- Значок в строке меню / трее -----

    def gesture_hint(self) -> str:
        return f"{GESTURE} по файлам в {FILE_MANAGER}"

    def request_permission(self) -> None:
        self.trigger.ask_permission()
        system.open_accessibility_settings()

    def _fill_tray_menu(self) -> None:
        m = self.tray_menu
        m.clear()
        if self.tasks:
            m.addAction(f"Конвертируется: {plural_files(len(self.tasks))} ({self._total_progress()}%)").setEnabled(False)
            m.addAction("Остановить все", self.cancel_all)
            m.addSeparator()
        if self.trigger.active:
            m.addAction(self.gesture_hint()).setEnabled(False)
            m.addAction(DRAG_GESTURE).setEnabled(False)
        elif self.trigger.supported:
            m.addAction(f"{GESTURE} не работает: нет доступа").setEnabled(False)
            m.addAction("Разрешить «Универсальный доступ»…", self.request_permission)
        m.addAction("Открыть окно DropConvert", self.show_window)
        m.addSeparator()
        if system.autostart_supported():
            autostart = QAction("Запускать при входе в систему", m)
            autostart.setCheckable(True)
            autostart.setChecked(system.autostart_enabled())
            autostart.toggled.connect(self._set_autostart)
            m.addAction(autostart)
        m.addSeparator()
        m.addAction("Выйти из DropConvert", self.quit)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # На macOS щелчок по значку и так открывает меню; на Windows по щелчку открываем окно
        if sys.platform != "darwin" and reason in (
            QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick
        ):
            self.show_window()

    def _set_autostart(self, on: bool) -> None:
        try:
            system.set_autostart(on)
        except OSError as e:
            self.notify("Не удалось изменить автозапуск", str(e), QSystemTrayIcon.MessageIcon.Warning)

    def notify(self, title: str, text: str,
               icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information) -> None:
        self.tray.showMessage(title, text, icon, 5000)

    # ----- Меню действий у курсора -----

    def show_action_menu(self, x: float | None = None, y: float | None = None,
                         allow_click: bool = False) -> None:
        if self.menu_open:
            return
        try:
            selected = system.selected_files()
        except system.SelectionError as e:
            self.notify("DropConvert", str(e), QSystemTrayIcon.MessageIcon.Warning)
            return
        except Exception as e:  # COM или AppleScript могут упасть как угодно; утилита должна жить
            self.notify("DropConvert", f"Не удалось получить выделенные файлы: {e}",
                        QSystemTrayIcon.MessageIcon.Warning)
            return

        if not selected and allow_click and x is not None:
            # Ничего не выделено или окно было неактивно: щёлкаем по файлу под курсором,
            # как сделал бы Finder при обычном правом щелчке, и спрашиваем выделение снова
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

    # ----- Колесо при перетаскивании -----

    def on_drag_key(self, step: int) -> None:
        """Tab во время перетаскивания: первый открывает колесо, следующие листают страницы."""
        if self.drag_wheel is not None:
            self.drag_wheel.switch_page(step)
        elif not self.menu_open:
            self.show_drag_wheel()

    def on_drag_ended(self) -> None:
        # Кнопку отпустили, но бросок на колесо приходит чуть позже отпускания:
        # даём ему время, иначе закроемся раньше, чем узнаем, на какой сектор бросили
        if wheel := self.drag_wheel:
            QTimer.singleShot(400, wheel, wheel.cancel)  # с контекстом: колесо к тому времени может исчезнуть

    def show_drag_wheel(self) -> None:
        wheel = FormatWheel("DropConvert")
        session = WheelSession(wheel)
        session.set_files(None)  # что тащат, станет известно, когда файл окажется над колесом
        wheel.accept_files(session.set_files)
        self.menu_open = True
        self.drag_wheel = wheel
        try:
            wheel.popup(QCursor.pos(), activate=False)  # фокус не трогаем, иначе прервём перетаскивание
            data = session.run()
            center = wheel.center_global()
            wheel.close_animated(collapse=self._is_job(data))
        finally:
            self.menu_open = False
            self.drag_wheel = None
        self._apply(session, data, center)

    # ----- Выбор в колесе -----

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

    # ----- Конвертация -----

    def convert(self, paths: list[Path], category: str, fmt: str, label: str,
                at: QPoint | None = None) -> None:
        """fmt — формат или инструмент из formats.TOOLS; at — где появится кольцо прогресса."""
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
        if message != "Отменено":
            self.ring_failed = True
        if batch := self._finish(uid):
            if message != "Отменено":
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
                f"Не удалось сконвертировать {len(batch.errors)} из {batch.total}",
                batch.errors[0], QSystemTrayIcon.MessageIcon.Warning,
            )
        elif ok:
            self.notified_output = batch.last_output
            name = batch.last_output.name if ok == 1 and batch.last_output else (
                f"{plural_files(ok)} {batch.label}")
            self.notify("Готово", f"{name}\nНажмите, чтобы показать в {FILE_MANAGER}")

    def cancel_all(self) -> None:
        for _, item in self.tasks.values():
            item.cancel.set()

    # ----- Окно -----

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
            where = "в строке меню" if sys.platform == "darwin" else "в трее"
            hint = f"{self.gesture_hint()} открывает меню конвертации." if self.trigger.active else ""
            self.notify("DropConvert продолжает работать", f"Значок {where}. {hint}".strip())

    def quit(self) -> None:
        self.cancel_all()
        if self.window is not None:
            self.window.shutdown()
        self.pool.waitForDone(3000)
        self.trigger.stop()
        self.tray.hide()
        self.app.quit()


# ---------- Один экземпляр ----------

_SERVER_NAME = f"DropConvert-{getpass.getuser()}"


def forward_to_running(files: list[Path]) -> bool:
    """Если DropConvert уже запущен, передаём ему файлы и выходим. True — передали."""
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
        QLocalServer.removeServer(_SERVER_NAME)  # остался от упавшего процесса
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
    # Windows: файлы, перетащенные на .exe или ярлык, приходят аргументами
    files = [Path(a) for a in argv[1:] if not a.startswith("--") and Path(a).exists()]
    if forward_to_running(files):
        return 0

    agent = Agent(app)
    server = listen_for_instances(agent)  # noqa: F841 — живёт, пока живёт agent
    # macOS: файлы, брошенные на иконку в Dock, приходят событием FileOpen
    app.fileOpened.connect(lambda f: agent.show_window([Path(f)]))

    if files or not background:
        agent.show_window(files)
    else:
        system.set_dock_icon_visible(False)
    return app.exec()
