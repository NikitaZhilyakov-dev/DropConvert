import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPointF, QTimer, QUrl, Qt
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from dropconvert.agent import CONVERT_PAGE, TOOLS_PAGE, WheelSession
from dropconvert.wheel import FormatWheel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def files(tmp_path):
    image = tmp_path / "photo.png"
    Image.new("RGB", (8, 8)).save(image)
    video = tmp_path / "clip.mov"
    video.write_bytes(b"x")
    return image, video


def drag_wheel(app):
    wheel = FormatWheel("DropConvert")
    session = WheelSession(wheel)
    session.set_files(None)
    wheel.accept_files(session.set_files)
    wheel.show()
    return wheel, session


def mime(*paths):
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return data


def top_sector(wheel):
    c = wheel._center()
    return QPointF(c.x(), c.y() - (wheel.R_IN + wheel.R_OUT) / 2)


ACTIONS = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction


def test_tools_page_only_for_images(app, files):
    image, video = files
    session = WheelSession(FormatWheel("x"))
    session.set_files([video])
    assert session.pages == [CONVERT_PAGE]
    session.set_files([image, video])
    assert session.pages == [CONVERT_PAGE, TOOLS_PAGE]


def test_drop_on_tools_page(app, files):
    wheel, session = drag_wheel(app)
    pos = top_sector(wheel)
    data = mime(*files)
    enter = QDragEnterEvent(pos.toPoint(), ACTIONS, data, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    app.sendEvent(wheel, enter)
    assert enter.isAccepted() and wheel.title == "2 файла"

    def drop():
        event = QDropEvent(pos, ACTIONS, data, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        app.sendEvent(wheel, event)
        assert event.isAccepted() and event.dropAction() == Qt.DropAction.CopyAction
        wheel.cancel()  # запоздалая отмена после броска результат не меняет

    QTimer.singleShot(0, lambda: wheel.switch_page(1))  # Tab
    QTimer.singleShot(20, drop)
    assert session.run() == ("tool", "compress")
    assert session.page == 1


def test_drop_outside_sectors_is_rejected(app, files):
    wheel, session = drag_wheel(app)
    data = mime(files[1])
    app.sendEvent(wheel, QDragEnterEvent(top_sector(wheel).toPoint(), ACTIONS, data,
                                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    event = QDropEvent(QPointF(2, 2), ACTIONS, data, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    app.sendEvent(wheel, event)
    assert not event.isAccepted()


def test_drag_end_without_drop_closes(app):
    wheel, session = drag_wheel(app)
    QTimer.singleShot(0, wheel.cancel)
    assert session.run() is None


def test_mixed_files_spring_open_category(app, files):
    """Картинка и видео: задержка над «Изображениями» раскрывает форматы, бросок на сам тип не тратится."""
    wheel, session = drag_wheel(app)
    pos = top_sector(wheel)
    data = mime(*files)
    app.sendEvent(wheel, QDragEnterEvent(pos.toPoint(), ACTIONS, data,
                                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    early = QDropEvent(pos, ACTIONS, data, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    app.sendEvent(wheel, early)
    assert not early.isAccepted()

    def drop_on_format():
        assert wheel.items[0].data == ("image", "jpg")  # PNG пропущен: файл уже в нём
        event = QDropEvent(pos, ACTIONS, data, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        app.sendEvent(wheel, event)

    app.sendEvent(wheel, QDragLeaveEvent())  # файл увели и вернули
    app.sendEvent(wheel, QDragEnterEvent(pos.toPoint(), ACTIONS, data,
                                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    QTimer.singleShot(wheel.SPRING_MS + 100, drop_on_format)
    QTimer.singleShot(3000, wheel.cancel)  # чтобы тест не завис, если раскрытие не сработает
    assert session.run() == ("image", "jpg")
