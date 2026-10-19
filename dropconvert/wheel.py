"""Колесо форматов: всплывает у курсора вместо обычного меню.

Сектора-«лепестки» по кругу, наведённый подсвечивается и чуть выдвигается наружу,
в центре плашка с названием формата. Управление мышью или клавиатурой
(стрелки, Enter, цифры, Esc).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

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
    hint: str = ""     # мелкая подпись под названием в секторе
    caption: str = ""  # подпись под колесом при наведении


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


def _theme() -> _Theme:
    dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    return _DARK if dark else _LIGHT


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor.fromRgbF(*(x + (y - x) * t for x, y in zip(a.getRgbF(), b.getRgbF())))


def _ease_out_back(t: float) -> float:
    c = 1.4
    return 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2


class FormatWheel(QWidget):
    R_OUT = 150      # внешний радиус лепестков
    R_IN = 70        # внутренний радиус лепестков
    RIM = 12         # серый обод вокруг лепестков
    SHADOW = 26
    CAPTION = 44     # место под подписи сверху и снизу
    GAP = 6          # зазор между лепестками
    CORNER = 13      # скругление лепестков
    POP = 7          # насколько наведённый лепесток выдвигается наружу
    APPEAR_MS, CLOSE_MS = 230, 140

    def __init__(self, title: str) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        side = 2 * (self.R_OUT + self.RIM + self.SHADOW)
        self.resize(side, side + 2 * self.CAPTION)

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

        self._appear = QElapsedTimer()
        self._close = QElapsedTimer()
        self._closing = False
        self._loop: QEventLoop | None = None
        self._result: Any = None
        self._was_active = False
        self._timer = QTimer(self, interval=16, timeout=self._tick)

    # ----- Публичный интерфейс -----

    def set_items(self, items: list[WheelItem], center_label: str, center_data: Any = None,
                  center_caption: str = "", idle_caption: str = "") -> None:
        """Новый набор секторов (например, после выбора категории) с повтором анимации появления."""
        self.items = items
        self.center_label, self.center_data = center_label, center_data
        self.center_caption, self.idle_caption = center_caption, idle_caption
        self.hover = self.chosen = None
        self.glow = {}
        self._paths = [self._petal_path(i, len(items)) for i in range(len(items))]
        self._appear.start()
        self._timer.start()
        self.update()

    def popup(self, pos: QPoint) -> None:
        """Показывает колесо центром в точке pos, не вылезая за край экрана."""
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(pos.x() - self.width() // 2, area.left()), area.right() - self.width())
        y = min(max(pos.y() - self._center().y(), area.top()), area.bottom() - self.height())
        self.move(int(x), int(y))
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._update_hover(self.mapFromGlobal(QCursor.pos()))

    def choose(self, timeout_ms: int = 0) -> Any:
        """Ждёт выбора. Возвращает data выбранного сектора или None, если колесо закрыли."""
        self._result = None
        self._loop = QEventLoop()
        if timeout_ms:
            QTimer.singleShot(timeout_ms, lambda: self._finish(None))
        self._loop.exec()
        self._loop = None
        return self._result

    def close_animated(self) -> None:
        self._closing = True
        self._close.start()
        loop = QEventLoop()
        QTimer.singleShot(self.CLOSE_MS + 20, loop.quit)
        loop.exec()
        self._timer.stop()
        self.close()

    # ----- Геометрия -----

    def _center(self) -> QPointF:
        return QPointF(self.width() / 2, self.CAPTION + (self.width()) / 2)

    def _angles(self, i: int, n: int) -> tuple[float, float]:
        """Середина и ширина сектора в градусах; 0-й сектор сверху, дальше по часовой."""
        span = 360 / n
        return -90 + i * span, span

    def _petal_path(self, i: int, n: int) -> QPainterPath:
        """Кольцевой сектор со скруглёнными углами (в координатах с центром колеса в нуле).

        Строим сектор, ужатый на радиус скругления, и обводим его круглой кистью:
        объединение даёт ровный зазор и мягкие углы без ручной геометрии дуг.
        """
        mid, span = self._angles(i, n)
        c = self.CORNER
        r0, r1 = self.R_IN + c, self.R_OUT - c
        half = span / 2
        d0 = min(math.degrees((self.GAP / 2 + c) / r0), half - 1)
        d1 = min(math.degrees((self.GAP / 2 + c) / r1), half - 1)

        def arc(r: float, a_from: float, a_to: float, steps: int = 24) -> list[QPointF]:
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

    # ----- Анимация -----

    def _tick(self) -> None:
        for key in [*range(len(self.items)), CENTER]:
            target = 1.0 if key in (self.hover, self.chosen) else 0.0
            value = self.glow.get(key, 0.0)
            self.glow[key] = value + (target - value) * 0.35
        self.update()

    def _progress(self) -> tuple[float, float]:
        """(появление 0..1, закрытие 0..1)."""
        appear = min(1.0, self._appear.elapsed() / self.APPEAR_MS) if self._appear.isValid() else 1.0
        close = min(1.0, self._close.elapsed() / self.CLOSE_MS) if self._closing else 0.0
        return appear, close

    # ----- Рисование -----

    def paintEvent(self, _event) -> None:
        t = _theme()
        appear, close = self._progress()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(min(1.0, appear * 1.6) * (1 - close))

        c = self._center()
        scale = 0.8 + 0.2 * _ease_out_back(appear) - 0.06 * close
        p.save()
        p.translate(c)
        p.scale(scale, scale)
        self._paint_disc(p, t)
        for i, item in enumerate(self.items):
            self._paint_petal(p, t, i, item)
        self._paint_hub(p, t)
        p.restore()

        self._paint_caption(p, t, self.title, QPointF(c.x(), self.CAPTION / 2 + 2), bold=True)
        self._paint_caption(p, t, self._bottom_caption(),
                            QPointF(c.x(), self.height() - self.CAPTION / 2 - 2))

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
        font = QFont()
        font.setPixelSize(17 if n <= 7 else 15 if n <= 9 else 14)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(_mix(t.text, t.accent_text, g))
        # Ширина подписи по хорде сектора: у двух-трёх широких секторов влезает «Изображения»
        width = min(170.0, 2 * label_r * math.sin(math.radians(min(180 / n, 90))) - 16)
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
        # Длинное слово вроде «Изображения» сначала уменьшаем и только потом обрезаем
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
        """Подпись на плашке: колесо висит поверх чего угодно, голый серый текст не прочитать."""
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

    # ----- Ввод -----

    def _update_hover(self, pos: QPointF | QPoint) -> None:
        hover = self._hit(QPointF(pos))
        if hover == CENTER and self.center_data is None:
            hover = None
        if hover != self.hover:
            self.hover = hover
            self.update()

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
        self._result = result
        if self._loop is not None:
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
        key = self._hit(event.position())
        if key is None:
            self._finish(None)  # щелчок мимо колеса
        else:
            self._select(key)

    def keyPressEvent(self, event) -> None:
        key, n = event.key(), len(self.items)
        K = Qt.Key
        if key == K.Key_Escape:
            self._finish(None)
        elif n and key in (K.Key_Right, K.Key_Down, K.Key_Tab, K.Key_Left, K.Key_Up, K.Key_Backtab):
            step = 1 if key in (K.Key_Right, K.Key_Down, K.Key_Tab) else -1
            current = self.hover if isinstance(self.hover, int) else (-1 if step > 0 else 0)
            self.hover = (current + step) % n
            self.update()
        elif key in (K.Key_Return, K.Key_Enter, K.Key_Space):
            self._select(self.hover if self.hover is not None else (0 if n else None))
        elif K.Key_1 <= key <= K.Key_9:
            self._select(key - K.Key_1)
        else:
            super().keyPressEvent(event)

    def event(self, event) -> bool:
        # Ушёл фокус (щелчок в другом окне, ⌘Tab) — закрываемся, как обычное меню
        if event.type() == QEvent.Type.WindowActivate:
            self._was_active = True
        elif event.type() == QEvent.Type.WindowDeactivate and self._was_active and not self._closing:
            self._finish(None)
        return super().event(event)

