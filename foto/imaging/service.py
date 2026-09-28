"""Asynchronous thumbnail / preview loading.

UI code calls `get()`; a cached image comes back immediately, otherwise a
background job is queued and `ready` fires when it lands. The newest request
runs first, so whatever is on screen right now wins over earlier scrolling.

Raw thumbnails are rendered by Foto's develop pipeline (so the grid looks like
the loupe and Develop). The first time, the camera's embedded JPEG stands in
until the render is done; it is shown but never cached as the thumbnail.
Views show raws larger than a thumbnail on the GPU, so raws have no previews.
"""

from __future__ import annotations

import os
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage

from foto.catalog import ImageRecord
from foto.formats import is_raw
from foto.imaging import decode as dec
from foto.imaging.cache import DiskCache, cache_key

# How many decoded images of each level to keep in memory.
MEMORY_LIMITS = {dec.THUMB: 800, dec.PREVIEW: 8, dec.FULL: 2}
# Loupe requests jump ahead of grid thumbnails.
_LEVEL_BOOST = {dec.THUMB: 0, dec.PREVIEW: 1_000_000, dec.FULL: 2_000_000}
# Check the disk cache against its size limit after this many loads.
TRIM_EVERY = 200


def auto_threads() -> int:
    return max(2, (os.cpu_count() or 4) - 1)


class _Signals(QObject):
    done = Signal(str, str, int, QImage)  # key, level, image_id, image
    provisional = Signal(str, str, int, QImage)  # a stand-in while the real image is made
    failed = Signal(str, str, int, str)


class _Job(QRunnable):
    def __init__(self, signals: _Signals, cache: DiskCache, rec: ImageRecord, key: str, level: str,
                 settings: dict | None = None):
        super().__init__()
        self.signals, self.cache, self.rec, self.key, self.level = signals, cache, rec, key, level
        self.settings = settings

    def run(self) -> None:
        if is_raw(self.rec.path) and self.level == dec.THUMB:
            self._raw_thumb()
            return
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

    def _raw_thumb(self) -> None:
        from foto.develop.engine import render_thumbnail

        image = self.cache.load(self.key, dec.THUMB)
        if image is not None:
            self.signals.done.emit(self.key, dec.THUMB, self.rec.id, image)
            return
        stand_in = None
        try:
            stand_in = dec.decode(self.rec.path, dec.THUMB)
            self.signals.provisional.emit(self.key, dec.THUMB, self.rec.id, stand_in)
        except Exception:
            pass
        try:
            image = render_thumbnail(self.rec, dec.MAX_EDGE[dec.THUMB], self.settings)
        except Exception as exc:
            if stand_in is None:
                self.signals.failed.emit(self.key, dec.THUMB, self.rec.id, str(exc))
                return
            image = stand_in  # LibRaw can't develop it, but the camera JPEG is better than nothing
        self.cache.store(self.key, dec.THUMB, image)
        self.signals.done.emit(self.key, dec.THUMB, self.rec.id, image)


class ImageService(QObject):
    ready = Signal(int, str, QImage)  # image_id, level, image
    failed = Signal(int, str, str)  # image_id, level, message

    def __init__(self, cache: DiskCache, parent: QObject | None = None, develop_settings=None):
        super().__init__(parent)
        self.cache = cache
        self.develop_settings = develop_settings or (lambda image_id: {})  # image_id -> develop settings
        self._looks: dict[int, str] = {}  # image_id -> render signature, for raws
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(auto_threads())
        self.memory_limits = dict(MEMORY_LIMITS)
        self._since_trim = TRIM_EVERY  # trim soon after startup
        self._memory: dict[str, OrderedDict[str, QImage]] = {lvl: OrderedDict() for lvl in dec.LEVELS}
        self._pending: set[tuple[str, str]] = set()
        self._broken: set[tuple[str, str]] = set()
        self._counter = 0
        self._signals = _Signals()
        self._signals.done.connect(self._on_done)
        self._signals.provisional.connect(self._on_provisional)
        self._signals.failed.connect(self._on_failed)

    def key_for(self, rec: ImageRecord) -> str:
        if not is_raw(rec.path):
            return cache_key(rec.path, rec.file_size, rec.mtime_ns)
        look = self._looks.get(rec.id)
        if look is None:
            from foto.develop.engine import render_signature

            look = self._looks[rec.id] = render_signature(rec, self.develop_settings(rec.id))
        return cache_key(rec.path, rec.file_size, rec.mtime_ns, look)

    def invalidate(self, image_id: int) -> None:
        """A raw's develop settings changed: its next thumbnail request renders the new look."""
        self._looks.pop(image_id, None)

    def get(self, rec: ImageRecord, level: str, request: bool = True) -> QImage | None:
        if level == dec.PREVIEW and is_raw(rec.path):
            level = dec.THUMB  # raws have no previews; views render them on the GPU
        key = self.key_for(rec)
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
        key = key or self.key_for(rec)
        if (key, level) in self._pending or (key, level) in self._broken:
            return
        self._pending.add((key, level))
        self._counter += 1
        settings = self.develop_settings(rec.id) if is_raw(rec.path) else None
        self.pool.start(_Job(self._signals, self.cache, rec, key, level, settings), self._counter + _LEVEL_BOOST[level])

    def set_threads(self, n: int) -> None:
        """Decoder threads; 0 = automatic."""
        self.pool.setMaxThreadCount(n or auto_threads())

    def set_cache(self, cache: DiskCache) -> None:
        """Switch disk caches (e.g. a new location). Images already in memory stay."""
        self.cache = cache
        self._since_trim = TRIM_EVERY

    def trim_cache(self) -> None:
        """Enforce the disk cache size limit in the background."""
        self._since_trim = 0
        cache = self.cache
        if cache.limit_bytes:
            self.pool.start(cache.trim, -1)

    def shutdown(self) -> None:
        self.pool.clear()
        self.pool.waitForDone(5000)

    def _on_done(self, key: str, level: str, image_id: int, image: QImage) -> None:
        self._pending.discard((key, level))
        mem = self._memory[level]
        mem[key] = image
        while len(mem) > self.memory_limits[level]:
            mem.popitem(last=False)
        if level != dec.FULL:
            self._since_trim += 1
            if self._since_trim >= TRIM_EVERY:
                self.trim_cache()
        self.ready.emit(image_id, level, image)

    def _on_provisional(self, key: str, level: str, image_id: int, image: QImage) -> None:
        """Show a stand-in now; the request stays pending until the real image arrives."""
        mem = self._memory[level]
        if key not in mem:
            mem[key] = image
            self.ready.emit(image_id, level, image)

    def _on_failed(self, key: str, level: str, image_id: int, message: str) -> None:
        self._pending.discard((key, level))
        self._broken.add((key, level))
        self.failed.emit(image_id, level, message)
