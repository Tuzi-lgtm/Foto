"""Raw render pipeline: linear camera RGB -> display-referred linear sRGB.

Follows the DNG SDK's reference render, which is what Camera Raw / Lightroom
profiles are built for:

    camera RGB --clip at camera white--> x camera->ProPhoto (white balance + profile matrices)
      -> hue/sat map -> exposure (baseline + user) -> look table -> tone curve (hue preserving)
      -> ProPhoto -> linear sRGB

This module is the CPU reference (thumbnails, export, tests). The viewer runs
the same maths as a GLSL shader (develop/shader.py) fed from `RenderParams`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from foto.develop import colormath as cm
from foto.develop.dcp import HueSatTable, Profile

CURVE_SIZE = 4096


@dataclass
class LinearRaw:
    """Demosaiced, black-subtracted camera RGB scaled so sensor clip = 1.0, oriented like the photo."""

    rgb: np.ndarray  # float32 (H, W, 3) or uint16 (H, W, 3) = value * 65535
    as_shot_neutral: np.ndarray  # camera RGB of the as-shot white, max 1
    camera_matrix: np.ndarray | None = None  # LibRaw's XYZ->camera (fallback when no profile)
    baseline: float = 0.0  # camera baseline exposure, EV (see develop/baseline.py)


def decode_linear(path: str, half_size: bool = True) -> LinearRaw:
    """Linear camera RGB via LibRaw: no white balance, no colour conversion, no gamma, no auto-brightening."""
    import rawpy

    with rawpy.imread(path) as raw:
        # Scale to the sensor's real saturation point (Canon reports it separately from the
        # container's maximum), so clipped highlights land exactly on 1.0 like in Camera Raw.
        sat = raw.camera_white_level_per_channel
        sat = int(min(sat)) if sat is not None and 0 < min(sat) < raw.white_level else None
        rgb = raw.postprocess(
            user_sat=sat,
            output_color=rawpy.ColorSpace.raw,
            gamma=(1, 1),
            no_auto_bright=True,
            output_bps=16,
            user_wb=[1.0, 1.0, 1.0, 1.0],
            half_size=half_size,
            demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
            highlight_mode=rawpy.HighlightMode.Clip,
        )
        mul = np.asarray(raw.camera_whitebalance[:3], float)
        if not np.all(mul > 0):
            mul = np.asarray(raw.daylight_whitebalance[:3], float)
        neutral = 1.0 / np.where(mul > 0, mul, 1.0)
        xyz_cam = np.asarray(raw.rgb_xyz_matrix[:3], float)
    return LinearRaw(rgb, neutral / neutral.max(), xyz_cam if np.any(xyz_cam) else None)


def spline_lut(points: np.ndarray, size: int = CURVE_SIZE) -> np.ndarray:
    """Natural cubic spline through (x, y) points, sampled on [0, 1]."""
    x, y = points[:, 0].astype(float), points[:, 1].astype(float)
    n = len(x)
    xs = np.linspace(0.0, 1.0, size)
    if n < 3:
        return np.interp(xs, x, y).astype(np.float32)
    h = np.diff(x)
    a = np.zeros((n, n))
    rhs = np.zeros(n)
    a[0, 0] = a[-1, -1] = 1.0
    for i in range(1, n - 1):
        a[i, i - 1], a[i, i], a[i, i + 1] = h[i - 1], 2 * (h[i - 1] + h[i]), h[i]
        rhs[i] = 6 * ((y[i + 1] - y[i]) / h[i] - (y[i] - y[i - 1]) / h[i - 1])
    m = np.linalg.solve(a, rhs)
    i = np.clip(np.searchsorted(x, xs) - 1, 0, n - 2)
    t = xs - x[i]
    hi = h[i]
    out = (m[i] * (x[i + 1] - xs) ** 3 + m[i + 1] * t ** 3) / (6 * hi) \
        + (y[i] / hi - m[i] * hi / 6) * (x[i + 1] - xs) + (y[i + 1] / hi - m[i + 1] * hi / 6) * t
    return np.clip(out, 0.0, 1.0).astype(np.float32)


@dataclass
class RenderParams:
    camera_white: np.ndarray  # clip level per camera channel
    camera_to_prophoto: np.ndarray  # 3x3
    hue_sat_map: HueSatTable | None
    exposure: float  # stops: baseline + profile offset + user
    look_table: HueSatTable | None
    tone_curve: np.ndarray  # CURVE_SIZE samples on [0, 1]
    output: np.ndarray  # ProPhoto -> linear sRGB

    @property
    def white(self) -> float:
        """Exposure ramp: linear value that maps to 1.0."""
        return 2.0 ** -self.exposure


def _blend_table(tables: list[HueSatTable], g: float) -> HueSatTable | None:
    if not tables:
        return None
    if len(tables) == 1:
        return tables[0]
    a, b = tables
    return HueSatTable(a.hue_divs, a.sat_divs, a.val_divs, (g * a.data + (1 - g) * b.data).astype(np.float32),
                       a.srgb_encoded)


def render_params(profile: Profile | None, neutral, exposure: float = 0.0, baseline: float = 0.0,
                  camera_matrix: np.ndarray | None = None) -> RenderParams:
    """Everything the render needs for a profile + white balance + exposure."""
    neutral = np.asarray(neutral, float) / np.max(neutral)
    if profile is None:
        # No DCP: LibRaw's (Adobe-derived) matrix for the camera, as a single-illuminant profile.
        profile = Profile("Matrix", "", (21, None), [camera_matrix if camera_matrix is not None else np.eye(3)])
    white, cam_to_pp = cm.camera_to_prophoto(profile, neutral)
    temp, _ = cm.xy_to_temp_tint(cm.neutral_to_xy(profile, neutral))
    g = cm.illuminant_weight(temp, *profile.temperatures)
    curve = spline_lut(profile.tone_curve) if profile.tone_curve is not None else np.linspace(0, 1, CURVE_SIZE, dtype=np.float32)
    return RenderParams(
        camera_white=white,
        camera_to_prophoto=cam_to_pp,
        hue_sat_map=_blend_table(profile.hue_sat_maps, g),
        exposure=baseline + profile.baseline_exposure_offset + exposure,
        look_table=profile.look_table,
        tone_curve=curve,
        output=cm.PROPHOTO_TO_SRGB,
    )


# -- stages ----------------------------------------------------------------

def srgb_encode(x):
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(np.maximum(x, 0.0031308), 1 / 2.4) - 0.055)


def srgb_decode(x):
    return np.where(x <= 0.04045, x / 12.92, np.power((np.maximum(x, 0.04045) + 0.055) / 1.055, 2.4))


def rgb_to_hsv(rgb):
    """DNG hexcone HSV: h in [0, 6), s, v."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    v = np.max(rgb, axis=-1)
    mn = np.min(rgb, axis=-1)
    gap = v - mn
    s = np.where(v > 0, gap / np.where(v > 0, v, 1), 0.0)
    safe = np.where(gap > 0, gap, 1)
    h = np.where(r == v, (g - b) / safe, np.where(g == v, 2 + (b - r) / safe, 4 + (r - g) / safe))
    h = np.where(gap > 0, np.mod(h, 6.0), 0.0)
    return h, s, v


def hsv_to_rgb(h, s, v):
    h = np.mod(h, 6.0)
    i = np.floor(h)
    f = h - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    i = i.astype(int) % 6
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


def apply_hue_sat(rgb, table: HueSatTable):
    """DNG hue/sat map (RefBaselineHueSatMap): hue wraps, sat and val clamp, trilinear.

    Hue and saturation always come from the linear colour; an sRGB-encoded table only
    encodes the value (brightness) axis, both for the lookup and the value scale."""
    h, s, v = rgb_to_hsv(np.clip(rgb, 0.0, None))
    if table.srgb_encoded:
        v = srgb_encode(np.clip(v, 0.0, 1.0))
    hs = h * (table.hue_divs / 6.0)
    ss = s * (table.sat_divs - 1)
    h0 = np.floor(hs).astype(int) % table.hue_divs
    h1 = (h0 + 1) % table.hue_divs
    hf = hs - np.floor(hs)
    s0 = np.minimum(np.floor(ss).astype(int), max(table.sat_divs - 2, 0))
    sf = np.clip(ss - s0, 0.0, 1.0)
    s1 = np.minimum(s0 + 1, table.sat_divs - 1)
    if table.val_divs > 1:
        vs = np.clip(v, 0.0, 1.0) * (table.val_divs - 1)
        v0 = np.minimum(np.floor(vs).astype(int), table.val_divs - 2)
        vf = vs - v0
        v1 = v0 + 1
    else:
        v0 = v1 = np.zeros_like(h0)
        vf = np.zeros_like(hf)
    d = table.data

    def lerp_hs(vi):
        a = d[vi, h0, s0] * (1 - sf)[..., None] + d[vi, h0, s1] * sf[..., None]
        b = d[vi, h1, s0] * (1 - sf)[..., None] + d[vi, h1, s1] * sf[..., None]
        return a * (1 - hf)[..., None] + b * hf[..., None]

    e = lerp_hs(v0) * (1 - vf)[..., None] + lerp_hs(v1) * vf[..., None]
    h = h + e[..., 0] * (6.0 / 360.0)
    s = np.minimum(s * e[..., 1], 1.0)
    v = np.clip(v * e[..., 2], 0.0, 1.0)
    if table.srgb_encoded:
        v = srgb_decode(v)
    return hsv_to_rgb(h, s, v)


def apply_rgb_tone(rgb, lut: np.ndarray):
    """Hue-preserving tone curve (DNG RefBaselineRGBTone): curve the max and min, interpolate the middle."""
    x = np.clip(rgb, 0.0, 1.0)
    mx, mn = x.max(axis=-1, keepdims=True), x.min(axis=-1, keepdims=True)
    scale = len(lut) - 1
    cmx = np.interp(mx * scale, np.arange(len(lut)), lut)
    cmn = np.interp(mn * scale, np.arange(len(lut)), lut)
    span = mx - mn
    return np.where(span > 0, cmn + (cmx - cmn) * (x - mn) / np.where(span > 0, span, 1), cmx)


def render(raw_rgb: np.ndarray, p: RenderParams) -> np.ndarray:
    """Linear camera RGB (float 0..1 or uint16) -> display-referred linear sRGB in [0, 1]."""
    x = raw_rgb.astype(np.float32) / (65535.0 if raw_rgb.dtype == np.uint16 else 1.0)
    x = np.minimum(x, p.camera_white.astype(np.float32))
    x = np.clip(x @ p.camera_to_prophoto.T.astype(np.float32), 0.0, 1.0)
    if p.hue_sat_map is not None:
        x = apply_hue_sat(x, p.hue_sat_map)
    x = np.minimum(x / p.white, 1.0)
    if p.look_table is not None:
        x = apply_hue_sat(x, p.look_table)
    x = apply_rgb_tone(x, p.tone_curve)
    return np.clip(x @ p.output.T, 0.0, 1.0).astype(np.float32)
