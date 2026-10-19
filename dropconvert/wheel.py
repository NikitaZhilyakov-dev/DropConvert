"""Format wheel: pops up at the cursor instead of a regular menu.

Petal sectors around a circle; the hovered one lights up and moves outward slightly,
with a pill showing the format name in the center. Works with the mouse or keyboard
(arrows, Enter, digits, Esc). The wheel can have several pages
("Convert", "Tools"): Tab cycles through them.

The wheel accepts dragged files: they are dropped right onto a sector.
This module also has the progress ring the wheel collapses into after a choice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QElapsedTimer, QEvent, QEventLoop, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPainterPathStroker, QPen,
    QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import QWidget

CENTER = "center"


@dataclass
class WheelItem:
    label: str
    data: Any
    hint: str = ""     # small text under the label in the sector
    caption: str = ""  # caption under the wheel on hover
    opens: bool = False  # opens the next set of sectors: springs open by itself while dragging


@dataclass(frozen=True)
class _Theme:
    disc: QColor
    disc_edge: QColor
    petal: QColor
    petal_shade: QColor
    petal_edge: QColor
    text: QColor
    muted: QColor
    hub: QColor
    pill: QColor
    accent: QColor
    accent_text: QColor


_LIGHT = _Theme(
    disc=QColor("#ECECEE"), disc_edge=QColor("#DCDCE0"), petal=QColor("#FFFFFF"),
    petal_shade=QColor("#EEEEF0"), petal_edge=QColor("#D6D6DA"), text=QColor("#141414"),
    muted=QColor("#6E6E73"), hub=QColor("#F7F7F8"), pill=QColor("#FFFFFF"),
    accent=QColor("#EC5F2C"), accent_text=QColor("#FFFFFF"),
)
_DARK = _Theme(
    disc=QColor("#262628"), disc_edge=QColor("#38383B"), petal=QColor("#3A3A3D"),
    petal_shade=QColor("#2F2F32"), petal_edge=QColor("#47474B"), text=QColor("#F2F2F4"),
    muted=QColor("#A1A1A6"), hub=QColor("#1E1E20"), pill=QColor("#323235"),
    accent=QColor("#EC5F2C"), accent_text=QColor("#FFFFFF"),
)
_ERROR = QColor("#E5484D")


def _theme() -> _Theme:
    dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    return _DARK if dark else _LIGHT


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor.fromRgbF(*(x + (y - x) * t for x, y in zip(a.getRgbF(), b.getRgbF())))


def _ease_in_cubic(t: float) -> float:
    return t * t * t


def _ease_out_back(t: float) -> float:
    c = 1.4
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


class FormatWheel(QWidget):
    R_OUT = 150      # outer petal radius
    R_IN = 70        # inner petal radius
    RIM = 12         # gray rim around the petals
    SHADOW = 26
    CAPTION = 44     # room for the captions above and below
    TABS = 40        # page strip under the bottom caption
    GAP = 6          # gap between petals
    CORNER = 13      # petal corner radius
    POP = 7          # how far the hovered petal moves outward
    APPEAR_MS, CLOSE_MS, COLLAPSE_MS = 230, 140, 200
    SPRING_MS = 500  # how long to hold a file over an opens sector for it to spring open
    COLLAPSED = 0.16  # how small the wheel shrinks before turning into the progress ring

    def __init__(self, title: str) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Otherwise macOS hides an inactive app's panel window (the wheel while dragging)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        side = 2 * (self.R_OUT + self.RIM + self.SHADOW)
        self.resize(side, side + 2 * self.CAPTION + self.TABS)

        self.title = title
        self.items: list[WheelItem] = []
        self.center_label = ""
        self.center_data: Any = None
        self.center_caption = ""
        self.idle_caption = ""
        self.hover: int | str | None = None
        self.chosen: int | str | None = None
        self.glow: dict[int | str, float] = {}
        self._paths: list[QPainterPath] = []
        self.pages: list[str] = []
        self.page = 0
        self._page_rects: list[QRectF] = []
        self._on_files: Callable[[list[Path]], None] | None = None

        self._appear = QElapsedTimer()
        self._close = QElapsedTimer()
        self._closing = False
        self._collapse = False
        self._loop: QEventLoop | None = None
        self._result: Any = None
        self._done = False
        self._was_active = False
        self._timer = QTimer(self, interval=16, timeout=self._tick)
        self._spring = QTimer(self, singleShot=True, interval=self.SPRING_MS, timeout=self._spring_open)

    # ----- Public interface -----

    def set_items(self, items: list[WheelItem], center_label: str, center_data: Any = None,
                  center_caption: str = "", idle_caption: str = "") -> None:
        """A new set of sectors (e.g. after choosing a category), replaying the appear animation."""
        self.items = items
        self.center_label, self.center_data = center_label, center_data
        self.center_caption, self.idle_caption = center_caption, idle_caption
        self.hover = self.chosen = None
        self.glow = {}
        self._paths = [self._petal_path(i, len(items)) for i in range(len(items))]
        self._appear.start()
        self._timer.start()
        self.update()

    def set_pages(self, pages: list[str], page: int) -> None:
        """Page names for the strip at the bottom; with a single page there is no strip."""
        self.pages, self.page = pages, page
        self.update()

    def switch_page(self, step: int) -> None:
        """Switches pages: choose() returns ("page", index), the caller sets the new sectors."""
        if len(self.pages) > 1 and not self._closing:
            self._finish(("page", (self.page + step) % len(self.pages)))

    def accept_files(self, on_files: Callable[[list[Path]], None]) -> None:
        """The wheel accepts dragged files; on_files learns what is dragged before the drop."""
        self._on_files = on_files
        self.setAcceptDrops(True)

    def cancel(self) -> None:
        self._finish(None)

    def center_global(self) -> QPoint:
        return self.mapToGlobal(self._center().toPoint())

    def popup(self, pos: QPoint, activate: bool = True) -> None:
        """Shows the wheel centered on pos, keeping it within the screen.

        Without activate the window doesn't take focus: that's how it opens mid-drag.
        """
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(pos.x() - self.width() // 2, area.left()), area.right() - self.width())
        y = min(max(pos.y() - self._center().y(), area.top()), area.bottom() - self.height())
        self.move(int(x), int(y))
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        self.show()
        self.raise_()
        if activate:
            self.activateWindow()
            self.setFocus()
        self._update_hover(self.mapFromGlobal(QCursor.pos()))

    def choose(self, timeout_ms: int = 0) -> Any:
        """Waits for a choice. Returns the chosen sector's data, or None if the wheel was closed."""
        self._result = None
        self._done = False
        self._loop = QEventLoop()
        if timeout_ms:
            QTimer.singleShot(timeout_ms, lambda: self._finish(None))
        self._loop.exec()
        self._loop = None
        return self._result

    def close_animated(self, collapse: bool = False) -> None:
        """collapse: the wheel shrinks to the point where the progress ring will appear."""
        self._closing = True
        self._collapse = collapse
        self._close.start()
        loop = QEventLoop()
        QTimer.singleShot(self._close_ms() + 20, loop.quit)
        loop.exec()
        self._timer.stop()
        self.close()

    # ----- Geometry -----

    def _close_ms(self) -> int:
        return self.COLLAPSE_MS if self._collapse else self.CLOSE_MS

    def _center(self) -> QPointF:
        return QPointF(self.width() / 2, self.CAPTION + (self.width()) / 2)

    def _angles(self, i: int, n: int) -> tuple[float, float]:
        """Sector middle and width in degrees; sector 0 is at the top, then clockwise."""
        span = 360 / n
        return -90 + i * span, span

    def _petal_path(self, i: int, n: int) -> QPainterPath:
        """An annular sector with rounded corners (in coordinates with the wheel center at zero).

        Build the sector shrunk by the corner radius and stroke it with a round pen:
        the union gives an even gap and soft corners without hand-made arc geometry.
        """
        mid, span = self._angles(i, n)
        c = self.CORNER
        r0, r1 = self.R_IN + c, self.R_OUT - c
        half = span / 2
        d0 = min(math.degrees((self.GAP / 2 + c) / r0), half - 1)
        d1 = min(math.degrees((self.GAP / 2 + c) / r1), half - 1)

        def arc(r: float, a_from: float, a_to: float) -> list[QPointF]:
            steps = max(24, int(abs(a_to - a_from) / 3))  # a lone sector spans the whole ring
            return [QPointF(r * math.cos(math.radians(a)), r * math.sin(math.radians(a)))
                    for a in (a_from + (a_to - a_from) * k / steps for k in range(steps + 1))]

        points = arc(r1, mid - half + d1, mid + half - d1) + arc(r0, mid + half - d0, mid - half + d0)
        core = QPainterPath()
        core.addPolygon(QPolygonF(points))
        core.closeSubpath()
        stroker = QPainterPathStroker()
        stroker.setWidth(2 * c)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return core.united(stroker.createStroke(core)).simplified()

    def _hit(self, pos: QPointF) -> int | str | None:
        v = pos - self._center()
        dist = math.hypot(v.x(), v.y())
        if dist < self.R_IN - 4:
            return CENTER
        if not self.items or dist > self.R_OUT + self.RIM:
            return None
        angle = math.degrees(math.atan2(v.y(), v.x()))
        span = 360 / len(self.items)
        return int(((angle + 90 + span / 2) % 360) // span)

    # ----- Animation -----

    def _tick(self) -> None:
        for key in [*range(len(self.items)), CENTER]:
            target = 1.0 if key in (self.hover, self.chosen) else 0.0
            value = self.glow.get(key, 0.0)
            self.glow[key] = value + (target - value) * 0.35
        self.update()

    def _progress(self) -> tuple[float, float]:
        """(appear 0..1, close 0..1)."""
        appear = min(1.0, self._appear.elapsed() / self.APPEAR_MS) if self._appear.isValid() else 1.0
        close = min(1.0, self._close.elapsed() / self._close_ms()) if self._closing else 0.0
        return appear, close

    # ----- Painting -----

    def paintEvent(self, _event) -> None:
        t = _theme()
        appear, close = self._progress()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self._center()
        scale = 0.8 + 0.2 * _ease_out_back(appear)
        if self._collapse:
            scale *= 1 - (1 - self.COLLAPSED) * _ease_in_cubic(close)
            fade = 1 - max(0.0, close - 0.5) * 2  # fades only at the end, once it is small
        else:
            scale -= 0.06 * close
            fade = 1 - close
        p.setOpacity(min(1.0, appear * 1.6) * fade)
        p.save()
        p.translate(c)
        p.scale(scale, scale)
        self._paint_disc(p, t)
        for i, item in enumerate(self.items):
            self._paint_petal(p, t, i, item)
        self._paint_hub(p, t)
        p.restore()

        if self._collapse:
            p.setOpacity(p.opacity() * (1 - close))  # captions disappear right away
        self._paint_caption(p, t, self.title, QPointF(c.x(), self.CAPTION / 2 + 2), bold=True)
        self._paint_caption(p, t, self._bottom_caption(),
                            QPointF(c.x(), self.height() - self.TABS - self.CAPTION / 2 - 2))
        self._paint_pages(p, t, QPointF(c.x(), self.height() - self.TABS / 2 - 4))

    def _paint_disc(self, p: QPainter, t: _Theme) -> None:
        r = self.R_OUT + self.RIM
        shadow = QRadialGradient(QPointF(0, 6), r + self.SHADOW)
        shadow.setColorAt(r / (r + self.SHADOW) - 0.04, QColor(0, 0, 0, 70))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(shadow)
        p.drawEllipse(QPointF(0, 6), r + self.SHADOW, r + self.SHADOW)
        p.setPen(QPen(t.disc_edge, 1))
        p.setBrush(t.disc)
        p.drawEllipse(QPointF(0, 0), r, r)

    def _paint_petal(self, p: QPainter, t: _Theme, i: int, item: WheelItem) -> None:
        g = self.glow.get(i, 0.0)
        mid, _ = self._angles(i, len(self.items))
        dx, dy = math.cos(math.radians(mid)), math.sin(math.radians(mid))
        p.save()
        p.translate(dx * self.POP * g, dy * self.POP * g)

        label_r = (self.R_IN + self.R_OUT) / 2
        focus = QPointF(dx * label_r, dy * label_r)
        fill = QRadialGradient(focus, (self.R_OUT - self.R_IN) * 0.9)
        fill.setColorAt(0.0, _mix(t.petal, t.accent, g))
        fill.setColorAt(1.0, _mix(t.petal_shade, t.accent.darker(108), g))
        p.setPen(QPen(_mix(t.petal_edge, t.accent, g), 1))
        p.setBrush(fill)
        p.drawPath(self._paths[i])

        n = len(self.items)
        # Label width along the sector chord: two or three wide sectors fit "Images"
        width = min(170.0, 2 * label_r * math.sin(math.radians(min(180 / n, 90))) - 16)
        font = QFont()
        font.setWeight(QFont.Weight.Bold)
        # Long labels like "Compress" shrink first and only then get elided
        for size in range(17 if n <= 7 else 15 if n <= 9 else 14, 11, -1):
            font.setPixelSize(size)
            if QFontMetricsF(font).horizontalAdvance(item.label) <= width:
                break
        p.setFont(font)
        p.setPen(_mix(t.text, t.accent_text, g))
        box = QRectF(focus.x() - width / 2, focus.y() - 14, width, 28)
        if item.hint:
            box.translate(0, -6)
        label = QFontMetricsF(font).elidedText(item.label, Qt.TextElideMode.ElideRight, width)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        if item.hint:
            small = QFont()
            small.setPixelSize(10)
            p.setFont(small)
            p.setPen(_mix(t.muted, t.accent_text, g))
            p.drawText(QRectF(box.x(), box.bottom() - 4, width, 16), Qt.AlignmentFlag.AlignCenter, item.hint)
        p.restore()

    def _paint_hub(self, p: QPainter, t: _Theme) -> None:
        r = self.R_IN - 8
        p.setPen(QPen(t.disc_edge, 1))
        p.setBrush(t.hub)
        p.drawEllipse(QPointF(0, 0), r, r)

        text = self.items[self.hover].label if isinstance(self.hover, int) else self.center_label
        if not text:
            return
        g = self.glow.get(CENTER, 0.0) if self.center_data is not None else 0.0
        font = QFont()
        font.setWeight(QFont.Weight.DemiBold)
        # A long word like "Images" shrinks first and only then gets elided
        for size in (14, 13, 12, 11):
            font.setPixelSize(size)
            metrics = QFontMetricsF(font)
            if metrics.horizontalAdvance(text) + 24 <= 2 * r - 10:
                break
        width = min(2 * r - 10, metrics.horizontalAdvance(text) + 32)
        text = metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, width - 20)
        pill = QRectF(-width / 2, -19, width, 38)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 22))
        p.drawRoundedRect(pill.translated(0, 2), 19, 19)
        p.setPen(QPen(_mix(t.disc_edge, t.accent, g), 1 + g))
        p.setBrush(t.pill)
        p.drawRoundedRect(pill, 19, 19)
        p.setFont(font)
        p.setPen(t.text)
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)

    def _bottom_caption(self) -> str:
        if isinstance(self.hover, int):
            return self.items[self.hover].caption
        if self.hover == CENTER and self.center_data is not None:
            return self.center_caption
        return self.idle_caption

    def _paint_caption(self, p: QPainter, t: _Theme, text: str, at: QPointF, bold: bool = False) -> None:
        """Caption on a pill: the wheel floats over anything, bare gray text would be unreadable."""
        if not text:
            return
        font = QFont()
        font.setPixelSize(13)
        if bold:
            font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetricsF(font)
        text = metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, self.width() - 60)
        w = metrics.horizontalAdvance(text) + 24
        rect = QRectF(at.x() - w / 2, at.y() - 13, w, 26)
        p.setPen(QPen(t.disc_edge, 1))
        bg = QColor(t.disc)
        bg.setAlphaF(0.94)
        p.setBrush(bg)
        p.drawRoundedRect(rect, 13, 13)
        p.setFont(font)
        p.setPen(t.text if bold else t.muted)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_pages(self, p: QPainter, t: _Theme, at: QPointF) -> None:
        """Page strip: the current page is filled with the accent, a "Tab" hint on the right."""
        self._page_rects = []
        if len(self.pages) < 2:
            return
        font = QFont()
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        small = QFont()
        small.setPixelSize(10)
        small.setWeight(QFont.Weight.DemiBold)
        metrics, key_metrics = QFontMetricsF(font), QFontMetricsF(small)
        widths = [metrics.horizontalAdvance(name) + 24 for name in self.pages]
        key_w = key_metrics.horizontalAdvance("Tab") + 12
        total = sum(widths) + key_w + 16
        outer = QRectF(at.x() - total / 2, at.y() - 15, total, 30)

        bg = QColor(t.disc)
        bg.setAlphaF(0.94)
        p.setPen(QPen(t.disc_edge, 1))
        p.setBrush(bg)
        p.drawRoundedRect(outer, 15, 15)

        x = outer.left() + 4
        p.setFont(font)
        for i, (name, w) in enumerate(zip(self.pages, widths)):
            rect = QRectF(x, outer.top() + 4, w, outer.height() - 8)
            self._page_rects.append(rect)
            if i == self.page:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(t.accent)
                p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            p.setPen(t.accent_text if i == self.page else t.muted)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)
            x += w

        key = QRectF(x + 4, outer.center().y() - 9, key_w, 18)
        p.setPen(QPen(t.petal_edge, 1))
        p.setBrush(t.petal)
        p.drawRoundedRect(key, 5, 5)
        p.setFont(small)
        p.setPen(t.muted)
        p.drawText(key, Qt.AlignmentFlag.AlignCenter, "Tab")

    # ----- Input -----

    def _update_hover(self, pos: QPointF | QPoint) -> None:
        hover = self._hit(QPointF(pos))
        if hover == CENTER and self.center_data is None:
            hover = None
        if hover != self.hover:
            self.hover = hover
            self.update()

    def items_opens(self, key: int | str | None) -> bool:
        return isinstance(key, int) and self.items[key].opens

    def _select(self, key: int | str | None) -> None:
        if key is None or self._closing:
            return
        if key == CENTER:
            if self.center_data is None:
                return
            self.chosen = CENTER
            self._finish(self.center_data)
        elif isinstance(key, int) and 0 <= key < len(self.items):
            self.chosen = self.hover = key
            self._finish(self.items[key].data)

    def _finish(self, result: Any) -> None:
        # A second call before the loop exits (a drop, then the cancel timer) doesn't overwrite the result
        if self._loop is None or self._done:
            return
        self._done = True
        self._result = result
        self._loop.quit()

    def mouseMoveEvent(self, event) -> None:
        self._update_hover(event.position())

    def leaveEvent(self, _event) -> None:
        self.hover = None

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            self._finish(None)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        for i, rect in enumerate(self._page_rects):
            if rect.contains(event.position()):
                if i != self.page:
                    self._finish(("page", i))
                return
        key = self._hit(event.position())
        if key is None:
            self._finish(None)  # click outside the wheel
        else:
            self._select(key)

    def keyPressEvent(self, event) -> None:
        key, n = event.key(), len(self.items)
        K = Qt.Key
        if key == K.Key_Escape:
            self._finish(None)
        elif key in (K.Key_Tab, K.Key_Backtab):
            self.switch_page(-1 if key == K.Key_Backtab or event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
        elif n and key in (K.Key_Right, K.Key_Down, K.Key_Left, K.Key_Up):
            step = 1 if key in (K.Key_Right, K.Key_Down) else -1
            current = self.hover if isinstance(self.hover, int) else (-1 if step > 0 else 0)
            self.hover = (current + step) % n
            self.update()
        elif key in (K.Key_Return, K.Key_Enter, K.Key_Space):
            self._select(self.hover if self.hover is not None else (0 if n else None))
        elif K.Key_1 <= key <= K.Key_9:
            self._select(key - K.Key_1)
        else:
            super().keyPressEvent(event)

    # ----- Dragging -----

    def dragEnterEvent(self, event) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths or self._on_files is None:
            event.ignore()
            return
        self._on_files(paths)
        event.acceptProposedAction()
        self._drag_hover(event)

    def dragMoveEvent(self, event) -> None:
        self._drag_hover(event)

    def dragLeaveEvent(self, _event) -> None:
        self.hover = None
        self._spring.stop()

    def _spring_open(self) -> None:
        if isinstance(self.hover, int) and self.items[self.hover].opens:
            self._select(self.hover)

    def _drag_hover(self, event) -> None:
        before = self.hover
        self._update_hover(event.position())
        if self.hover != before:
            # Like folders in Finder: hold the file over "Images" and the formats spring open
            opens = isinstance(self.hover, int) and self.items[self.hover].opens
            self._spring.start() if opens else self._spring.stop()
        if self.hover is None:
            event.ignore()  # not over a sector: "not allowed" cursor, the file goes back
        else:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

    def dropEvent(self, event) -> None:
        self._update_hover(event.position())
        self._spring.stop()
        if self.hover is None or self.items_opens(self.hover):
            event.ignore()
            return
        # Copy: Finder and File Explorer leave the source file untouched
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self._select(self.hover)

    def event(self, event) -> bool:
        # Focus lost (click in another window, ⌘Tab): close like a regular menu
        if event.type() == QEvent.Type.WindowActivate:
            self._was_active = True
        elif event.type() == QEvent.Type.WindowDeactivate and self._was_active and not self._closing:
            self._finish(None)
        return super().event(event)



class ProgressRing(QWidget):
    """A circle at the cursor: conversion percent, then a checkmark (or "!" on errors).

    Follows the cursor with a slight lag and doesn't block clicks.
    """

    R = 15                      # progress arc radius
    DISC = 22                   # backing disc radius
    OFFSET = QPointF(30, 30)    # offset from the cursor tip
    APPEAR_MS, HOLD_MS, FADE_MS = 160, 900, 260

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint
                         | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        side = 2 * (self.DISC + 10)
        self.resize(side, side)

        self.value = 0.0
        self.state = "progress"  # progress | done | error
        self._pos = QPointF()
        self._clock = QElapsedTimer()   # since appearing
        self._ended = QElapsedTimer()   # since finishing
        self._timer = QTimer(self, interval=16, timeout=self._tick)

    @property
    def running(self) -> bool:
        return self.isVisible() and self.state == "progress"

    def start(self, at: QPoint) -> None:
        """Appears at `at` (where the wheel collapsed) and glides to the cursor."""
        if not self.isVisible() or self.state != "progress":
            self._pos = QPointF(at)
            self._clock.start()
        self.state = "progress"
        self.value = 0.0
        self._place()
        self.show()
        self._timer.start()

    def set_value(self, value: float) -> None:
        self.value = max(0.0, min(1.0, value))

    def finish(self, ok: bool) -> None:
        if not self.isVisible():
            return
        self.state = "done" if ok else "error"
        self.value = 1.0
        self._ended.start()

    def _place(self) -> None:
        self.move(round(self._pos.x() - self.width() / 2), round(self._pos.y() - self.height() / 2))

    def _tick(self) -> None:
        target = QPointF(QCursor.pos()) + self.OFFSET
        self._pos += (target - self._pos) * 0.22
        self._place()
        if self.state != "progress" and self._ended.elapsed() > self.HOLD_MS + self.FADE_MS:
            self._timer.stop()
            self.hide()
        self.update()

    def paintEvent(self, _event) -> None:
        t = _theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        appear = min(1.0, self._clock.elapsed() / self.APPEAR_MS) if self._clock.isValid() else 1.0
        fade = 1.0
        if self.state != "progress":
            fade = 1 - max(0.0, min(1.0, (self._ended.elapsed() - self.HOLD_MS) / self.FADE_MS))
        p.setOpacity(appear * fade)
        p.translate(self.width() / 2, self.height() / 2)
        p.scale(0.85 + 0.15 * appear, 0.85 + 0.15 * appear)

        # Backing disc with a shadow, like the wheel
        shadow = QRadialGradient(QPointF(0, 2), self.DISC + 8)
        shadow.setColorAt(self.DISC / (self.DISC + 8) - 0.1, QColor(0, 0, 0, 60))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(shadow)
        p.drawEllipse(QPointF(0, 2), self.DISC + 8, self.DISC + 8)

        ended = self._ended.elapsed() if self.state != "progress" else 0
        fill = min(1.0, ended / 180) if self.state != "progress" else 0.0
        color = t.accent if self.state != "error" else _ERROR
        p.setPen(QPen(_mix(t.disc_edge, color, fill), 1))
        p.setBrush(_mix(t.hub, color, fill))
        p.drawEllipse(QPointF(0, 0), self.DISC, self.DISC)

        ring = QRectF(-self.R, -self.R, 2 * self.R, 2 * self.R)
        if self.state == "progress":
            track = QColor(t.muted)
            track.setAlphaF(0.25)
            p.setPen(QPen(track, 3.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(ring)
            p.setPen(QPen(t.accent, 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            if self.value < 0.01:  # duration unknown yet: a spinning arc
                start = -self._clock.elapsed() * 0.4
                p.drawArc(ring, int(start * 16), int(-90 * 16))
            else:
                p.drawArc(ring, 90 * 16, int(-self.value * 360 * 16))
                font = QFont()
                font.setPixelSize(10)
                font.setWeight(QFont.Weight.DemiBold)
                p.setFont(font)
                p.setPen(t.text)
                p.drawText(ring, Qt.AlignmentFlag.AlignCenter, f"{int(self.value * 100)}")
            return

        # The checkmark draws in as a stroke, "!" just fades in
        draw = min(1.0, max(0.0, (ended - 80) / 220))
        p.setPen(QPen(t.accent_text, 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.state == "done":
            a, b, c = QPointF(-7, 0.5), QPointF(-2, 5.5), QPointF(7.5, -5)
            first = math.hypot(b.x() - a.x(), b.y() - a.y())
            second = math.hypot(c.x() - b.x(), c.y() - b.y())
            length = draw * (first + second)
            path = QPainterPath(a)
            if length <= first:
                path.lineTo(a + (b - a) * (length / first))
            else:
                path.lineTo(b)
                path.lineTo(b + (c - b) * ((length - first) / second))
            p.drawPath(path)
        else:
            p.setOpacity(p.opacity() * draw)
            p.drawLine(QPointF(0, -7), QPointF(0, 2))
            p.drawPoint(QPointF(0, 7))
