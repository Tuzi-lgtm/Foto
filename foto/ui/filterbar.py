"""Filter / sort toolbar above the grid."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QSlider, QToolButton, QWidget

from foto.catalog import LibraryFilter

RATINGS = [("Any rating", 0), ("Unrated", -1)] + [("≥ " + "★" * n, n) for n in range(1, 6)]
FLAGS = [
    ("All flags", "all"), ("Picked", "picked"), ("Unflagged", "unflagged"),
    ("Rejected", "rejected"), ("Not rejected", "not_rejected"),
]
BACKUP = [("Any backup", "any"), ("Backed up", "backed_up"), ("Not backed up", "not_backed_up")]
SORTS = [("Capture time", "capture_time"), ("File name", "filename"), ("Import order", "imported"), ("Rating", "rating")]


def _combo(items) -> QComboBox:
    c = QComboBox()
    for label, value in items:
        c.addItem(label, value)
    return c


class FilterBar(QWidget):
    changed = Signal()
    cellSizeChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.search = QLineEdit(placeholderText="Search name, camera, lens, tag…")
        self.search.setClearButtonEnabled(True)
        self.rating = _combo(RATINGS)
        self.flag = _combo(FLAGS)
        self.backup = _combo(BACKUP)
        self.sort = _combo(SORTS)
        self.desc = QToolButton(text="↓", checkable=True, toolTip="Descending")
        self.size = QSlider(Qt.Horizontal, minimum=100, maximum=420, value=200)
        self.size.setFixedWidth(110)
        self.count = QLabel()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        for w in (self.search, self.rating, self.flag, self.backup, QLabel("Sort"), self.sort, self.desc):
            lay.addWidget(w)
        lay.addStretch(1)
        lay.addWidget(self.count)
        lay.addWidget(QLabel("Size"))
        lay.addWidget(self.size)

        self.search.textChanged.connect(self.changed)
        for c in (self.rating, self.flag, self.backup, self.sort):
            c.currentIndexChanged.connect(self.changed)
        self.desc.toggled.connect(self.changed)
        self.size.valueChanged.connect(self.cellSizeChanged)

    def apply_to(self, f: LibraryFilter) -> LibraryFilter:
        r = self.rating.currentData()
        f.unrated_only = r == -1
        f.min_rating = max(0, r)
        f.flag = self.flag.currentData()
        f.backup = self.backup.currentData()
        f.text = self.search.text()
        f.sort = self.sort.currentData()
        f.descending = self.desc.isChecked()
        return f
