"""Photo editor: crop in a preview before saving a copy.

Opened by "Crop" on the Tools wheel. The image is shown upright (EXIF rotation applied),
the way the copy comes out, with a crop frame on top: drag inside it to move, drag its
corners and edges to resize, drag outside to draw a new one. Ratio presets lock the
frame's proportions. The frame is kept in pixels of the full image: the preview is only
a downscaled copy for display, the converter crops the original.

With several images the editor goes through them one by one: Save queues the crop,
Skip moves on, Cancel stops (crops already saved stay queued).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QEventLoop, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import converters  # noqa: F401 — registers the HEIC/AVIF opener for the preview

Box = tuple[int, int, int, int]  # left, top, right, bottom in pixels of the upright image
SKIP = "skip"

PREVIEW_SIDE = 2400  # the preview's longest side: enough for a Retina screen, fast to decode

ACCENT = "#EC5F2C"
_BACKGROUND = QColor("#1C1C1E")
_MUTED = QColor("#A1A1A6")

STYLE = f"""
QWidget#editor {{ background: #1C1C1E; color: #F2F2F4; font-size: 13px; }}
QLabel {{ color: #F2F2F4; }}
QLabel#title {{ font-weight: 600; }}
QLabel#muted {{ color: #A1A1A6; }}
QPushButton {{
    background: #2C2C2E; color: #F2F2F4; border: 1px solid #3A3A3D; border-radius: 7px;
    padding: 5px 11px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton:disabled {{ color: #6E6E73; border-color: #2C2C2E; }}
QPushButton#save {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; padding: 5px 18px; }}
QPushButton#save:disabled {{ background: #3A3A3D; border-color: #3A3A3D; color: #8E8E93; }}
"""


def load_preview(path: Path) -> tuple[QImage, QSize]:
    """A downscaled upright copy for display, and the full upright size in pixels."""
    with Image.open(path) as im:
        if getattr(im, "n_frames", 1) > 1:
            raise ValueError("animated images aren't supported yet")
        width, height = im.size
        if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):  # EXIF rotation by 90°
            width, height = height, width
        im.draft("RGB", (PREVIEW_SIDE, PREVIEW_SIDE))  # JPEG decodes straight at a smaller size
        preview = ImageOps.exif_transpose(im).convert("RGBA")
    preview.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), Image.Resampling.LANCZOS)
    image = QImage(preview.tobytes(), preview.width, preview.height, 4 * preview.width,
                   QImage.Format.Format_RGBA8888).copy()  # copy: the bytes object goes away
    return image, QSize(width, height)


_CURSORS = {
    "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
    "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
    "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
    "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
    "move": Qt.CursorShape.SizeAllCursor, "new": Qt.CursorShape.CrossCursor,
}


class CropCanvas(QWidget):
    """The image with the crop frame. The frame is in image pixels, float while dragging."""

    changed = Signal()
    PAD = 28    # room around the image
    GRAB = 12   # how close to an edge, in screen pixels, the pointer grabs it
    MIN = 24    # the smallest frame side on screen
    HANDLE = 18

    def __init__(self) -> None:
        super().__init__()
        self.setMouseTracking(True)
        self.setMinimumSize(320, 240)
        self.image: QImage | None = None
        self.full = QSize()
        self.crop = QRectF()
        self.ratio: float | None = None  # width / height, None for free
        self.message = ""
        self._scaled: QPixmap | None = None
        self._scaled_for = QSize()
        # (part being dragged: "tl", "r", "move", "new"…; press point in image pixels; frame at press)
        self._drag: tuple[str, QPointF, QRectF] | None = None

    # ----- State -----

    def set_image(self, image: QImage, full: QSize) -> None:
        self.image, self.full, self.message = image, full, ""
        self.crop = QRectF(0, 0, full.width(), full.height())
        self._scaled = None
        self.update()
        self.changed.emit()

    def set_message(self, text: str) -> None:
        """Shown instead of an image that couldn't be opened."""
        self.image, self.full, self.crop, self.message = None, QSize(), QRectF(), text
        self.update()
        self.changed.emit()

    def set_ratio(self, ratio: float | None) -> None:
        """Locks the proportions: the frame becomes the largest one of that ratio around its center."""
        self.ratio = ratio
        if ratio and self.image is not None:
            W, H = self.full.width(), self.full.height()
            w, h = (W, W / ratio) if W / ratio <= H else (H * ratio, H)
            c = self.crop.center()
            self.crop = QRectF(min(max(c.x() - w / 2, 0), W - w), min(max(c.y() - h / 2, 0), H - h), w, h)
            self.update()
            self.changed.emit()

    def reset(self) -> None:
        if self.image is not None:
            self.ratio = None
            self.crop = QRectF(0, 0, self.full.width(), self.full.height())
            self.update()
            self.changed.emit()

    def box(self) -> Box:
        # The size is rounded, not each edge: a 4:3 frame stays exactly 4:3 in pixels
        r, W, H = self.crop, self.full.width(), self.full.height()
        left, top = max(0, round(r.left())), max(0, round(r.top()))
        return left, top, min(W, left + round(r.width())), min(H, top + round(r.height()))

    def is_full(self) -> bool:
        return self.box() == (0, 0, self.full.width(), self.full.height())

    # ----- Geometry -----

    def _view(self) -> QRectF:
        """Where the image is drawn: fitted into the widget."""
        area = QRectF(self.rect()).adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)
        s = min(area.width() / self.full.width(), area.height() / self.full.height())
        w, h = self.full.width() * s, self.full.height() * s
        return QRectF(area.center().x() - w / 2, area.center().y() - h / 2, w, h)

    def _scale(self) -> float:
        return self._view().width() / self.full.width()

    def _to_view(self, r: QRectF) -> QRectF:
        v, s = self._view(), self._scale()
        return QRectF(v.x() + r.x() * s, v.y() + r.y() * s, r.width() * s, r.height() * s)

    def _to_image(self, p: QPointF) -> QPointF:
        v, s = self._view(), self._scale()
        return QPointF((p.x() - v.x()) / s, (p.y() - v.y()) / s)

    def _clamp(self, p: QPointF) -> QPointF:
        return QPointF(min(max(p.x(), 0), self.full.width()), min(max(p.y(), 0), self.full.height()))

    def _hit(self, p: QPointF) -> str:
        """The part of the frame under p: edges such as "tl" or "r", "move" inside, "new" outside."""
        r, g = self._to_view(self.crop), self.GRAB
        if not r.adjusted(-g, -g, g, g).contains(p):
            return "new"
        edges = ""
        if abs(p.y() - r.top()) <= g:
            edges += "t"
        elif abs(p.y() - r.bottom()) <= g:
            edges += "b"
        if abs(p.x() - r.left()) <= g:
            edges += "l"
        elif abs(p.x() - r.right()) <= g:
            edges += "r"
        return edges or "move"

    def _from_anchor(self, a: QPointF, p: QPointF) -> QRectF:
        """A frame stretched from the fixed point a to the pointer p, kept inside the image."""
        p = self._clamp(p)
        w, h = abs(p.x() - a.x()), abs(p.y() - a.y())
        right, down = p.x() >= a.x(), p.y() >= a.y()
        if self.ratio:
            w = max(w, h * self.ratio)
            h = w / self.ratio
            room_w = self.full.width() - a.x() if right else a.x()
            room_h = self.full.height() - a.y() if down else a.y()
            fit = min(1.0, room_w / w if w else 1.0, room_h / h if h else 1.0)
            w, h = w * fit, h * fit
        return QRectF(a.x() if right else a.x() - w, a.y() if down else a.y() - h, w, h)

    def _from_edge(self, edge: str, r: QRectF, p: QPointF) -> QRectF:
        """One edge follows the pointer; with a locked ratio the frame grows evenly across."""
        W, H = self.full.width(), self.full.height()
        p = self._clamp(p)
        if edge in ("l", "r"):
            a = r.left() if edge == "r" else r.right()
            w = p.x() - a if edge == "r" else a - p.x()
            if not self.ratio:
                return QRectF(a if edge == "r" else p.x(), r.top(), w, r.height())
            c = r.center().y()
            w = min(w, 2 * min(c, H - c) * self.ratio)
            h = w / self.ratio
            return QRectF(a if edge == "r" else a - w, c - h / 2, w, h)
        a = r.top() if edge == "b" else r.bottom()
        h = p.y() - a if edge == "b" else a - p.y()
        if not self.ratio:
            return QRectF(r.left(), a if edge == "b" else p.y(), r.width(), h)
        c = r.center().x()
        h = min(h, 2 * min(c, W - c) / self.ratio)
        w = h * self.ratio
        return QRectF(c - w / 2, a if edge == "b" else a - h, w, h)

    def _dragged(self, mode: str, start: QPointF, r: QRectF, p: QPointF) -> QRectF:
        if mode == "move":
            x = min(max(r.x() + p.x() - start.x(), 0), self.full.width() - r.width())
            y = min(max(r.y() + p.y() - start.y(), 0), self.full.height() - r.height())
            return QRectF(x, y, r.width(), r.height())
        if mode == "new":
            return self._from_anchor(start, p)
        if len(mode) == 2:  # a corner: the opposite one stays put
            return self._from_anchor(QPointF(r.right() if "l" in mode else r.left(),
                                             r.bottom() if "t" in mode else r.top()), p)
        return self._from_edge(mode, r, p)

    # ----- Mouse -----

    def mousePressEvent(self, event) -> None:
        if self.image is None or event.button() != Qt.MouseButton.LeftButton:
            return
        mode = self._hit(event.position())
        self._drag = (mode, self._clamp(self._to_image(event.position())), QRectF(self.crop))
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self.image is None:
            return
        if self._drag is None:
            self.setCursor(_CURSORS[self._hit(event.position())])
            return
        mode, start, r = self._drag
        new = self._dragged(mode, start, r, self._to_image(event.position()))
        # Too small (or turned inside out): the frame stops where it was
        smallest = self.MIN / self._scale()
        if new.width() >= smallest and new.height() >= smallest:
            self.crop = new
            self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, _event) -> None:
        self._drag = None
        self.update()

    def resizeEvent(self, _event) -> None:
        self._scaled = None

    # ----- Painting -----

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), _BACKGROUND)
        if self.image is None:
            p.setPen(_MUTED)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message)
            return

        view = self._view()
        if self._scaled is None or self._scaled_for != view.size().toSize():
            # Scaled once per size: smooth scaling of the whole preview on every mouse move is too slow
            dpr = self.devicePixelRatioF()
            self._scaled = QPixmap.fromImage(self.image.scaled(
                (view.size() * dpr).toSize(), Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            self._scaled.setDevicePixelRatio(dpr)
            self._scaled_for = view.size().toSize()
        p.drawPixmap(view.topLeft(), self._scaled)

        r = self._to_view(self.crop)
        shade = QPainterPath()
        shade.addRect(view)
        shade.addRect(r)  # odd-even fill: everything but the frame darkens
        p.fillPath(shade, QColor(0, 0, 0, 150))

        if self._drag is not None:  # rule of thirds while adjusting
            p.setPen(QPen(QColor(255, 255, 255, 110), 1))
            for k in (1, 2):
                x = r.left() + r.width() * k / 3
                y = r.top() + r.height() * k / 3
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        p.setPen(QPen(QColor(ACCENT), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)

        p.setPen(QPen(QColor(ACCENT), 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        n = min(self.HANDLE, r.width() / 3, r.height() / 3)
        for x, y, dx, dy in ((r.left(), r.top(), 1, 1), (r.right(), r.top(), -1, 1),
                             (r.left(), r.bottom(), 1, -1), (r.right(), r.bottom(), -1, -1)):
            p.drawPolyline(QPolygonF([QPointF(x + dx * n, y), QPointF(x, y), QPointF(x, y + dy * n)]))
        c = r.center()
        for a, b in (((c.x() - n / 2, r.top()), (c.x() + n / 2, r.top())),
                     ((c.x() - n / 2, r.bottom()), (c.x() + n / 2, r.bottom())),
                     ((r.left(), c.y() - n / 2), (r.left(), c.y() + n / 2)),
                     ((r.right(), c.y() - n / 2), (r.right(), c.y() + n / 2))):
            p.drawLine(QPointF(*a), QPointF(*b))


class CropEditor(QWidget):
    """The editor window: the canvas, ratio presets and Save / Skip / Cancel."""

    # Ratios in landscape form; ⇄ turns them portrait
    RATIOS: list[tuple[str, object]] = [
        ("Free", None), ("Original", "original"), ("1:1", (1, 1)), ("4:3", (4, 3)), ("3:2", (3, 2)), ("16:9", (16, 9)),
    ]

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setObjectName("editor")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(STYLE)
        self.setMinimumSize(560, 420)
        self._loop: QEventLoop | None = None
        self._result: Box | str | None = None
        self._ratio: object = None
        self._portrait = False

        self.canvas = CropCanvas()
        self.canvas.changed.connect(self._on_changed)

        self.title = QLabel()
        self.title.setObjectName("title")
        hint = QLabel("Drag the corners or draw a new frame · Enter saves, Esc cancels")
        hint.setObjectName("muted")
        top = QHBoxLayout()
        top.setContentsMargins(16, 12, 16, 0)
        top.addWidget(self.title)
        top.addStretch()
        top.addWidget(hint)

        self.ratio_buttons: list[QPushButton] = []
        group = QButtonGroup(self)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(16, 10, 16, 14)
        bottom.setSpacing(4)
        for label, value in self.RATIOS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, v=value: self._set_ratio(v))
            group.addButton(btn)
            bottom.addWidget(btn)
            self.ratio_buttons.append(btn)
        self.ratio_buttons[0].setChecked(True)
        self.flip = QPushButton("⇄")
        self.flip.setToolTip("Swap portrait and landscape")
        self.flip.clicked.connect(self._flip)
        bottom.addSpacing(4)
        bottom.addWidget(self.flip)
        bottom.addStretch()
        self.size_label = QLabel()
        self.size_label.setObjectName("muted")
        bottom.addWidget(self.size_label)
        bottom.addSpacing(12)

        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset)
        self.skip = QPushButton("Skip")
        self.skip.clicked.connect(lambda: self._finish(SKIP))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(lambda: self._finish(None))
        self.save = QPushButton("Save")
        self.save.setObjectName("save")
        self.save.clicked.connect(self._save)
        for btn in (reset, self.skip, cancel, self.save):
            bottom.addSpacing(4)
            bottom.addWidget(btn)
        # Enter and Esc go to the window, not to whichever button was clicked last
        for btn in [*self.ratio_buttons, self.flip, reset, self.skip, cancel, self.save]:
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(bottom)

    def ask(self, path: Path, index: int, total: int) -> Box | str | None:
        """Shows one image. Returns its frame (Save), SKIP, or None (Cancel, Esc, closed window)."""
        self.setWindowTitle(f"Crop · {path.name}")
        self.title.setText(path.name if total == 1 else f"{path.name}  ·  {index} of {total}")
        self.skip.setVisible(total > 1)
        try:
            image, full = load_preview(path)
        except Exception as e:  # Pillow raises all kinds of exceptions
            self.canvas.set_message(f"Couldn't open the image: {e}")
        else:
            self.canvas.set_image(image, full)
            self._portrait = full.height() > full.width()
            self._apply_ratio()  # the chosen ratio carries over to the next image
        if not self.isVisible():
            self._place()
            self.show()
        self.raise_()
        self.activateWindow()
        self._result = None
        self._loop = QEventLoop()
        self._loop.exec()
        self._loop = None
        return self._result

    def _place(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.resize(max(self.minimumWidth(), int(area.width() * 0.7)),
                    max(self.minimumHeight(), int(area.height() * 0.78)))
        self.move(area.center().x() - self.width() // 2, area.center().y() - self.height() // 2)

    # ----- Ratio -----

    def _set_ratio(self, value: object) -> None:
        self._ratio = value
        self._apply_ratio()

    def _flip(self) -> None:
        self._portrait = not self._portrait
        self._apply_ratio()

    def _apply_ratio(self) -> None:
        full = self.canvas.full
        ratio = None
        if self._ratio == "original" and not full.isEmpty():
            ratio = max(full.width(), full.height()) / min(full.width(), full.height())
        elif isinstance(self._ratio, tuple):
            ratio = self._ratio[0] / self._ratio[1]
        if ratio and self._portrait:
            ratio = 1 / ratio
        self.canvas.set_ratio(ratio)
        for btn, (label, value) in zip(self.ratio_buttons, self.RATIOS):
            flipped = isinstance(value, tuple) and self._portrait and value[0] != value[1]
            btn.setText(f"{value[1]}:{value[0]}" if flipped else label)
        self.flip.setEnabled(ratio is not None and abs(ratio - 1) > 1e-6)

    def _reset(self) -> None:
        self.ratio_buttons[0].setChecked(True)
        self._ratio = None
        self.canvas.reset()
        self._apply_ratio()

    # ----- Result -----

    def _on_changed(self) -> None:
        ready = self.canvas.image is not None
        if ready:
            left, top, right, bottom = self.canvas.box()
            self.size_label.setText(f"{right - left} × {bottom - top} px")
        else:
            self.size_label.setText("")
        self.save.setEnabled(ready and not self.canvas.is_full())

    def _save(self) -> None:
        if self.save.isEnabled():
            self._finish(self.canvas.box())

    def _finish(self, result: Box | str | None) -> None:
        if self._loop is not None:
            self._result = result
            self._loop.quit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._save()
        elif event.key() == Qt.Key.Key_Escape:
            self._finish(None)
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._finish(None)
        event.accept()


def crop_images(paths: list[Path]) -> dict[Path, Box]:
    """Opens the editor for each image in turn; returns the frames of the images to crop."""
    editor = CropEditor()
    boxes: dict[Path, Box] = {}
    try:
        for i, path in enumerate(paths, 1):
            result = editor.ask(path, i, len(paths))
            if result is None:
                break
            if result != SKIP:
                boxes[path] = result
    finally:
        editor.close()
        editor.deleteLater()
    return boxes
