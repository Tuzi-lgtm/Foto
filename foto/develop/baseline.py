"""Baseline exposure: the camera-specific brightness offset Adobe applies to raws.

DNG files carry it (BaselineExposure). For native raws Adobe keeps a private
per-camera table, so Foto keeps its own, measured against Lightroom renders.
Canon Highlight Tone Priority underexposes the raw to protect highlights;
Lightroom brightens those files again, so HTP adds to the baseline.
"""

from __future__ import annotations

import io
from functools import lru_cache

from foto.develop.dcp import camera_key

# camera_key -> baseline (EV) without HTP, fitted so renders match Lightroom's (Camera Neutral,
# default settings), with Foto's highlight shoulder. They land 0.05-0.2 EV above Adobe's DNG values.
BASELINES = {
    "canoneos5dm3": 0.30,  # DNG: 0.25
    "canoneosr5m2": 0.55,  # DNG: 1.35 with HTP (= 0.35 + 1.0)
}
HTP_EXTRA = 1.0  # Lightroom brightens Highlight Tone Priority files by a stop
DNG_OFFSET = 0.1  # Lightroom renders 0.05-0.2 EV above a DNG's own BaselineExposure


def canon_htp(path: str) -> int:
    """Canon Highlight Tone Priority from the maker notes: 0 off, 1 on, 2 enhanced (-1 if unknown)."""
    import exifread

    try:
        if path.lower().endswith(".cr3"):
            from foto.importer import cr3_block

            block = cr3_block(path, b"CMT3")
            tags = exifread.process_file(io.BytesIO(block), details=True) if block else {}
            opt = tags.get("Image Tag 0x4018")
        else:
            with open(path, "rb") as fh:
                tags = exifread.process_file(fh, details=True, extract_thumbnail=False)
            opt = tags.get("MakerNote Tag 0x4018")
        values = list(opt.values) if opt is not None else []
        return int(values[3]) if len(values) > 3 else -1
    except Exception:
        return -1


def dng_baseline(path: str) -> float | None:
    import exifread

    try:
        with open(path, "rb") as fh:
            tags = exifread.process_file(fh, details=False, extract_thumbnail=False)
        tag = tags.get("Image Tag 0xC62A") or tags.get("Image BaselineExposure")
        if tag is None:
            return None
        v = tag.values[0]
        return float(v.num) / float(v.den) if hasattr(v, "num") else float(v)
    except Exception:
        return None


@lru_cache(maxsize=512)
def baseline_exposure(path: str, make: str | None, model: str | None) -> float:
    if path.lower().endswith(".dng"):
        found = dng_baseline(path)
        if found is not None:
            return found + DNG_OFFSET
    key = camera_key(model or "")
    base = BASELINES.get(key, 0.0)
    if (make or "").lower().startswith("canon") and canon_htp(path) > 0:
        base += HTP_EXTRA
    return base
