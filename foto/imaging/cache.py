"""On-disk JPEG cache for thumbnails and previews.

The key hashes path, size and mtime, all stored in the catalog, so a lookup
needs no filesystem stat of the original. An edited-on-disk original gets a
new key automatically.

Files are touched when read, so `trim()` can drop the least recently used
ones once the cache grows past its size limit.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtGui import QImage

LEVELS = ("thumb", "preview")
_TOUCH_AFTER = 24 * 3600  # re-stamp a file's mtime on read at most once a day


def cache_key(path: str, file_size: int, mtime_ns: int, look: str = "") -> str:
    """Identifies one original (and, for rendered raws, the look it was rendered with)."""
    return hashlib.sha1(f"{path}|{file_size}|{mtime_ns}{'|' + look if look else ''}".encode()).hexdigest()


class DiskCache:
    def __init__(self, root: Path, quality: int = 85, limit_bytes: int = 0):
        self.root = Path(root)
        self.quality = quality
        self.limit_bytes = limit_bytes  # 0 = unlimited
        self._trim_lock = threading.Lock()

    def path_for(self, key: str, level: str) -> Path:
        return self.root / level / key[:2] / f"{key}.jpg"

    def load(self, key: str, level: str) -> QImage | None:
        p = self.path_for(key, level)
        try:
            st = p.stat()
        except OSError:
            return None
        image = QImage(str(p))
        if image.isNull():
            return None
        if time.time() - st.st_mtime > _TOUCH_AFTER:
            try:
                os.utime(p)
            except OSError:
                pass
        return image

    def store(self, key: str, level: str, image: QImage) -> None:
        p = self.path_for(key, level)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.{id(image)}.tmp")
        if image.save(str(tmp), "JPG", self.quality):
            os.replace(tmp, p)
        else:
            tmp.unlink(missing_ok=True)

    def _files(self, levels=LEVELS):
        """(path, size, mtime) of every cached JPEG."""
        for level in levels:
            base = self.root / level
            if not base.is_dir():
                continue
            for sub in os.scandir(base):
                if not sub.is_dir():
                    continue
                for f in os.scandir(sub.path):
                    if f.name.endswith(".jpg"):
                        try:
                            st = f.stat()
                        except OSError:
                            continue
                        yield f.path, st.st_size, st.st_mtime

    def usage(self) -> dict[str, tuple[int, int]]:
        """level -> (files, bytes)."""
        out = {}
        for level in LEVELS:
            sizes = [size for _, size, _ in self._files((level,))]
            out[level] = (len(sizes), sum(sizes))
        return out

    def size_bytes(self) -> int:
        return sum(s for _, s, _ in self._files())

    def trim(self, limit_bytes: int | None = None) -> int:
        """Delete least recently used files until the cache is under 90% of the limit. Returns bytes freed."""
        limit = self.limit_bytes if limit_bytes is None else limit_bytes
        if not limit or not self._trim_lock.acquire(blocking=False):
            return 0
        try:
            files = list(self._files())
            total = sum(s for _, s, _ in files)
            if total <= limit:
                return 0
            freed = 0
            for path, size, _ in sorted(files, key=lambda f: f[2]):
                if total - freed <= limit * 0.9:
                    break
                try:
                    os.remove(path)
                    freed += size
                except OSError:
                    pass
            return freed
        finally:
            self._trim_lock.release()

    def clear(self) -> None:
        """Remove every cached thumbnail and preview. Only our own level folders are touched."""
        for level in LEVELS:
            shutil.rmtree(self.root / level, ignore_errors=True)
