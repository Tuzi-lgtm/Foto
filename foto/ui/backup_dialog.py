"""Manage backup targets: NAS / mounted folders and rclone cloud remotes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from foto.backup.base import BackupError
from foto.backup.rclone import find_rclone, list_remotes
from foto.backup.targets import (
    BackupTarget, delete_target, list_targets, save_target, target_stats,
)
from foto.catalog import Catalog

HELP = (
    "<b>NAS / folder</b>: mount your Synology (or any) share first — SMB in Finder / "
    "Explorer — then pick the folder.<br>"
    "<b>Cloud (rclone)</b>: run <code>rclone config</code> once to add Google Cloud Storage, "
    "Google Drive, S3, Synology SFTP/WebDAV…, then enter <code>remote:path</code>."
)


class TargetEditor(QDialog):
    def __init__(self, target: BackupTarget | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Backup target")
        self.target = target or BackupTarget(None, "", "folder", {})
        self.name = QLineEdit(self.target.name)
        self.kind = QComboBox()
        self.kind.addItem("NAS / mounted folder", "folder")
        self.kind.addItem("Cloud via rclone", "rclone")
        self.kind.setCurrentIndex(0 if self.target.kind == "folder" else 1)
        self.location = QLineEdit(self.target.location or "")
        self.browse = QPushButton("Browse…")
        self.browse.clicked.connect(self._browse)
        self.remotes = QComboBox()
        self.remotes.addItem("Configured rclone remotes…")
        for r in list_remotes():
            self.remotes.addItem(r)
        self.remotes.activated.connect(self._pick_remote)
        self.keep = QSpinBox(minimum=1, maximum=500, value=self.target.keep_snapshots)
        self.rclone_note = QLabel()

        loc_row = QHBoxLayout()
        loc_row.addWidget(self.location, 1)
        loc_row.addWidget(self.browse)
        loc_row.addWidget(self.remotes)
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Type", self.kind)
        form.addRow("Location", loc_row)
        form.addRow("Catalog snapshots to keep", self.keep)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        help_label = QLabel(HELP)
        help_label.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.rclone_note)
        lay.addWidget(help_label)
        lay.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed()
        self.resize(560, 0)

    def _kind_changed(self) -> None:
        folder = self.kind.currentData() == "folder"
        self.browse.setVisible(folder)
        self.remotes.setVisible(not folder)
        self.location.setPlaceholderText("/Volumes/photo/Foto" if folder else "gcs:my-bucket/foto")
        self.rclone_note.setText("" if folder or find_rclone() else "⚠ rclone is not installed or not on PATH.")

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Backup folder", self.location.text())
        if path:
            self.location.setText(path)

    def _pick_remote(self, index: int) -> None:
        if index > 0:
            self.location.setText(self.remotes.itemText(index) + "Foto")

    def _accept(self) -> None:
        loc = self.location.text().strip()
        if not loc:
            QMessageBox.warning(self, "Backup target", "Choose a location.")
            return
        kind = self.kind.currentData()
        self.target.name = self.name.text().strip() or loc
        self.target.kind = kind
        self.target.config = {("path" if kind == "folder" else "remote"): loc, "keep_snapshots": self.keep.value()}
        try:
            self.target.backend().check()
        except BackupError as exc:
            if QMessageBox.question(self, "Backup target", f"{exc}\n\nSave anyway?") != QMessageBox.Yes:
                return
        self.accept()


class BackupDialog(QDialog):
    def __init__(self, catalog: Catalog, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Backup targets")
        self.catalog = catalog
        self.list = QListWidget()
        add = QPushButton("Add…")
        edit = QPushButton("Edit…")
        remove = QPushButton("Remove")
        add.clicked.connect(lambda: self._edit(None))
        edit.clicked.connect(lambda: self._edit(self._selected()))
        remove.clicked.connect(self._remove)
        self.list.itemDoubleClicked.connect(lambda _: self._edit(self._selected()))
        side = QVBoxLayout()
        for b in (add, edit, remove):
            side.addWidget(b)
        side.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        row.addLayout(side)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(QLabel("Removing a target only forgets it; files at the destination stay."))
        lay.addWidget(close)
        self.resize(620, 320)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for t in list_targets(self.catalog):
            n, total, last = target_stats(self.catalog, t.id)
            it = QListWidgetItem(f"{t.name}\n   {t.location}   ·   {n}/{total} backed up   ·   last: {last or 'never'}")
            it.setData(Qt.UserRole, t)
            self.list.addItem(it)

    def _selected(self) -> BackupTarget | None:
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _edit(self, target: BackupTarget | None) -> None:
        dlg = TargetEditor(target, self)
        if dlg.exec():
            save_target(self.catalog, dlg.target)
            self.refresh()

    def _remove(self) -> None:
        t = self._selected()
        if t and QMessageBox.question(self, "Remove target", f"Forget backup target “{t.name}”?") == QMessageBox.Yes:
            delete_target(self.catalog, t.id)
            self.refresh()
