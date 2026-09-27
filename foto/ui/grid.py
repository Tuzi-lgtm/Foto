"""Thumbnail grid: model, delegate and view."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QTransform
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

from foto.catalog import ImageRecord
from foto.catalog.catalog import FLAG_PICK, FLAG_REJECT
from foto.imaging.decode import THUMB
from foto.imaging.service import ImageService

RecordRole = Qt.UserRole + 1


class ImageListModel(QAbstractListModel):
    def __init__(self, service: ImageService, parent=None):
        super().__init__(parent)
        self.service = service
        self.records: list[ImageRecord] = []
        self._row_of: dict[int, int] = {}
        service.ready.connect(self._on_ready)

    def set_records(self, records: list[ImageRecord]) -> None:
        self.beginResetModel()
        self.records = records
        self._row_of = {r.id: i for i, r in enumerate(records)}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        rec = self.records[index.row()]
        if role == RecordRole:
            return rec
        if role == Qt.DisplayRole:
            return rec.filename
        if role == Qt.DecorationRole:
            return self.service.get(rec, THUMB)
        if role == Qt.ToolTipRole:
            return rec.path
        return None

    def row_of(self, image_id: int) -> int | None:
        return self._row_of.get(image_id)

    def update_records(self, records: list[ImageRecord]) -> None:
        """Replace rows whose data changed (rating, flag, rotation...)."""
        for rec in records:
            row = self._row_of.get(rec.id)
            if row is not None:
                self.records[row] = rec
                idx = self.index(row)
                self.dataChanged.emit(idx, idx)

    def _on_ready(self, image_id: int, level: str, _image: QImage) -> None:
        row = self._row_of.get(image_id)
        if row is not None and level == THUMB:
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, [Qt.DecorationRole])


class ThumbDelegate(QStyledItemDelegate):
    FOOTER = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cell = 200

    def sizeHint(self, option, index):
        return QSize(self.cell, self.cell)

    def paint(self, painter: QPainter, option, index):
        rec: ImageRecord = index.data(RecordRole)
        image: QImage | None = index.data(Qt.DecorationRole)
        r = option.rect.adjusted(3, 3, -3, -3)
        selected = bool(option.state & QStyle.State_Selected)
        current = bool(option.state & QStyle.State_HasFocus)

        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(78, 78, 78) if selected else QColor(52, 52, 52))
        painter.drawRoundedRect(r, 3, 3)

        img_area = r.adjusted(8, 8, -8, -self.FOOTER)
        if image is not None and not image.isNull():
            if rec.rotate:
                image = image.transformed(QTransform().rotate(rec.rotate))
            size = image.size().scaled(img_area.size(), Qt.KeepAspectRatio)
            target = QRect(0, 0, size.width(), size.height())
            target.moveCenter(img_area.center())
            if rec.flag == FLAG_REJECT:
                painter.setOpacity(0.3)
            painter.drawImage(target, image)
            painter.setOpacity(1.0)
        else:
            painter.setPen(QColor(110, 110, 110))
            painter.drawText(img_area, Qt.AlignCenter, "…")

        footer = QRect(r.left() + 8, r.bottom() - self.FOOTER + 2, r.width() - 16, self.FOOTER - 4)
        self._paint_stars(painter, footer, rec.rating)
        self._paint_badges(painter, footer, rec)

        if current or selected:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(200, 200, 200) if current else QColor(130, 130, 130), 1.5))
            painter.drawRoundedRect(QRectF(r).adjusted(0.75, 0.75, -0.75, -0.75), 3, 3)
        painter.restore()

    @staticmethod
    def _paint_stars(painter, rect, rating):
        f = QFont(painter.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() * 0.95))
        painter.setFont(f)
        text = "★" * rating + "·" * (5 - rating)
        painter.setPen(QColor(230, 230, 230) if rating else QColor(110, 110, 110))
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, text)

    @staticmethod
    def _paint_badges(painter, rect, rec: ImageRecord):
        parts = []
        if rec.backed_up:
            parts.append(("☁", QColor(120, 170, 230)))
        if rec.flag == FLAG_PICK:
            parts.append(("⚑", QColor(240, 240, 240)))
        elif rec.flag == FLAG_REJECT:
            parts.append(("✕", QColor(235, 90, 80)))
        x = rect.right()
        for text, color in reversed(parts):
            w = painter.fontMetrics().horizontalAdvance(text) + 6
            painter.setPen(color)
            painter.drawText(QRect(x - w, rect.top(), w, rect.height()), Qt.AlignRight | Qt.AlignVCenter, text)
            x -= w


class GridView(QListView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setLayoutMode(QListView.Batched)
        self.setBatchSize(500)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setSpacing(0)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setStyleSheet("QListView { background: #262626; border: none; }")
        self.delegate = ThumbDelegate(self)
        self.setItemDelegate(self.delegate)
        self.set_cell_size(200)

    def keyboardSearch(self, search: str) -> None:
        pass  # letters and digits are rating / flag shortcuts, not type-to-find

    def set_cell_size(self, px: int) -> None:
        self.delegate.cell = px
        self.setGridSize(QSize(px, px))
        self.scheduleDelayedItemsLayout()

    def selected_records(self) -> list[ImageRecord]:
        rows = sorted(i.row() for i in self.selectionModel().selectedIndexes())
        return [self.model().index(r, 0).data(RecordRole) for r in rows]

    def current_record(self) -> ImageRecord | None:
        idx = self.currentIndex()
        return idx.data(RecordRole) if idx.isValid() else None
