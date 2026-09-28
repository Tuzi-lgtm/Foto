"""Loupe (single image) and compare (side by side) views, built from RenderPanes."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from foto.catalog import ImageRecord
from foto.ui.pane import RenderPane


class LoupeView(QWidget):
    backgroundDoubleClicked = Signal()

    def __init__(self, service, loader, color, settings_for, show_source: bool = False, parent=None):
        super().__init__(parent)
        self.pane = RenderPane(service, loader, color, settings_for, show_source, self)
        self.pane.backgroundDoubleClicked.connect(self.backgroundDoubleClicked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.pane)

    def show_record(self, rec: ImageRecord | None) -> None:
        self.pane.show_record(rec)

    def toggle_zoom(self) -> None:
        self.pane.view.toggle_zoom()

    @property
    def record(self) -> ImageRecord | None:
        return self.pane.record

    @property
    def view(self):
        return self.pane.view


class CompareView(QWidget):
    """Two panes side by side. The active pane receives ratings and flags."""

    activeChanged = Signal(object)  # ImageRecord
    backgroundDoubleClicked = Signal()

    def __init__(self, service, loader, color, settings_for, parent=None):
        super().__init__(parent)
        self.panes = [RenderPane(service, loader, color, settings_for, parent=self) for _ in range(2)]
        self.sync = True
        self.active = 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        for i, pane in enumerate(self.panes):
            lay.addWidget(pane)
            pane.activated.connect(lambda i=i: self.set_active(i))
            pane.backgroundDoubleClicked.connect(self.backgroundDoubleClicked)
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
