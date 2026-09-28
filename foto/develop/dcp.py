"""DNG camera profiles (.dcp): parsing and finding the installed ones.

A .dcp is a tiny TIFF ("IIRC" magic) holding the DNG profile tags: colour
and forward matrices for two calibration illuminants, optional hue/sat map
and look table, a tone curve and an exposure offset. Lightroom / Camera Raw
install them per camera; Foto reads them in place and never copies them.
"""

from __future__ import annotations

import os
import re
import struct
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

# TIFF tag numbers (DNG spec 1.6).
UNIQUE_CAMERA_MODEL = 50708
COLOR_MATRIX = (50721, 50722)
CALIBRATION_ILLUMINANT = (50778, 50779)
PROFILE_NAME = 50936
HUE_SAT_MAP_DIMS = 50937
HUE_SAT_MAP_DATA = (50938, 50939)
PROFILE_TONE_CURVE = 50940
FORWARD_MATRIX = (50964, 50965)
LOOK_TABLE_DIMS = 50981
LOOK_TABLE_DATA = 50982
HUE_SAT_MAP_ENCODING = 51107
LOOK_TABLE_ENCODING = 51108
BASELINE_EXPOSURE_OFFSET = 51109
DEFAULT_BLACK_RENDER = 51110

_TYPE = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8), 7: ("B", 1),
         8: ("h", 2), 9: ("i", 4), 10: ("ii", 8), 11: ("f", 4), 12: ("d", 8)}

# EXIF LightSource codes -> correlated colour temperature (K).
ILLUMINANT_CCT = {
    1: 5500, 2: 4150, 3: 2850, 4: 5500, 9: 5500, 10: 6500, 11: 7500, 12: 6400, 13: 5000, 14: 4150,
    15: 3450, 16: 2940, 17: 2856, 18: 4874, 19: 6774, 20: 5503, 21: 6504, 22: 7504, 23: 5003, 24: 3200,
}


@dataclass
class HueSatTable:
    """(hue, sat, val) divisions -> (hue shift in degrees, sat scale, val scale)."""

    hue_divs: int
    sat_divs: int
    val_divs: int
    data: np.ndarray  # float32 (val, hue, sat, 3)
    srgb_encoded: bool = False


@dataclass
class Profile:
    name: str
    camera: str
    illuminants: tuple[int, int | None]
    color_matrices: list[np.ndarray]  # XYZ -> camera, per illuminant
    forward_matrices: list[np.ndarray] = field(default_factory=list)  # white-balanced camera -> XYZ D50
    hue_sat_maps: list[HueSatTable] = field(default_factory=list)
    look_table: HueSatTable | None = None
    tone_curve: np.ndarray | None = None  # (N, 2) x -> y, both 0..1
    baseline_exposure_offset: float = 0.0
    black_render_none: bool = False
    path: str = ""

    @property
    def temperatures(self) -> tuple[float, float | None]:
        a, b = self.illuminants
        return ILLUMINANT_CCT.get(a, 5000), (ILLUMINANT_CCT.get(b, 6500) if b else None)


def _read_tags(blob: bytes) -> dict[int, object]:
    order = {b"II": "<", b"MM": ">"}.get(blob[:2])
    if order is None or struct.unpack(order + "H", blob[2:4])[0] not in (0x4352, 42):  # "RC" or TIFF
        raise ValueError("not a DNG camera profile")
    ifd = struct.unpack(order + "I", blob[4:8])[0]
    count = struct.unpack(order + "H", blob[ifd : ifd + 2])[0]
    tags: dict[int, object] = {}
    for i in range(count):
        entry = ifd + 2 + 12 * i
        tag, typ, n = struct.unpack(order + "HHI", blob[entry : entry + 8])
        if typ not in _TYPE:
            continue
        fmt, size = _TYPE[typ]
        nbytes = size * n
        pos = entry + 8 if nbytes <= 4 else struct.unpack(order + "I", blob[entry + 8 : entry + 12])[0]
        raw = blob[pos : pos + nbytes]
        if typ == 2:
            tags[tag] = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        elif typ in (5, 10):
            parts = struct.unpack(order + fmt[0] * (2 * n), raw)
            tags[tag] = np.array([a / b if b else 0.0 for a, b in zip(parts[::2], parts[1::2])], np.float64)
        else:
            tags[tag] = np.frombuffer(raw, dtype=np.dtype(order + fmt)).astype(
                np.float32 if typ in (11, 12) else np.int64)
    return tags


def _table(dims, data, encoding) -> HueSatTable | None:
    if dims is None or data is None:
        return None
    h, s, v = (int(x) for x in dims)
    v = max(v, 1)
    if data.size != h * s * v * 3:
        return None
    return HueSatTable(h, s, v, np.asarray(data, np.float32).reshape(v, h, s, 3), bool(encoding is not None and encoding[0] == 1))


def load_profile(path: str | os.PathLike) -> Profile:
    tags = _read_tags(Path(path).read_bytes())

    def matrix(tag):
        m = tags.get(tag)
        return None if m is None or len(m) != 9 else np.asarray(m, np.float64).reshape(3, 3)

    illum = tags.get(CALIBRATION_ILLUMINANT[0])
    illum2 = tags.get(CALIBRATION_ILLUMINANT[1])
    cms = [m for m in (matrix(COLOR_MATRIX[0]), matrix(COLOR_MATRIX[1])) if m is not None]
    if not cms:
        raise ValueError(f"{path}: no ColorMatrix")
    fms = [m for m in (matrix(FORWARD_MATRIX[0]), matrix(FORWARD_MATRIX[1])) if m is not None]
    hsm_dims = tags.get(HUE_SAT_MAP_DIMS)
    hsm_enc = tags.get(HUE_SAT_MAP_ENCODING)
    maps = [t for t in (_table(hsm_dims, tags.get(HUE_SAT_MAP_DATA[0]), hsm_enc),
                        _table(hsm_dims, tags.get(HUE_SAT_MAP_DATA[1]), hsm_enc)) if t is not None]
    curve = tags.get(PROFILE_TONE_CURVE)
    offset = tags.get(BASELINE_EXPOSURE_OFFSET)
    black = tags.get(DEFAULT_BLACK_RENDER)
    return Profile(
        name=str(tags.get(PROFILE_NAME, Path(path).stem)),
        camera=str(tags.get(UNIQUE_CAMERA_MODEL, "")),
        illuminants=(int(illum[0]) if illum is not None else 21, int(illum2[0]) if illum2 is not None and len(cms) > 1 else None),
        color_matrices=cms,
        forward_matrices=fms if len(fms) == len(cms) else [],
        hue_sat_maps=maps,
        look_table=_table(tags.get(LOOK_TABLE_DIMS), tags.get(LOOK_TABLE_DATA), tags.get(LOOK_TABLE_ENCODING)),
        tone_curve=np.asarray(curve, np.float64).reshape(-1, 2) if curve is not None and len(curve) >= 4 else None,
        baseline_exposure_offset=float(offset[0]) if offset is not None else 0.0,
        black_render_none=bool(black is not None and black[0] == 1),
        path=str(path),
    )


# -- finding installed profiles ------------------------------------------

def profile_dirs() -> list[Path]:
    """Where Lightroom / Camera Raw / DNG Converter install camera profiles."""
    if sys.platform == "darwin":
        roots = [Path("/Library/Application Support/Adobe/CameraRaw/CameraProfiles"),
                 Path.home() / "Library/Application Support/Adobe/CameraRaw/CameraProfiles"]
    else:
        roots = [Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Adobe/CameraRaw/CameraProfiles",
                 Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Adobe/CameraRaw/CameraProfiles"]
    return [r for r in roots if r.is_dir()]


_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}


def camera_key(name: str) -> str:
    """Normalise camera names so EXIF and Adobe spellings meet:
    "Canon EOS R5m2" and "Canon EOS R5 Mark II" -> "canoneosr5m2"."""
    s = name.lower()
    s = re.sub(r"\bmark\s*(ii|iii|iv|vi|v)\b", lambda m: "m" + _ROMAN[m.group(1)], s)
    s = re.sub(r"\bmk\s*(ii|iii|iv|vi|v)\b", lambda m: "m" + _ROMAN[m.group(1)], s)
    return re.sub(r"[^a-z0-9]", "", s)


@lru_cache(maxsize=1)
def _index(dirs: tuple[Path, ...]) -> dict[str, dict[str, str]]:
    """camera_key -> {profile name -> path}. Names come from the file name ("<camera> <profile>.dcp")."""
    index: dict[str, dict[str, str]] = {}
    for root in dirs:
        for path in root.rglob("*.dcp"):
            camera = path.parent.name if path.parent.parent.name == "Camera" else None
            stem = path.stem
            if camera and stem.startswith(camera + " "):
                cam, prof = camera, stem[len(camera) + 1:]
            elif stem.endswith(" Adobe Standard"):
                cam, prof = stem[: -len(" Adobe Standard")], "Adobe Standard"
            else:
                continue
            index.setdefault(camera_key(cam), {})[prof] = str(path)
    return index


def installed_profiles(make: str | None, model: str | None) -> dict[str, str]:
    """{profile name: path} available for a camera, e.g. {"Camera Neutral": ..., "Adobe Standard": ...}."""
    if not model:
        return {}
    index = _index(tuple(profile_dirs()))
    for name in (model, f"{make} {model}" if make else model):
        found = index.get(camera_key(name))
        if found:
            return dict(found)
    return {}


@lru_cache(maxsize=16)
def cached_profile(path: str) -> Profile:
    return load_profile(path)
