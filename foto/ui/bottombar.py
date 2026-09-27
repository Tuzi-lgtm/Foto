"""Toolbar along the bottom: view modes, secondary window, rating, flags, edits and zoom."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QMenu, QToolButton, QWidget

from foto.catalog import ImageRecord
from foto.catalog.catalog import FLAG_NONE, FLAG_PICK, FLAG_REJECT

ZOOM_STEPS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)  # pixel ratios offered in the zoom menu

STYLE = """
QToolButton { padding: 3px 9px; border: none; border-radius: 3px; }
QToolButton:hover { background: #4a4a4a; }
QToolButton:checked { background: #5a5a5a; color: white; }
QToolButton:disabled { color: #666; }
QToolButton::menu-indicator { image: none; width: 0; }
QToolButton#star { padding: 2px 1px; font-size: 16px; color: #5a5a5a; }
QToolButton#star[lit="true"] { color: #e8e8e8; }
QToolButton#pick:checked { background: #5a5a5a; color: #f0f0f0; }
QToolButton#reject:checked { background: #6a3530; color: #ff9b90; }
QFrame#group { background: #2e2e2e; border-radius: 4px; }
"""


def _group(*widgets) -> QFrame:
    """A rounded capsule holding related buttons."""
    frame = QFrame(objectName="group")
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(4, 1, 4, 1)
    lay.setSpacing(1)
    for w in widgets:
        lay.addWidget(w)
    return frame


class BottomBar(QWidget):
    modeClicked = Signal(int)  # GRID / LOUPE / COMPARE (main window's numbering)
    ratingClicked = Signal(int)
    flagClicked = Signal(int)
    zoomRequested = Signal(object)  # None = fit, else pixel ratio

    def __init__(self, modes: list[tuple[int, str, str]], secondary_actions: list[QAction],
                 edit_actions: list[QAction], parent=None):
        super().__init__(parent)
        self.setStyleSheet(STYLE)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(8)

        # View modes
        self.mode_buttons = QButtonGroup(self)
        mode_widgets = []
        for mode, name, key in modes:
            b = QToolButton(text=name, checkable=True, toolTip=f"{name} ({key})")
            self.mode_buttons.addButton(b, mode)
            mode_widgets.append(b)
        self.mode_buttons.idClicked.connect(self.modeClicked)
        second = QToolButton(text="Secondary ▾", toolTip="Secondary window (F11)")
        second.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(second)
        menu.addAction(secondary_actions[0])
        menu.addSeparator()
        menu.addActions(secondary_actions[1:])
        second.setMenu(menu)
        lay.addWidget(_group(*mode_widgets))
        lay.addWidget(second)
        lay.addStretch(1)

        # Rating and flags
        self.stars = []
        for n in range(1, 6):
            b = QToolButton(text="★", objectName="star", toolTip=f"{n} star{'s' * (n > 1)} ({n}); click again to clear")
            b.clicked.connect(lambda _=False, n=n: self._star_clicked(n))
            self.stars.append(b)
        self.pick = QToolButton(text="⚑", objectName="pick", checkable=True, toolTip="Pick (P); click again to unflag")
        self.reject = QToolButton(text="✕", objectName="reject", checkable=True, toolTip="Reject (X); click again to unflag")
        self.pick.clicked.connect(lambda on: self.flagClicked.emit(FLAG_PICK if on else FLAG_NONE))
        self.reject.clicked.connect(lambda on: self.flagClicked.emit(FLAG_REJECT if on else FLAG_NONE))
        lay.addWidget(_group(*self.stars, self.pick, self.reject))

        # Edit commands (rotate, copy / paste settings)
        edit_buttons = []
        for action in edit_actions:
            b = QToolButton()
            b.setDefaultAction(action)
            edit_buttons.append(b)
        lay.addWidget(_group(*edit_buttons))
        lay.addStretch(1)

        # Zoom
        self.fit = QToolButton(text="Fit", toolTip="Fit to window (Z toggles Fit / 100%)")
        self.fit.clicked.connect(lambda: self.zoomRequested.emit(None))
        self.zoom = QToolButton(text="100% ▾", toolTip="Zoom")
        self.zoom.setPopupMode(QToolButton.InstantPopup)
        zoom_menu = QMenu(self.zoom)
        for ratio in ZOOM_STEPS:
            zoom_menu.addAction(f"{ratio:.0%}", lambda r=ratio: self.zoomRequested.emit(r))
        self.zoom.setMenu(zoom_menu)
        lay.addWidget(_group(self.fit, self.zoom))

        self._rating = 0
        self.show_record(None)

    # -- state from the main window --------------------------------------

    def set_mode(self, mode: int, zoomable: bool) -> None:
        self.mode_buttons.button(mode).setChecked(True)
        self.fit.setEnabled(zoomable)
        self.zoom.setEnabled(zoomable)

    def show_record(self, rec: ImageRecord | None) -> None:
        """Reflect the current photo's rating and flag."""
        self._rating = rec.rating if rec else 0
        for n, b in enumerate(self.stars, start=1):
            b.setProperty("lit", n <= self._rating)
            b.style().unpolish(b)
            b.style().polish(b)
            b.setEnabled(rec is not None)
        self.pick.setChecked(bool(rec) and rec.flag == FLAG_PICK)
        self.reject.setChecked(bool(rec) and rec.flag == FLAG_REJECT)
        self.pick.setEnabled(rec is not None)
        self.reject.setEnabled(rec is not None)

    def show_zoom(self, percent: float | None) -> None:
        self.zoom.setText("Fit ▾" if percent is None else f"{percent:.0f}% ▾")

    def _star_clicked(self, n: int) -> None:
        self.ratingClicked.emit(0 if n == self._rating else n)
