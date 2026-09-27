"""Filmstrip: one resizable row of thumbnails under the loupe / compare view.

It shares the grid's model and selection model, so clicking a photo here is
the same as clicking it in the grid. Thumbnails keep their aspect ratio and
scale with the strip's height (drag the splitter above it to resize).
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QTransform
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

from foto.catalog import ImageRecord
from foto.catalog.catalog import FLAG_REJECT
from foto.ui.grid import RecordRole

PAD = 4  # space above and below the thumbnails
MIN_HEIGHT, MAX_HEIGHT = 56, 320


def display_aspect(rec: ImageRecord) -> float:
    """Width / height as shown (camera orientation and user rotation applied), clamped for panoramas."""
    if not rec.width or not rec.height:
        return 1.5
    w, h = (rec.height, rec.width) if rec.rotate % 180 else (rec.width, rec.height)
    return max(0.5, min(3.0, w / h))


class FilmstripDelegate(QStyledItemDelegate):
    def __init__(self, view: "Filmstrip"):
        super().__init__(view)
        self.view = view

    def thumb_height(self) -> int:
        return max(24, self.view.viewport().height() - 2 * PAD)

    def sizeHint(self, option, index):
        h = self.thumb_height()
        rec: ImageRecord = index.data(RecordRole)
        return QSize(round(h * display_aspect(rec)) if rec else h, h)

    def paint(self, painter: QPainter, option, index):
        rec: ImageRecord = index.data(RecordRole)
        image: QImage | None = index.data(Qt.DecorationRole)
        r = option.rect.adjusted(1, 0, -1, 0)
        selected = bool(option.state & QStyle.State_Selected)
        current = index == self.view.currentIndex()

        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(r, QColor(44, 44, 44))
        if image is not None and not image.isNull():
            if rec.rotate:
                image = image.transformed(QTransform().rotate(rec.rotate))
            # Fill the cell, cropping the small mismatch between cell and thumbnail aspect.
            size = image.size().scaled(r.size(), Qt.KeepAspectRatioByExpanding)
            src = QRectF(0, 0, r.width() * image.width() / size.width(), r.height() * image.height() / size.height())
            src.moveCenter(QRectF(image.rect()).center())
            if rec.flag == FLAG_REJECT:
                painter.setOpacity(0.3)
            elif not (selected or current):
                painter.setOpacity(0.8)
            painter.drawImage(QRectF(r), image, src)
            painter.setOpacity(1.0)
        if current or selected:
            painter.setPen(QPen(QColor(245, 245, 245) if current else QColor(150, 150, 150), 2))
            painter.drawRect(QRect(r).adjusted(1, 1, -1, -1))
        painter.restore()


class Filmstrip(QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ListMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListView.Static)
        self.setLayoutMode(QListView.Batched)
        self.setBatchSize(500)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setViewportMargins(0, PAD, 0, PAD)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setStyleSheet("QListView { background: #1e1e1e; border: none; }")
        self.setMinimumHeight(MIN_HEIGHT)
        self.setMaximumHeight(MAX_HEIGHT)
        self.setItemDelegate(FilmstripDelegate(self))

    def keyboardSearch(self, search: str) -> None:
        pass  # letters and digits are rating / flag shortcuts

    def wheelEvent(self, event):
        # A plain mouse wheel scrolls sideways; trackpads already send horizontal deltas.
        delta = event.angleDelta()
        step = delta.x() or delta.y()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - step)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().height() != event.oldSize().height():
            self.scheduleDelayedItemsLayout()  # thumbnails follow the strip's height

    def center_on(self, index) -> None:
        if index.isValid():
            self.scrollTo(index, QAbstractItemView.PositionAtCenter)
