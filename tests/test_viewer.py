from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent


def _dclick(view, x, y):
    pos = QPointF(x, y)
    view.mouseDoubleClickEvent(
        QMouseEvent(QEvent.MouseButtonDblClick, pos, view.mapToGlobal(pos), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    )


def test_double_click_beside_image_goes_back(qapp):
    from foto.color.ocio import ColorManager
    from foto.ui.image_view import ImageView

    view = ImageView(ColorManager())
    view.resize(400, 400)
    image = QImage(600, 300, QImage.Format_RGB888)  # landscape: letterboxed top and bottom
    image.fill(0)
    view.set_image(image, native_size=(6000, 3000), keep_view=False)
    backs = []
    view.backgroundDoubleClicked.connect(lambda: backs.append(1))

    _dclick(view, 200, 20)  # black bar above the photo
    assert backs == [1] and view.scale == 1.0

    _dclick(view, 200, 200)  # on the photo: 1:1 as before
    assert backs == [1] and view.scale > 1.0
