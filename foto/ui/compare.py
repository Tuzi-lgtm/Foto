"""Loupe (single image) and compare (side by side) views."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from foto.catalog import ImageRecord
from foto.catalog.catalog import FLAG_PICK, FLAG_REJECT
from foto.color.ocio import ColorManager
from foto.imaging import decode as dec
from foto.imaging.service import ImageService
from foto.ui.image_view import ImageView

_RANK = {None: -1, dec.THUMB: 0, dec.PREVIEW: 1, dec.FULL: 2}


def describe(rec: ImageRecord) -> str:
    stars = "★" * rec.rating if rec.rating else ""
    flag = {FLAG_PICK: "  ⚑ Pick", FLAG_REJECT: "  ✕ Rejected"}.get(rec.flag, "")
    return f"{rec.filename}   {stars}{flag}"


class ImagePane(QFrame):
    """One ImageView bound to one catalog image, loading thumb → preview → full."""

    activated = Signal()

    def __init__(self, service: ImageService, color: ColorManager, parent=None):
        super().__init__(parent)
        self.service = service
        self.record: ImageRecord | None = None
        self._level: str | None = None
        self.view = ImageView(color, self)
        self.view.wantsFullRes.connect(self._want_full)
        self.view.clicked.connect(self.activated)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.view)
        self.set_active(False)
        service.ready.connect(self._on_ready)

    def set_active(self, active: bool) -> None:
        self.setStyleSheet(
            "ImagePane { border: 2px solid %s; background: #262626; }" % ("#8a8a8a" if active else "#262626")
        )

    def show_record(self, rec: ImageRecord | None) -> None:
        same = self.record is not None and rec is not None and rec.id == self.record.id
        self.record = rec
        if rec is None:
            self._level = None
            self.view.set_image(None)
            self.view.overlay_text = ""
            self.view.update()
            return
        self.view.set_rotation(rec.rotate)
        self.view.overlay_text = describe(rec)
        if same:
            self.view.update()
            return
        self._level = None
        native = (rec.width, rec.height) if rec.width and rec.height else None
        self.view.set_native_size(None)
        image, level = self.service.best_available(rec)
        if image is not None:
            self._set(image, level, native)
        else:
            self.view.set_image(None, keep_view=False)
        if _RANK[self._level] < _RANK[dec.PREVIEW]:
            thumb = self.service.get(rec, dec.THUMB)
            if thumb is not None and self._level is None:
                self._set(thumb, dec.THUMB, native)
            self.service.get(rec, dec.PREVIEW)

    def _set(self, image: QImage, level: str, native=None) -> None:
        first = self._level is None
        self._level = level
        if level == dec.FULL:
            native = (image.width(), image.height())
        self.view.set_image(image, native_size=native, keep_view=not first)

    def _want_full(self) -> None:
        if self.record is not None and self._level != dec.FULL:
            self.service.get(self.record, dec.FULL)

    def _on_ready(self, image_id: int, level: str, image: QImage) -> None:
        if self.record is None or image_id != self.record.id:
            return
        if _RANK[level] > _RANK[self._level]:
            native = (self.record.width, self.record.height) if self.record.width else None
            self._set(image, level, native)


class LoupeView(QWidget):
    def __init__(self, service: ImageService, color: ColorManager, parent=None):
        super().__init__(parent)
        self.pane = ImagePane(service, color, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.pane)

    def show_record(self, rec: ImageRecord | None) -> None:
        self.pane.show_record(rec)

    def toggle_zoom(self) -> None:
        self.pane.view.toggle_zoom()


class CompareView(QWidget):
    """Two panes side by side. The active pane receives ratings and flags."""

    activeChanged = Signal(object)  # ImageRecord

    def __init__(self, service: ImageService, color: ColorManager, parent=None):
        super().__init__(parent)
        self.panes = [ImagePane(service, color, self), ImagePane(service, color, self)]
        self.sync = True
        self.active = 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        for i, pane in enumerate(self.panes):
            lay.addWidget(pane)
            pane.activated.connect(lambda i=i: self.set_active(i))
            pane.view.viewChanged.connect(lambda s, x, y, i=i: self._sync_from(i, s, x, y))
        self.set_active(0)

    def show_records(self, a: ImageRecord | None, b: ImageRecord | None) -> None:
        self.panes[0].show_record(a)
        self.panes[1].show_record(b)
        self.set_active(self.active)

    def records(self) -> list[ImageRecord | None]:
        return [p.record for p in self.panes]

    def active_record(self) -> ImageRecord | None:
        return self.panes[self.active].record

    def set_active(self, i: int) -> None:
        self.active = i
        for j, pane in enumerate(self.panes):
            pane.set_active(j == i)
        self.activeChanged.emit(self.panes[i].record)

    def refresh(self, recs_by_id: dict[int, ImageRecord]) -> None:
        for pane in self.panes:
            if pane.record and pane.record.id in recs_by_id:
                pane.show_record(recs_by_id[pane.record.id])

    def swap(self) -> None:
        a, b = self.records()
        self.show_records(b, a)

    def toggle_zoom(self) -> None:
        self.panes[self.active].view.toggle_zoom()

    def _sync_from(self, i: int, scale: float, cx: float, cy: float) -> None:
        if self.sync:
            self.panes[1 - i].view.set_view(scale, cx, cy)
