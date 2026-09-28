"""Glue between catalog records and the render pipeline: profiles, render params, background decoding."""

from __future__ import annotations

import os
from collections import OrderedDict

import numpy as np
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from foto.catalog import ImageRecord
from foto.develop import pipeline as pl
from foto.develop.baseline import baseline_exposure
from foto.develop.dcp import Profile, cached_profile, installed_profiles
from foto.formats import is_raw

DEFAULT_PROFILE = "Camera Neutral"
DISPLAY_ONLY = {"rotate"}  # edit settings applied when displaying, not when rendering


def develop_settings(edit: dict) -> dict:
    """The part of a photo's edit settings that changes how its raw renders."""
    return {k: v for k, v in edit.items() if k not in DISPLAY_ONLY}
PROFILE_FALLBACKS = ("Adobe Standard",)
HALF, FULL = "half", "full"


def available_profiles(rec: ImageRecord) -> dict[str, str]:
    return installed_profiles(rec.make, rec.model)


def resolve_profile(rec: ImageRecord, name: str = DEFAULT_PROFILE) -> Profile | None:
    """The named profile for this camera, else a fallback, else None (LibRaw matrix)."""
    found = available_profiles(rec)
    for candidate in (name, *PROFILE_FALLBACKS):
        if candidate in found:
            return cached_profile(found[candidate])
    return None


def render_params_for(rec: ImageRecord, raw: pl.LinearRaw, settings: dict | None = None) -> pl.RenderParams:
    settings = settings or {}
    profile = resolve_profile(rec, settings.get("profile", DEFAULT_PROFILE))
    return pl.render_params(profile, raw.as_shot_neutral, exposure=float(settings.get("exposure", 0.0)),
                            baseline=raw.baseline, camera_matrix=raw.camera_matrix)


def profile_label(rec: ImageRecord, settings: dict | None = None) -> str:
    profile = resolve_profile(rec, (settings or {}).get("profile", DEFAULT_PROFILE))
    return profile.name if profile else "Camera matrix (no profile installed)"


class _Signals(QObject):
    done = Signal(int, str, object)  # image_id, level, LinearRaw
    failed = Signal(int, str, str)


class _Decode(QRunnable):
    def __init__(self, signals: _Signals, rec: ImageRecord, level: str):
        super().__init__()
        self.signals, self.rec, self.level = signals, rec, level

    def run(self) -> None:
        try:
            raw = pl.decode_linear(self.rec.path, half_size=self.level == HALF)
            raw.baseline = baseline_exposure(self.rec.path, self.rec.make, self.rec.model)
            self.signals.done.emit(self.rec.id, self.level, raw)
        except Exception as exc:
            self.signals.failed.emit(self.rec.id, self.level, str(exc))


class RawLoader(QObject):
    """Decodes linear raws off the UI thread and keeps the last few in memory (they are big)."""

    ready = Signal(int, str, object)  # image_id, level, LinearRaw
    failed = Signal(int, str, str)
    LIMITS = {HALF: 3, FULL: 1}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(1, min(3, (os.cpu_count() or 2) // 4)))
        self._memory = {HALF: OrderedDict(), FULL: OrderedDict()}
        self._pending: set[tuple[int, str]] = set()
        self._signals = _Signals()
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)

    @staticmethod
    def supports(rec: ImageRecord | None) -> bool:
        return rec is not None and is_raw(rec.path)

    def get(self, rec: ImageRecord, level: str, request: bool = True) -> pl.LinearRaw | None:
        mem = self._memory[level]
        if rec.id in mem:
            mem.move_to_end(rec.id)
            return mem[rec.id]
        if request and (rec.id, level) not in self._pending:
            self._pending.add((rec.id, level))
            self.pool.start(_Decode(self._signals, rec, level), 1 if level == FULL else 0)
        return None

    def shutdown(self) -> None:
        self.pool.clear()
        self.pool.waitForDone(10000)

    def _on_done(self, image_id: int, level: str, raw: pl.LinearRaw) -> None:
        self._pending.discard((image_id, level))
        mem = self._memory[level]
        mem[image_id] = raw
        while len(mem) > self.LIMITS[level]:
            mem.popitem(last=False)
        self.ready.emit(image_id, level, raw)

    def _on_failed(self, image_id: int, level: str, message: str) -> None:
        self._pending.discard((image_id, level))
        self.failed.emit(image_id, level, message)


def native_size(raw: pl.LinearRaw, full_size: tuple[int, int] | None) -> tuple[int, int] | None:
    """Full-resolution (w, h) for the view's 1:1 maths, oriented like the decoded pixels."""
    if full_size is None:
        return None
    h, w = raw.rgb.shape[:2]
    fw, fh = full_size
    return (fw, fh) if (w >= h) == (fw >= fh) else (fh, fw)


def to_uint16(raw: pl.LinearRaw) -> np.ndarray:
    rgb = raw.rgb
    return rgb if rgb.dtype == np.uint16 else (np.clip(rgb, 0, 1) * 65535).astype(np.uint16)


# -- rendered thumbnails (CPU) -------------------------------------------------

RENDER_VERSION = 1  # bump when the pipeline's look changes, so cached renders are rebuilt


def render_signature(rec: ImageRecord, settings: dict | None = None) -> str:
    """Identifies a rendered look: pipeline version + develop settings. Part of the cache key."""
    import hashlib
    import json

    blob = json.dumps(settings or {}, sort_keys=True)
    return f"r{RENDER_VERSION}:{hashlib.sha1(blob.encode()).hexdigest()[:12]}"


def _downsample(rgb: np.ndarray, max_edge: int) -> np.ndarray:
    """Area-average a uint16 linear image so its long edge is about max_edge (float32, 0..1)."""
    h, w = rgb.shape[:2]
    f = max(1, int(max(h, w) // max_edge))
    h2, w2 = h // f * f, w // f * f
    x = rgb[:h2, :w2].astype(np.float32).reshape(h2 // f, f, w2 // f, f, 3).mean(axis=(1, 3)) / 65535.0
    return x


def render_thumbnail(rec: ImageRecord, max_edge: int, settings: dict | None = None):
    """Camera Neutral render of a raw at thumbnail size, as a QImage (CPU; runs in worker threads)."""
    from foto.imaging.decode import array_to_qimage, fit

    raw = pl.decode_linear(rec.path, half_size=True)
    raw.baseline = baseline_exposure(rec.path, rec.make, rec.model)
    small = _downsample(to_uint16(raw), max_edge)
    params = render_params_for(rec, raw, settings)
    out = pl.srgb_encode(pl.render(small, params))
    return fit(array_to_qimage((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)), max_edge)
