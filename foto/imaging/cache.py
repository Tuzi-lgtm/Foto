"""On-disk JPEG cache for thumbnails and previews.

The key hashes path, size and mtime, all stored in the catalog, so a lookup
needs no filesystem stat of the original. An edited-on-disk original gets a
new key automatically.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PySide6.QtGui import QImage


def cache_key(path: str, file_size: int, mtime_ns: int) -> str:
    return hashlib.sha1(f"{path}|{file_size}|{mtime_ns}".encode()).hexdigest()


class DiskCache:
    def __init__(self, root: Path, quality: int = 85):
        self.root = Path(root)
        self.quality = quality

    def path_for(self, key: str, level: str) -> Path:
        return self.root / level / key[:2] / f"{key}.jpg"

    def load(self, key: str, level: str) -> QImage | None:
        p = self.path_for(key, level)
        if not p.exists():
            return None
        image = QImage(str(p))
        return None if image.isNull() else image

    def store(self, key: str, level: str, image: QImage) -> None:
        p = self.path_for(key, level)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.{id(image)}.tmp")
        if image.save(str(tmp), "JPG", self.quality):
            os.replace(tmp, p)
        else:
            tmp.unlink(missing_ok=True)

    def size_bytes(self) -> int:
        return sum(f.stat().st_size for f in self.root.rglob("*.jpg"))
