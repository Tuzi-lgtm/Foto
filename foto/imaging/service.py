"""Asynchronous thumbnail / preview loading.

UI code calls `get()`; a cached image comes back immediately, otherwise a
background job is queued and `ready` fires when it lands. The newest request
runs first, so whatever is on screen right now wins over earlier scrolling.
"""

from __future__ import annotations

import os
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage

from foto.catalog import ImageRecord
from foto.imaging import decode as dec
from foto.imaging.cache import DiskCache, cache_key

# How many decoded images of each level to keep in memory.
MEMORY_LIMITS = {dec.THUMB: 800, dec.PREVIEW: 8, dec.FULL: 2}
# Loupe requests jump ahead of grid thumbnails.
_LEVEL_BOOST = {dec.THUMB: 0, dec.PREVIEW: 1_000_000, dec.FULL: 2_000_000}


class _Signals(QObject):
    done = Signal(str, str, int, QImage)  # key, level, image_id, image
    failed = Signal(str, str, int, str)


class _Job(QRunnable):
    def __init__(self, signals: _Signals, cache: DiskCache, rec: ImageRecord, key: str, level: str):
        super().__init__()
        self.signals, self.cache, self.rec, self.key, self.level = signals, cache, rec, key, level

    def run(self) -> None:
        try:
            image = None if self.level == dec.FULL else self.cache.load(self.key, self.level)
            if image is None:
                image = dec.decode(self.rec.path, self.level)
                if self.level != dec.FULL:
                    self.cache.store(self.key, self.level, image)
                if self.level == dec.PREVIEW and self.cache.load(self.key, dec.THUMB) is None:
                    self.cache.store(self.key, dec.THUMB, dec.fit(image, dec.MAX_EDGE[dec.THUMB]))
            self.signals.done.emit(self.key, self.level, self.rec.id, image)
        except Exception as exc:  # decode errors must not kill the pool thread
            self.signals.failed.emit(self.key, self.level, self.rec.id, str(exc))


class ImageService(QObject):
    ready = Signal(int, str, QImage)  # image_id, level, image
    failed = Signal(int, str, str)  # image_id, level, message

    def __init__(self, cache: DiskCache, parent: QObject | None = None):
        super().__init__(parent)
        self.cache = cache
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(2, (os.cpu_count() or 4) - 1))
        self._memory: dict[str, OrderedDict[str, QImage]] = {lvl: OrderedDict() for lvl in dec.LEVELS}
        self._pending: set[tuple[str, str]] = set()
        self._broken: set[tuple[str, str]] = set()
        self._counter = 0
        self._signals = _Signals()
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)

    def get(self, rec: ImageRecord, level: str, request: bool = True) -> QImage | None:
        key = cache_key(rec.path, rec.file_size, rec.mtime_ns)
        mem = self._memory[level]
        if key in mem:
            mem.move_to_end(key)
            return mem[key]
        if request:
            self.request(rec, level, key)
        return None

    def best_available(self, rec: ImageRecord) -> tuple[QImage | None, str | None]:
        """Highest-resolution image already in memory, without queueing anything."""
        for level in reversed(dec.LEVELS):
            image = self.get(rec, level, request=False)
            if image is not None:
                return image, level
        return None, None

    def request(self, rec: ImageRecord, level: str, key: str | None = None) -> None:
        key = key or cache_key(rec.path, rec.file_size, rec.mtime_ns)
        if (key, level) in self._pending or (key, level) in self._broken:
            return
        self._pending.add((key, level))
        self._counter += 1
        self.pool.start(_Job(self._signals, self.cache, rec, key, level), self._counter + _LEVEL_BOOST[level])

    def shutdown(self) -> None:
        self.pool.clear()
        self.pool.waitForDone(5000)

    def _on_done(self, key: str, level: str, image_id: int, image: QImage) -> None:
        self._pending.discard((key, level))
        mem = self._memory[level]
        mem[key] = image
        while len(mem) > MEMORY_LIMITS[level]:
            mem.popitem(last=False)
        self.ready.emit(image_id, level, image)

    def _on_failed(self, key: str, level: str, image_id: int, message: str) -> None:
        self._pending.discard((key, level))
        self._broken.add((key, level))
        self.failed.emit(image_id, level, message)
