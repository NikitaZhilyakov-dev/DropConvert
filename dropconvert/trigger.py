"""Opening the menu with a gesture: ⌘ + right-click on macOS, Alt + right-click on Windows.

The click is intercepted before Finder or File Explorer gets it, so their own
context menu doesn't open. On macOS this needs Accessibility access: without it
the system doesn't allow intercepting mouse events.

The second gesture is Tab while dragging: while the left button is held and the
mouse has moved, Tab doesn't reach applications but opens the wheel (and switches
its pages). A released button or Esc means the drag is over.
"""
from __future__ import annotations

import ctypes
import sys
import threading

from PySide6.QtCore import QObject, QTimer, Signal

GESTURE = "⌘ + right-click" if sys.platform == "darwin" else "Alt + right-click"
DRAG_GESTURE = "Tab while dragging a file"
_DRAG_THRESHOLD = 6  # how many pixels the mouse must move to count as a drag


class MouseTrigger(QObject):
    # Click coordinates in OS coordinates; used to click the same spot when nothing is selected
    activated = Signal(float, float)
    ready = Signal()  # interception started working after the user granted permission
    # Drag signals come from the tap thread: connect with QueuedConnection
    drag_key = Signal(int)  # Tab (+1) or Shift+Tab (-1) while dragging
    drag_ended = Signal()   # button released or Esc pressed

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if sys.platform == "darwin":
            self._backend = _MacTap(self._fire, self.drag_key.emit, self.drag_ended.emit)
        elif sys.platform == "win32":
            self._backend = _WinHook(self._fire, self.drag_key.emit, self.drag_ended.emit)
        else:
            self._backend = None
        # Permission can be granted at any moment, so keep trying to start until it works
        self._retry = QTimer(self, interval=2000, timeout=self._try_start)

    @property
    def supported(self) -> bool:
        return self._backend is not None

    @property
    def active(self) -> bool:
        return self._backend is not None and self._backend.active

    def start(self, ask_permission: bool = True) -> bool:
        if self._backend is None:
            return False
        if ask_permission:
            self._backend.ask_permission()
        if not self._try_start():
            self._retry.start()
        return self.active

    def _try_start(self) -> bool:
        if self._backend.start():
            if self._retry.isActive():
                self._retry.stop()
                self.ready.emit()
            return True
        return False

    def stop(self) -> None:
        self._retry.stop()
        if self._backend is not None:
            self._backend.stop()

    def ask_permission(self) -> None:
        if self._backend is not None:
            self._backend.ask_permission()

    def click(self, x: float, y: float) -> None:
        """A plain left click at the gesture point to select the file under the cursor."""
        if self._backend is not None:
            self._backend.click(x, y)

    def _fire(self, x: float, y: float) -> None:
        # The tap must return right away or the system disables it
        # (and on Windows the mouse stalls), so the menu is shown afterwards
        QTimer.singleShot(0, lambda: self.activated.emit(x, y))


# ---------- macOS ----------

class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_RIGHT_DOWN, _RIGHT_UP, _LEFT_DOWN, _LEFT_UP, _LEFT_DRAGGED, _KEY_DOWN = 3, 4, 1, 2, 6, 10
_TAP_DISABLED = (0xFFFFFFFE, 0xFFFFFFFF)  # by timeout and by the user
_FLAG_SHIFT, _FLAG_CONTROL, _FLAG_OPTION, _FLAG_COMMAND = 0x20000, 0x40000, 0x80000, 0x100000
_FIELD_AUTOREPEAT, _FIELD_KEYCODE = 8, 9
_KEY_TAB, _KEY_ESCAPE = 48, 53


class _MacTap:
    _CALLBACK = ctypes.CFUNCTYPE(
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p
    )

    def __init__(self, callback, on_drag_key, on_drag_end) -> None:
        cg = self._cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        cf = self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg.CGEventTapCreate.restype = ctypes.c_void_p
        cg.CGEventTapCreate.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64, self._CALLBACK, ctypes.c_void_p,
        ]
        cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        cg.CGEventGetFlags.restype = ctypes.c_uint64
        cg.CGEventGetFlags.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = _CGPoint
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        cg.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
        cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        cf.CFRunLoopGetMain.restype = ctypes.c_void_p
        cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRunLoopRemoveSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFMachPortInvalidate.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
        cg.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]

        self._callback = callback
        self._swallow_up = False
        self._tap = None
        self._source = None
        self._handler = self._CALLBACK(self._on_event)  # keep a reference, or the garbage collector frees it

        # Dragging is watched by a separate tap on its own thread: every key press in the system
        # goes through it, and a busy Qt main thread must not delay them
        self._on_drag_key, self._on_drag_end = on_drag_key, on_drag_end
        self._drag_handler = self._CALLBACK(self._on_drag_event)
        self._drag_thread: threading.Thread | None = None
        self._drag_tap = None
        self._drag_loop = None
        self._down: tuple[float, float] | None = None
        self._dragging = False

    @property
    def active(self) -> bool:
        return self._tap is not None

    def ask_permission(self) -> None:
        """Shows the system Accessibility prompt if access isn't granted yet."""
        cf = self._cf
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
        ]
        key = ctypes.c_void_p.in_dll(self._cg, "kAXTrustedCheckOptionPrompt")
        true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        keys, values = (ctypes.c_void_p * 1)(key), (ctypes.c_void_p * 1)(true)
        options = cf.CFDictionaryCreate(
            None, keys, values, 1,
            ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
            ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks")),
        )
        self._cg.AXIsProcessTrustedWithOptions(options)
        cf.CFRelease(options)

    def start(self) -> bool:
        if self._tap:
            return True
        mask = (1 << _RIGHT_DOWN) | (1 << _RIGHT_UP)
        # kCGSessionEventTap = 1, kCGHeadInsertEventTap = 0, kCGEventTapOptionDefault = 0 (can swallow events)
        tap = self._cg.CGEventTapCreate(1, 0, 0, mask, self._handler, None)
        if not tap:  # no Accessibility access
            return False
        self._tap = tap
        self._source = self._cf.CFMachPortCreateRunLoopSource(None, tap, 0)
        modes = ctypes.c_void_p.in_dll(self._cf, "kCFRunLoopCommonModes")
        self._cf.CFRunLoopAddSource(self._cf.CFRunLoopGetMain(), self._source, modes)
        self._cg.CGEventTapEnable(tap, True)
        self._drag_thread = threading.Thread(target=self._run_drag_tap, name="DropConvert drag tap", daemon=True)
        self._drag_thread.start()
        return True

    def _run_drag_tap(self) -> None:
        cg, cf = self._cg, self._cf
        mask = (1 << _LEFT_DOWN) | (1 << _LEFT_UP) | (1 << _LEFT_DRAGGED) | (1 << _KEY_DOWN)
        tap = cg.CGEventTapCreate(1, 0, 0, mask, self._drag_handler, None)
        if not tap:
            return
        source = cf.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._drag_loop = cf.CFRunLoopGetCurrent()
        cf.CFRunLoopAddSource(self._drag_loop, source, ctypes.c_void_p.in_dll(cf, "kCFRunLoopCommonModes"))
        self._drag_tap = tap
        cg.CGEventTapEnable(tap, True)
        cf.CFRunLoopRun()  # until CFRunLoopStop from stop()
        cg.CGEventTapEnable(tap, False)
        cf.CFMachPortInvalidate(tap)
        cf.CFRelease(source)
        cf.CFRelease(tap)
        self._drag_tap = None

    def _on_drag_event(self, _proxy, event_type, event, _data):
        cg = self._cg
        if event_type in _TAP_DISABLED:
            if self._drag_tap:
                cg.CGEventTapEnable(self._drag_tap, True)
            return event
        if event_type == _LEFT_DOWN:
            point = cg.CGEventGetLocation(event)
            self._down, self._dragging = (point.x, point.y), False
        elif event_type == _LEFT_DRAGGED:
            if self._down and not self._dragging:
                point = cg.CGEventGetLocation(event)
                dx, dy = point.x - self._down[0], point.y - self._down[1]
                self._dragging = dx * dx + dy * dy > _DRAG_THRESHOLD ** 2
        elif event_type == _LEFT_UP:
            if self._dragging:
                self._on_drag_end()
            self._down, self._dragging = None, False
        elif event_type == _KEY_DOWN and self._dragging:
            code = cg.CGEventGetIntegerValueField(event, _FIELD_KEYCODE)
            flags = cg.CGEventGetFlags(event)
            if code == _KEY_TAB and not flags & (_FLAG_COMMAND | _FLAG_CONTROL | _FLAG_OPTION):
                if not cg.CGEventGetIntegerValueField(event, _FIELD_AUTOREPEAT):
                    self._on_drag_key(-1 if flags & _FLAG_SHIFT else 1)
                return None  # the app under the cursor won't see the Tab
            if code == _KEY_ESCAPE:  # the system cancels the drag itself
                self._dragging = False
                self._on_drag_end()
        return event

    def stop(self) -> None:
        if self._drag_thread is not None:
            if self._drag_loop:
                self._cf.CFRunLoopStop(self._drag_loop)
            self._drag_thread.join(1)
            self._drag_thread = self._drag_loop = None
        if not self._tap:
            return
        self._cg.CGEventTapEnable(self._tap, False)
        modes = ctypes.c_void_p.in_dll(self._cf, "kCFRunLoopCommonModes")
        self._cf.CFRunLoopRemoveSource(self._cf.CFRunLoopGetMain(), self._source, modes)
        self._cf.CFMachPortInvalidate(self._tap)
        self._cf.CFRelease(self._source)
        self._cf.CFRelease(self._tap)
        self._tap = self._source = None

    def _on_event(self, _proxy, event_type, event, _data):
        if event_type in _TAP_DISABLED:
            # The system disables a tap that took too long; turn it back on
            self._cg.CGEventTapEnable(self._tap, True)
            return event
        if event_type == _RIGHT_DOWN:
            flags = self._cg.CGEventGetFlags(event)
            if flags & _FLAG_COMMAND and not flags & (_FLAG_CONTROL | _FLAG_OPTION | _FLAG_SHIFT):
                point = self._cg.CGEventGetLocation(event)
                self._swallow_up = True
                self._callback(point.x, point.y)
                return None  # Finder won't see the click
        elif event_type == _RIGHT_UP and self._swallow_up:
            self._swallow_up = False
            return None
        return event

    def click(self, x: float, y: float) -> None:
        """A plain left click at the point: selects the file under the cursor and activates its window."""
        cg = self._cg
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        for kind in (_LEFT_DOWN, _LEFT_UP):
            ev = cg.CGEventCreateMouseEvent(None, kind, _CGPoint(x, y), 0)
            cg.CGEventSetFlags(ev, 0)  # ⌘ is still physically held; with it the click would change the selection
            cg.CGEventPost(0, ev)  # kCGHIDEventTap
            self._cf.CFRelease(ev)


# ---------- Windows ----------

class _WinHook:
    WH_MOUSE_LL, WH_KEYBOARD_LL = 14, 13
    WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
    WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
    WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
    VK_MENU, VK_CONTROL, VK_SHIFT, VK_TAB, VK_ESCAPE = 0x12, 0x11, 0x10, 0x09, 0x1B
    LLMHF_INJECTED = 0x01

    def __init__(self, callback, on_drag_key, on_drag_end) -> None:
        from ctypes import wintypes

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        self._struct = MSLLHOOKSTRUCT
        self._key_struct = KBDLLHOOKSTRUCT
        self._proto = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u = self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, self._proto, wintypes.HINSTANCE, wintypes.DWORD]
        u.CallNextHookEx.restype = ctypes.c_ssize_t
        u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
        u.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
        self._kernel32 = ctypes.WinDLL("kernel32")
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        self._callback = callback
        self._on_drag_key, self._on_drag_end = on_drag_key, on_drag_end
        self._swallow_up = False
        self._hook = self._key_hook = None
        self._handler = self._proto(self._on_event)
        self._key_handler = self._proto(self._on_key)
        self._down: tuple[int, int] | None = None
        self._dragging = False
        self._tab_held = False

    @property
    def active(self) -> bool:
        return self._hook is not None

    def ask_permission(self) -> None:
        pass  # Windows needs no permission

    def start(self) -> bool:
        module = self._kernel32.GetModuleHandleW(None)
        if not self._hook:
            self._hook = self._user32.SetWindowsHookExW(self.WH_MOUSE_LL, self._handler, module, 0) or None
        if not self._key_hook:
            self._key_hook = self._user32.SetWindowsHookExW(
                self.WH_KEYBOARD_LL, self._key_handler, module, 0) or None
        return self._hook is not None

    def stop(self) -> None:
        for hook in (self._hook, self._key_hook):
            if hook:
                self._user32.UnhookWindowsHookEx(hook)
        self._hook = self._key_hook = None

    def _pressed(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _track_drag(self, wparam, info) -> None:
        if wparam == self.WM_LBUTTONDOWN:
            self._down, self._dragging, self._tab_held = (info.pt.x, info.pt.y), False, False
        elif wparam == self.WM_MOUSEMOVE and self._down and not self._dragging:
            dx, dy = info.pt.x - self._down[0], info.pt.y - self._down[1]
            self._dragging = dx * dx + dy * dy > _DRAG_THRESHOLD ** 2
        elif wparam == self.WM_LBUTTONUP:
            if self._dragging:
                self._on_drag_end()
            self._down, self._dragging = None, False

    def _on_key(self, code, wparam, lparam):
        if code >= 0 and self._dragging:
            info = self._key_struct.from_address(lparam)
            down = wparam in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
            if info.vkCode == self.VK_TAB and not self._pressed(self.VK_MENU) \
                    and not self._pressed(self.VK_CONTROL):
                if down and not self._tab_held:  # skip autorepeat
                    self._on_drag_key(-1 if self._pressed(self.VK_SHIFT) else 1)
                self._tab_held = down
                return 1  # the window under the cursor won't see the Tab
            if info.vkCode == self.VK_ESCAPE and down:  # the system cancels the drag itself
                self._dragging = False
                self._on_drag_end()
        return self._user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_event(self, code, wparam, lparam):
        if code >= 0:
            info = self._struct.from_address(lparam)
            if wparam in (self.WM_LBUTTONDOWN, self.WM_LBUTTONUP, self.WM_MOUSEMOVE):
                self._track_drag(wparam, info)
            elif not info.flags & self.LLMHF_INJECTED:
                if wparam == self.WM_RBUTTONDOWN and self._pressed(self.VK_MENU) \
                        and not self._pressed(self.VK_CONTROL) and not self._pressed(self.VK_SHIFT):
                    self._swallow_up = True
                    self._callback(float(info.pt.x), float(info.pt.y))
                    return 1
                if wparam == self.WM_RBUTTONUP and self._swallow_up:
                    self._swallow_up = False
                    # Windows treats Alt released after the click as a lone press and
                    # activates the window menu; the unassigned key 0xE8 prevents that
                    self._user32.keybd_event(0xE8, 0, 0, 0)
                    self._user32.keybd_event(0xE8, 0, 2, 0)  # KEYEVENTF_KEYUP
                    return 1
        return self._user32.CallNextHookEx(None, code, wparam, lparam)

    def click(self, x: float, y: float) -> None:
        # The cursor is already at the click point; MOUSEEVENTF_LEFTDOWN / LEFTUP
        self._user32.mouse_event(0x0002, 0, 0, 0, 0)
        self._user32.mouse_event(0x0004, 0, 0, 0, 0)
