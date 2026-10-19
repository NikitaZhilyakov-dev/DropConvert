"""Всё, что зависит от ОС: выделенные файлы, автозапуск, значок в Dock."""
from __future__ import annotations

import ctypes
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEventLoop


class SelectionError(Exception):
    pass


# ---------- Выделенные файлы в Finder / Проводнике ----------

_FINDER_SCRIPT = """
tell application "Finder"
    if not frontmost then return ""
    set out to ""
    repeat with f in (get selection as alias list)
        set out to out & POSIX path of f & linefeed
    end repeat
    return out
end tell
"""


def selected_files() -> list[Path]:
    """Файлы, выделенные в активном окне файлового менеджера."""
    if sys.platform == "darwin":
        return _finder_selection()
    if sys.platform == "win32":
        return _explorer_selection()
    raise SelectionError("Эта ОС пока не поддерживается")


def _finder_selection() -> list[Path]:
    try:
        # Таймаут большой: при первом запуске macOS ждёт, пока пользователь ответит на запрос доступа
        result = subprocess.run(
            ["osascript", "-e", _FINDER_SCRIPT], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        raise SelectionError("Finder не ответил") from None
    if result.returncode != 0:
        if "-1743" in result.stderr:  # errAEEventNotPermitted
            raise SelectionError(
                "Нет доступа к Finder. Разрешите его в «Системные настройки → "
                "Конфиденциальность и безопасность → Автоматизация»"
            )
        raise SelectionError(result.stderr.strip() or "Не удалось получить выделение в Finder")
    return [Path(line) for line in result.stdout.splitlines() if line]


def _explorer_selection() -> list[Path]:
    import win32com.client
    import win32gui

    hwnd = win32gui.GetForegroundWindow()
    if win32gui.GetClassName(hwnd) not in ("CabinetWClass", "ExploreWClass"):
        return []
    for window in win32com.client.Dispatch("Shell.Application").Windows():
        try:
            if int(window.HWND) != hwnd:
                continue
            items = window.Document.SelectedItems()
            return [Path(items.Item(i).Path) for i in range(items.Count)]
        except Exception:  # окна Internet Explorer и прочие без Document
            continue
    return []


# ---------- Автозапуск при входе в систему ----------

_AGENT_LABEL = "com.dropconvert.agent"
_LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{_AGENT_LABEL}.plist"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--background"]
    main = Path(__file__).resolve().parent.parent / "main.py"
    return [sys.executable, str(main), "--background"]


def autostart_supported() -> bool:
    return sys.platform in ("darwin", "win32")


def autostart_enabled() -> bool:
    if sys.platform == "darwin":
        return _LAUNCH_AGENT.exists()
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, "DropConvert")
            return True
        except OSError:
            return False
    return False


def set_autostart(on: bool) -> None:
    if sys.platform == "darwin":
        if on:
            _LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
            with _LAUNCH_AGENT.open("wb") as f:
                plistlib.dump(
                    {"Label": _AGENT_LABEL, "ProgramArguments": _launch_command(), "RunAtLoad": True}, f
                )
        else:
            _LAUNCH_AGENT.unlink(missing_ok=True)
    elif sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                command = subprocess.list2cmdline(_launch_command())
                winreg.SetValueEx(key, "DropConvert", 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, "DropConvert")
                except FileNotFoundError:
                    pass


# ---------- macOS: значок в Dock и активация ----------

class _ObjC:
    """Минимальный вызов Objective-C через objc_msgSend, чтобы не тянуть pyobjc."""

    def __init__(self) -> None:
        self.lib = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.sel_registerName.argtypes = [ctypes.c_char_p]
        ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")

    def send(self, obj, selector: str, *args, argtypes=(), restype=ctypes.c_void_p):
        proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
        fn = proto(ctypes.cast(self.lib.objc_msgSend, ctypes.c_void_p).value)
        return fn(obj, self.lib.sel_registerName(selector.encode()), *args)

    def cls(self, name: str):
        return self.lib.objc_getClass(name.encode())


_objc: _ObjC | None = None


def _mac() -> _ObjC:
    global _objc
    if _objc is None:
        _objc = _ObjC()
    return _objc


def set_dock_icon_visible(visible: bool) -> None:
    """На macOS прячем значок из Dock, пока открыто только меню в строке меню."""
    if sys.platform != "darwin":
        return
    m = _mac()
    app = m.send(m.cls("NSApplication"), "sharedApplication")
    # NSApplicationActivationPolicyRegular = 0, Accessory = 1
    m.send(app, "setActivationPolicy:", 0 if visible else 1, argtypes=(ctypes.c_long,), restype=ctypes.c_bool)


def open_accessibility_settings() -> None:
    if sys.platform == "darwin":
        subprocess.Popen(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"]
        )


def activate_app() -> None:
    """Выводит DropConvert на передний план (Qt сам этого на macOS не делает для фоновых приложений)."""
    if sys.platform == "darwin":
        m = _mac()
        app = m.send(m.cls("NSApplication"), "sharedApplication")
        m.send(app, "activateIgnoringOtherApps:", True, argtypes=(ctypes.c_bool,), restype=None)


class FocusKeeper:
    """Даёт меню получить фокус и возвращает его приложению, которое было активно до этого.

    Без активации меню на macOS не реагирует на клавиатуру, а без возврата фокус
    остался бы у невидимого DropConvert, и Finder пришлось бы снова щёлкать мышью.
    """

    def __init__(self) -> None:
        self._previous = None

    def __enter__(self):
        if sys.platform == "darwin":
            m = _mac()
            workspace = m.send(m.cls("NSWorkspace"), "sharedWorkspace")
            self._previous = m.send(workspace, "frontmostApplication")
            if self._previous:
                m.send(self._previous, "retain")
            activate_app()
            # Активация асинхронная, а меню, открытое до её окончания, не получает клавиатуру
            app = m.send(m.cls("NSApplication"), "sharedApplication")
            deadline = time.monotonic() + 0.5
            while not m.send(app, "isActive", restype=ctypes.c_bool) and time.monotonic() < deadline:
                QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
                time.sleep(0.005)
        return self

    def __exit__(self, *exc) -> None:
        if self._previous:
            m = _mac()
            m.send(self._previous, "activateWithOptions:", 0, argtypes=(ctypes.c_ulong,), restype=ctypes.c_bool)
            m.send(self._previous, "release", restype=None)
            self._previous = None
