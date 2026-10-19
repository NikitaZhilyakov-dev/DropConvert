import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtCore import QPointF, QSize, QTimer, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from dropconvert.editor import SKIP, CropCanvas, CropEditor, crop_images, load_preview


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def canvas(app):
    """A 1000×500 image shown at exactly half size: (0, 0) in image pixels is (28, 28) on screen."""
    c = CropCanvas()
    c.resize(500 + 2 * CropCanvas.PAD, 250 + 2 * CropCanvas.PAD)
    c.set_image(QImage(100, 50, QImage.Format.Format_RGB32), QSize(1000, 500))
    return c


def screen(x, y):
    return QPointF(CropCanvas.PAD + x / 2, CropCanvas.PAD + y / 2)


def drag(canvas, a, b):
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=screen(*a).toPoint())
    QTest.mouseMove(canvas, screen(*b).toPoint())
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=screen(*b).toPoint())


def test_starts_with_the_whole_image(canvas):
    assert canvas.box() == (0, 0, 1000, 500) and canvas.is_full()


def test_drag_corner(canvas):
    drag(canvas, (1000, 500), (800, 300))
    assert canvas.box() == (0, 0, 800, 300)


def test_drag_edge_and_move(canvas):
    drag(canvas, (0, 250), (200, 250))
    assert canvas.box() == (200, 0, 1000, 500)
    drag(canvas, (600, 250), (400, 250))  # inside: moves, stopping at the image edge
    assert canvas.box() == (0, 0, 800, 500)


def test_draw_new_frame_outside(canvas):
    drag(canvas, (1000, 500), (300, 200))
    drag(canvas, (900, 400), (700, 300))  # outside the frame, drawn backwards
    assert canvas.box() == (700, 300, 900, 400)


def test_tiny_drag_keeps_the_frame(canvas):
    drag(canvas, (1000, 500), (10, 10))
    assert canvas.box() == (0, 0, 1000, 500)  # would be smaller than the minimum: unchanged
    drag(canvas, (1000, 500), (60, 60))
    assert canvas.box() == (0, 0, 60, 60)


def test_ratio_gives_the_largest_frame(canvas):
    canvas.set_ratio(1.0)
    assert canvas.box() == (250, 0, 750, 500)


def test_ratio_corner_drag_keeps_proportions(canvas):
    canvas.set_ratio(1.0)
    drag(canvas, (750, 500), (650, 200))
    left, top, right, bottom = canvas.box()
    assert (left, top) == (250, 0) and right - left == bottom - top == 400


def test_ratio_edge_drag_grows_evenly_and_stays_inside(canvas):
    drag(canvas, (1000, 500), (400, 200))  # free frame 400×200 in the corner
    canvas.set_ratio(2.0)
    drag(canvas, (400, 100), (900, 100))  # right edge: height can't pass the image, so it stops
    left, top, right, bottom = canvas.box()
    assert top >= 0 and bottom <= 500 and right - left == 2 * (bottom - top)


def test_preview_is_upright_and_reports_full_size(tmp_path):
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (4000, 1000)).save(path, exif=exif)
    image, full = load_preview(path)
    assert (full.width(), full.height()) == (1000, 4000)
    assert image.height() > image.width() and max(image.width(), image.height()) <= 2400


def test_editor_goes_through_images(app, tmp_path):
    paths = []
    for name in ("a.png", "b.png", "c.png"):
        Image.new("RGB", (400, 200)).save(tmp_path / name)
        paths.append(tmp_path / name)

    def step(actions):
        editor = next(w for w in app.topLevelWidgets() if isinstance(w, CropEditor) and w.isVisible())
        action = actions.pop(0)
        if action == "crop":
            editor.ratio_buttons[2].click()  # 1:1
            QTest.keyClick(editor, Qt.Key.Key_Return)
        elif action == SKIP:
            editor.skip.click()
        else:
            QTest.keyClick(editor, Qt.Key.Key_Escape)
        if actions:
            QTimer.singleShot(50, lambda: step(actions))

    QTimer.singleShot(50, lambda: step(["crop", SKIP, "crop"]))
    assert crop_images(paths) == {paths[0]: (100, 0, 300, 200), paths[2]: (100, 0, 300, 200)}

    QTimer.singleShot(50, lambda: step(["crop", "cancel"]))
    assert crop_images(paths) == {paths[0]: (100, 0, 300, 200)}


def test_editor_save_needs_a_crop_and_reports_bad_files(app, tmp_path):
    good, bad = tmp_path / "good.png", tmp_path / "bad.png"
    Image.new("RGB", (40, 40)).save(good)
    bad.write_bytes(b"not an image")
    seen = []

    def check():
        editor = next(w for w in app.topLevelWidgets() if isinstance(w, CropEditor) and w.isVisible())
        seen.append((editor.save.isEnabled(), editor.canvas.message))
        QTest.keyClick(editor, Qt.Key.Key_Return)  # ignored while Save is off
        editor.skip.click()
        if len(seen) < 2:
            QTimer.singleShot(50, check)

    QTimer.singleShot(50, check)
    assert crop_images([good, bad]) == {}
    assert seen[0] == (False, "") and not seen[1][0] and "Couldn't open" in seen[1][1]
