"""Decode originals into display images.

Raw files use the camera's embedded JPEG where it is big enough (fast), and
fall back to a half-size LibRaw decode otherwise. Everything returned is an
8-bit, camera-oriented QImage; user edits such as rotation are not applied.
Safe to call from worker threads.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QTransform

from foto.formats import is_raw

THUMB, PREVIEW, FULL = "thumb", "preview", "full"
LEVELS = (THUMB, PREVIEW, FULL)
MAX_EDGE = {THUMB: 320, PREVIEW: 2560, FULL: None}

# LibRaw "flip" -> clockwise degrees
_FLIP_TO_DEGREES = {0: 0, 3: 180, 5: 270, 6: 90}


class DecodeError(Exception):
    pass


def decode(path: str, level: str) -> QImage:
    max_edge = MAX_EDGE[level]
    image = _decode_raw(path, max_edge) if is_raw(path) else _read(path, max_edge, auto_transform=True)
    if image.isNull():
        raise DecodeError(f"could not decode {path}")
    return fit(image, max_edge)


def fit(image: QImage, max_edge: int | None) -> QImage:
    if max_edge and max(image.width(), image.height()) > max_edge:
        return image.scaled(max_edge, max_edge, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return image


def _read(source, max_edge: int | None, auto_transform: bool) -> QImage:
    """QImageReader with DCT-domain downscaling for JPEGs (very fast thumbnails)."""
    reader = QImageReader(source)
    reader.setAutoTransform(auto_transform)
    size = reader.size()
    if max_edge and size.isValid() and max(size.width(), size.height()) > max_edge * 2:
        # Decode at ~2x target so the final smooth downscale stays sharp.
        s = (max_edge * 2) / max(size.width(), size.height())
        reader.setScaledSize(QSize(max(1, round(size.width() * s)), max(1, round(size.height() * s))))
    return reader.read()


def _decode_raw(path: str, max_edge: int | None) -> QImage:
    import rawpy

    try:
        raw = rawpy.imread(path)
    except Exception as exc:
        raise DecodeError(f"{path}: {exc}") from exc
    with raw:
        flip = raw.sizes.flip
        full_edge = max(raw.sizes.width, raw.sizes.height)
        embedded = _embedded(raw, max_edge)
        # Use the embedded preview if it covers the request (or is near full size).
        wanted = max_edge or full_edge
        if embedded is not None and max(embedded.width(), embedded.height()) * 2 >= min(wanted, full_edge):
            return _rotate(embedded, _FLIP_TO_DEGREES.get(flip, 0))
        try:
            rgb = raw.postprocess(
                half_size=max_edge is not None and max_edge * 2 <= full_edge,
                use_camera_wb=True,
                output_bps=8,
                no_auto_bright=False,
            )
        except Exception as exc:
            if embedded is not None:
                return _rotate(embedded, _FLIP_TO_DEGREES.get(flip, 0))
            raise DecodeError(f"{path}: {exc}") from exc
    return array_to_qimage(rgb)  # postprocess output is already oriented


def _embedded(raw, max_edge: int | None) -> QImage | None:
    import rawpy

    try:
        thumb = raw.extract_thumb()
    except Exception:
        return None
    if thumb.format == rawpy.ThumbFormat.JPEG:
        buf = QBuffer()
        buf.setData(QByteArray(thumb.data))
        buf.open(QIODevice.ReadOnly)
        image = _read(buf, max_edge, auto_transform=False)
        return None if image.isNull() else image
    if thumb.format == rawpy.ThumbFormat.BITMAP:
        return array_to_qimage(thumb.data)
    return None


def array_to_qimage(rgb: np.ndarray) -> QImage:
    rgb = np.ascontiguousarray(rgb[..., :3], dtype=np.uint8)
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


def _rotate(image: QImage, degrees: int) -> QImage:
    if degrees % 360 == 0:
        return image
    return image.transformed(QTransform().rotate(degrees))
