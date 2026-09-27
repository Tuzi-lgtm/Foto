"""Right panel: metadata for the current image and tag editing for the selection."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCompleter, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from foto.catalog import ImageRecord


def _shutter(s: float | None) -> str:
    if not s:
        return ""
    return f"1/{round(1 / s)} s" if s < 0.5 else f"{s:g} s"



def camera_name(make: str | None, model: str | None) -> str:
    """"Canon EOS R5m2", not "Canon Canon EOS R5m2": many cameras repeat the make in the model."""
    if make and model and model.lower().startswith(make.split()[0].lower()):
        return model
    return " ".join(x for x in (make, model) if x)

class Inspector(QWidget):
    addTags = Signal(list)
    removeTag = Signal(str)

    FIELDS = ("File", "Captured", "Camera", "Lens", "Exposure", "Size", "Path")

    def __init__(self, parent=None):
        super().__init__(parent)
        info = QGroupBox("Info")
        form = QFormLayout(info)
        form.setLabelAlignment(Qt.AlignRight)
        self._labels: dict[str, QLabel] = {}
        for name in self.FIELDS:
            lab = QLabel()
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lab.setWordWrap(True)
            self._labels[name] = lab
            form.addRow(name, lab)

        tags = QGroupBox("Tags")
        tl = QVBoxLayout(tags)
        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.tag_edit = QLineEdit(placeholderText="Add tags, comma separated")
        self.tag_edit.returnPressed.connect(self._add)
        self._completer = QCompleter([])
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.tag_edit.setCompleter(self._completer)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove)
        tl.addWidget(self.tag_list)
        row = QHBoxLayout()
        row.addWidget(self.tag_edit)
        tl.addLayout(row)
        tl.addWidget(remove)
        self.selection_label = QLabel()
        tl.addWidget(self.selection_label)

        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addWidget(tags, 1)

    def set_known_tags(self, names: list[str]) -> None:
        self._completer.model().setStringList(names)

    def show_record(self, rec: ImageRecord | None) -> None:
        if rec is None:
            for lab in self._labels.values():
                lab.clear()
            return
        exposure = "  ".join(
            x for x in (
                _shutter(rec.shutter),
                f"f/{rec.aperture:g}" if rec.aperture else "",
                f"ISO {rec.iso}" if rec.iso else "",
                f"{rec.focal:g} mm" if rec.focal else "",
            ) if x
        )
        values = {
            "File": rec.filename,
            "Captured": (rec.capture_time or "").replace("T", "  "),
            "Camera": camera_name(rec.make, rec.model),
            "Lens": rec.lens or "",
            "Exposure": exposure,
            "Size": f"{rec.width} × {rec.height}   {rec.file_size / 1e6:.1f} MB" if rec.width
            else f"{rec.file_size / 1e6:.1f} MB",
            "Path": rec.path,
        }
        for name, value in values.items():
            self._labels[name].setText(value)

    def show_tags(self, counts: dict[str, int], selected: int) -> None:
        self.tag_list.clear()
        for name, n in counts.items():
            it = QListWidgetItem(name if n == selected else f"{name}  ({n}/{selected})")
            it.setData(Qt.UserRole, name)
            self.tag_list.addItem(it)
        self.selection_label.setText(f"{selected} selected" if selected != 1 else "")

    def _add(self) -> None:
        names = [n.strip() for n in self.tag_edit.text().split(",") if n.strip()]
        if names:
            self.addTags.emit(names)
        self.tag_edit.clear()

    def _remove(self) -> None:
        for it in self.tag_list.selectedItems():
            self.removeTag.emit(it.data(Qt.UserRole))
