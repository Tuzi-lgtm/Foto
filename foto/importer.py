"""Folder import: scan, read metadata, add to catalog. Originals are only read."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator

import exifread

from foto.catalog import Catalog
from foto.formats import is_raw, is_supported

BATCH = 200


@dataclass
class ImportResult:
    scanned: int = 0
    added: int = 0
    skipped: int = 0
    failed: int = 0


def scan(root: str) -> Iterator[str]:
    """Supported files under root, sorted, skipping hidden files and folders."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.startswith(".") and is_supported(name):
                yield os.path.join(dirpath, name)


def import_folder(
    catalog: Catalog,
    root: str,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ImportResult:
    """Add every supported file under root. Already-catalogued paths are skipped."""
    root = os.path.normpath(os.path.abspath(root))
    known = catalog.known_paths()
    folder_ids: dict[str, int] = {}
    result = ImportResult()
    batch: list[dict] = []

    for path in scan(root):
        if cancelled and cancelled():
            break
        result.scanned += 1
        if progress:
            progress(result.scanned, path)
        if path in known:
            result.skipped += 1
            continue
        try:
            row = read_file(path)
        except OSError:
            result.failed += 1
            continue
        folder = os.path.dirname(path)
        if folder not in folder_ids:
            folder_ids[folder] = catalog.add_folder(folder)
        row["folder_id"] = folder_ids[folder]
        batch.append(row)
        if len(batch) >= BATCH:
            result.added += catalog.add_images(batch)
            batch.clear()

    if batch:
        result.added += catalog.add_images(batch)
    if not folder_ids:
        catalog.add_folder(root)
    return result


def read_file(path: str) -> dict:
    st = os.stat(path)
    row = {
        "path": path,
        "filename": os.path.basename(path),
        "ext": Path(path).suffix.lower(),
        "file_size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }
    row.update(read_metadata(path))
    if not row.get("capture_time"):
        row["capture_time"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
    return row


def read_metadata(path: str) -> dict:
    """Best-effort EXIF. Missing fields are simply absent."""
    meta: dict = {}
    try:
        with open(path, "rb") as fh:
            tags = exifread.process_file(fh, details=False, extract_thumbnail=False)
    except Exception:  # exifread raises assorted errors on odd files
        tags = {}

    def text(*keys: str) -> str | None:
        for k in keys:
            if k in tags:
                value = str(tags[k].values).strip() if hasattr(tags[k], "values") else str(tags[k])
                if value:
                    return value.strip("\x00 ")
        return None

    def number(*keys: str) -> float | None:
        for k in keys:
            if k in tags:
                vals = tags[k].values
                v = vals[0] if isinstance(vals, (list, tuple)) and vals else vals
                try:
                    return float(Fraction(str(v)))
                except (ValueError, ZeroDivisionError):
                    continue
        return None

    meta["make"] = text("Image Make")
    meta["model"] = text("Image Model")
    meta["lens"] = text("EXIF LensModel", "MakerNote LensModel")
    iso = number("EXIF ISOSpeedRatings", "EXIF PhotographicSensitivity")
    meta["iso"] = int(iso) if iso else None
    meta["shutter"] = number("EXIF ExposureTime")
    meta["aperture"] = number("EXIF FNumber")
    meta["focal"] = number("EXIF FocalLength")
    meta["capture_time"] = _exif_time(text("EXIF DateTimeOriginal", "Image DateTime"))
    w = number("EXIF ExifImageWidth", "Image ImageWidth")
    h = number("EXIF ExifImageLength", "Image ImageLength")
    meta["width"], meta["height"] = (int(w), int(h)) if w and h else (None, None)

    if is_raw(path):
        _fill_from_libraw(path, meta)
    else:
        _fill_pixel_size(path, meta)
    return {k: v for k, v in meta.items() if v is not None}


def _fill_from_libraw(path: str, meta: dict) -> None:
    """LibRaw knows formats exifread does not (e.g. CR3); use it to fill gaps."""
    try:
        import rawpy

        with rawpy.imread(path) as raw:
            sizes, other = raw.sizes, raw.other
            lens = raw.lens
    except Exception:
        return
    swap = sizes.flip in (5, 6)
    meta["width"] = sizes.height if swap else sizes.width
    meta["height"] = sizes.width if swap else sizes.height
    ts = other.timestamp
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts) if ts > 0 else None
    if not meta.get("capture_time") and ts and ts.year >= 1980:  # 0 = unknown
        meta["capture_time"] = ts.strftime("%Y-%m-%dT%H:%M:%S")
    meta["iso"] = meta.get("iso") or (int(other.iso_speed) if other.iso_speed else None)
    meta["shutter"] = meta.get("shutter") or (other.shutter_speed or None)
    meta["aperture"] = meta.get("aperture") or (other.aperture or None)
    meta["focal"] = meta.get("focal") or (other.focal_length or None)
    meta["lens"] = meta.get("lens") or (lens.model or None)


def _fill_pixel_size(path: str, meta: dict) -> None:
    """True pixel size from the file header (EXIF sizes are often missing or wrong)."""
    from PySide6.QtGui import QImageIOHandler, QImageReader

    reader = QImageReader(path)
    size = reader.size()
    if size.isValid():
        swap = bool(reader.transformation() & QImageIOHandler.TransformationRotate90)
        w, h = size.width(), size.height()
        meta["width"], meta["height"] = (h, w) if swap else (w, h)


def _exif_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
