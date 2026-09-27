"""Background threads for import and backup. Each opens its own catalog connection."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from foto.backup.engine import BackupEngine
from foto.backup.targets import get_target
from foto.catalog import Catalog
from foto.importer import import_folder


class _Worker(QThread):
    progress = Signal(int, int, str)  # done, total (0 = unknown), current file
    finishedWith = Signal(object, str)  # result, error message

    def __init__(self, db_path: Path, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def cancelled(self) -> bool:
        return self._cancel


class ImportWorker(_Worker):
    def __init__(self, db_path: Path, folder: str, parent=None):
        super().__init__(db_path, parent)
        self.folder = folder

    def run(self) -> None:
        catalog = Catalog.open(self.db_path)
        try:
            result = import_folder(
                catalog, self.folder, lambda n, p: self.progress.emit(n, 0, p), self.cancelled
            )
            self.finishedWith.emit(result, "")
        except Exception as exc:
            self.finishedWith.emit(None, str(exc))
        finally:
            catalog.close()


class BackupWorker(_Worker):
    def __init__(self, db_path: Path, target_id: int, image_ids=None, parent=None):
        super().__init__(db_path, parent)
        self.target_id = target_id
        self.image_ids = image_ids

    def run(self) -> None:
        catalog = Catalog.open(self.db_path)
        try:
            target = get_target(catalog, self.target_id)
            if target is None:
                raise RuntimeError("backup target no longer exists")
            result = BackupEngine(catalog, target).run(self.image_ids, self.progress.emit, self.cancelled)
            self.finishedWith.emit(result, "")
        except Exception as exc:
            self.finishedWith.emit(None, str(exc))
        finally:
            catalog.close()
