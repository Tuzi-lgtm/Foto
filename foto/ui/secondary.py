"""Secondary window, meant for a second monitor: a grid or the current photo in detail.

It shares the main window's model and selection model, so selecting a photo on
either screen selects it on both, and Detail always shows the current photo.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QSlider, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from foto.color.ocio import ColorManager
from foto.imaging.service import ImageService
from foto.ui.compare import LoupeView
from foto.ui.grid import GridView, ImageListModel, RecordRole

GRID, DETAIL = 0, 1
MODE_NAMES = {GRID: "Grid", DETAIL: "Detail"}


class SecondaryWindow(QWidget):
    modeChanged = Signal(int)
    visibilityChanged = Signal(bool)

    def __init__(self, model: ImageListModel, selection, service: ImageService, loader, color: ColorManager,
                 settings_for, settings: QSettings, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Foto — Secondary Window")
        self.settings = settings
        self.selection = selection

        self.grid = GridView()
        self.grid.setModel(model)
        self.grid.setSelectionModel(selection)
        self.grid.activated.connect(lambda _: self.set_mode(DETAIL))
        self.detail = LoupeView(service, loader, color, settings_for)
        self.detail.backgroundDoubleClicked.connect(lambda: self.set_mode(GRID))
        self.stack = QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.detail)
        selection.currentChanged.connect(self._current_changed)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        self.buttons = QButtonGroup(self)
        for mode, name in MODE_NAMES.items():
            b = QToolButton(text=name, checkable=True)
            b.setToolTip(f"{name} (Shift+{'G' if mode == GRID else 'E'})")
            self.buttons.addButton(b, mode)
            bar.addWidget(b)
        self.buttons.idClicked.connect(self.set_mode)
        bar.addSpacing(12)
        self.size_label = QLabel("Size")
        self.size = QSlider(Qt.Horizontal, minimumWidth=120, maximumWidth=200)
        self.size.setRange(100, 480)
        self.size.valueChanged.connect(self.grid.set_cell_size)
        bar.addWidget(self.size_label)
        bar.addWidget(self.size)
        bar.addStretch(1)
        self.full = QToolButton(text="Full Screen", checkable=True)
        self.full.setToolTip("Full screen (Ctrl+Shift+F)")
        self.full.toggled.connect(self._set_full_screen)
        bar.addWidget(self.full)

        self.setStyleSheet("QToolButton { padding: 3px 10px; } QToolButton:checked { background: #5a5a5a; color: white; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.stack, 1)
        QShortcut(QKeySequence("Ctrl+Shift+F"), self, self.full.toggle)

        self.size.setValue(int(settings.value("secondary/cell_size", 240)))
        self.set_mode(int(settings.value("secondary/mode", DETAIL)))

    # -- modes -----------------------------------------------------------

    def mode(self) -> int:
        return self.stack.currentIndex()

    def set_mode(self, mode: int) -> None:
        mode = DETAIL if mode == DETAIL else GRID
        self.stack.setCurrentIndex(mode)
        self.buttons.button(mode).setChecked(True)
        self.size_label.setVisible(mode == GRID)
        self.size.setVisible(mode == GRID)
        if mode == DETAIL:
            self._current_changed(self.selection.currentIndex())
        else:
            self.grid.scrollTo(self.selection.currentIndex())
        self.modeChanged.emit(mode)

    def _current_changed(self, current, _previous=None) -> None:
        if self.mode() == DETAIL:
            self.detail.show_record(current.data(RecordRole) if current.isValid() else None)
        elif current.isValid():
            self.grid.scrollTo(current)

    # -- placement -------------------------------------------------------

    def show_on_other_screen(self, main: QWidget) -> None:
        """Restore the last position, or open maximized on a screen other than the main window's."""
        geo = self.settings.value("secondary/geometry")
        if geo and self.restoreGeometry(geo):
            self.showMaximized() if self.settings.value("secondary/maximized", True, type=bool) else self.show()
        else:
            here = main.screen()
            others = [s for s in QGuiApplication.screens() if s is not here]
            target = others[0] if others else here
            self.setGeometry(target.availableGeometry().adjusted(40, 40, -40, -40))
            self.showMaximized() if others else self.show()
        self.raise_()

    def _set_full_screen(self, on: bool) -> None:
        if on:
            self.showFullScreen()
        else:
            self.showMaximized()

    def save_state(self) -> None:
        self.settings.setValue("secondary/visible", self.isVisible())
        self.settings.setValue("secondary/mode", self.mode())
        self.settings.setValue("secondary/cell_size", self.size.value())
        if self.isVisible():
            self.settings.setValue("secondary/maximized", self.isMaximized() or self.isFullScreen())
            self.settings.setValue("secondary/geometry", self.saveGeometry())

    def closeEvent(self, event) -> None:
        """The user closed this window: remember where it was, and that it's closed."""
        self.save_state()
        self.settings.setValue("secondary/visible", False)
        self.full.blockSignals(True)
        self.full.setChecked(False)
        self.full.blockSignals(False)
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.visibilityChanged.emit(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.visibilityChanged.emit(False)
