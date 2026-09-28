"""Colour science for raw rendering, following the DNG specification / reference SDK.

- xy <-> temperature/tint uses Robertson's method with the DNG SDK's tint
  scale, so Temp/Tint values mean what they mean in Lightroom.
- Camera white balance is expressed as a camera-space "neutral"; the
  profile's two colour matrices are blended by the white's temperature.
"""

from __future__ import annotations

import math

import numpy as np

D50_XY = (0.3457, 0.3585)
D50_XYZ = np.array([0.9642, 1.0, 0.8249])

PROPHOTO_TO_XYZ_D50 = np.array([
    [0.7976749, 0.1351917, 0.0313534],
    [0.2880402, 0.7118741, 0.0000857],
    [0.0000000, 0.0000000, 0.8252100],
])
XYZ_D50_TO_PROPHOTO = np.linalg.inv(PROPHOTO_TO_XYZ_D50)
# Linear sRGB / Rec.709 primaries, Bradford-adapted to the D50 PCS (as in ICC sRGB).
XYZ_D50_TO_SRGB = np.array([
    [3.1338561, -1.6168667, -0.4906146],
    [-0.9787684, 1.9161415, 0.0334540],
    [0.0719453, -0.2289914, 1.4052427],
])
PROPHOTO_TO_SRGB = XYZ_D50_TO_SRGB @ PROPHOTO_TO_XYZ_D50

# Robertson isotemperature lines: (mired, u, v, slope).
_ROBERTSON = np.array([
    (0, 0.18006, 0.26352, -0.24341), (10, 0.18066, 0.26589, -0.25479), (20, 0.18133, 0.26846, -0.26876),
    (30, 0.18208, 0.27119, -0.28539), (40, 0.18293, 0.27407, -0.30470), (50, 0.18388, 0.27709, -0.32675),
    (60, 0.18494, 0.28021, -0.35156), (70, 0.18611, 0.28342, -0.37915), (80, 0.18740, 0.28668, -0.40955),
    (90, 0.18880, 0.28997, -0.44278), (100, 0.19032, 0.29326, -0.47888), (125, 0.19462, 0.30141, -0.58204),
    (150, 0.19962, 0.30921, -0.70471), (175, 0.20525, 0.31647, -0.84901), (200, 0.21142, 0.32312, -1.0182),
    (225, 0.21807, 0.32909, -1.2168), (250, 0.22511, 0.33439, -1.4512), (275, 0.23247, 0.33904, -1.7298),
    (300, 0.24010, 0.34308, -2.0637), (325, 0.24702, 0.34655, -2.4681), (350, 0.25591, 0.34951, -2.9641),
    (375, 0.26400, 0.35200, -3.5814), (400, 0.27218, 0.35407, -4.3633), (425, 0.28039, 0.35577, -5.3762),
    (450, 0.28863, 0.35714, -6.7262), (475, 0.29685, 0.35823, -8.5955), (500, 0.30505, 0.35907, -11.324),
    (525, 0.31320, 0.35968, -15.628), (550, 0.32129, 0.36011, -23.325), (575, 0.32931, 0.36038, -40.770),
    (600, 0.33724, 0.36051, -116.45),
])
TINT_SCALE = -3000.0


def xy_to_xyz(xy) -> np.ndarray:
    x, y = xy
    return np.array([x / y, 1.0, (1 - x - y) / y])


def xyz_to_xy(xyz) -> tuple[float, float]:
    s = float(np.sum(xyz))
    return (float(xyz[0] / s), float(xyz[1] / s)) if s > 0 else D50_XY


def xy_to_temp_tint(xy) -> tuple[float, float]:
    x, y = xy
    d = 1.5 - x + 6.0 * y
    u, v = 2.0 * x / d, 3.0 * y / d
    last_dt = last_du = last_dv = 0.0
    for i in range(1, len(_ROBERTSON)):
        du, dv = 1.0, _ROBERTSON[i, 3]
        n = math.hypot(du, dv)
        du, dv = du / n, dv / n
        uu, vv = u - _ROBERTSON[i, 1], v - _ROBERTSON[i, 2]
        dt = -uu * dv + vv * du
        if dt <= 0 or i == len(_ROBERTSON) - 1:
            dt = -min(dt, 0.0)
            f = 0.0 if i == 1 else dt / (last_dt + dt)
            temp = 1e6 / (_ROBERTSON[i - 1, 0] * f + _ROBERTSON[i, 0] * (1 - f))
            uu = u - (_ROBERTSON[i - 1, 1] * f + _ROBERTSON[i, 1] * (1 - f))
            vv = v - (_ROBERTSON[i - 1, 2] * f + _ROBERTSON[i, 2] * (1 - f))
            du, dv = du * (1 - f) + last_du * f, dv * (1 - f) + last_dv * f
            n = math.hypot(du, dv)
            return temp, (uu * du / n + vv * dv / n) * TINT_SCALE
        last_dt, last_du, last_dv = dt, du, dv
    return 5000.0, 0.0


def temp_tint_to_xy(temp: float, tint: float) -> tuple[float, float]:
    r = 1e6 / max(temp, 1.0)
    offset = tint / TINT_SCALE
    for i in range(len(_ROBERTSON) - 1):
        if r < _ROBERTSON[i + 1, 0] or i == len(_ROBERTSON) - 2:
            f = (_ROBERTSON[i + 1, 0] - r) / (_ROBERTSON[i + 1, 0] - _ROBERTSON[i, 0])
            u = _ROBERTSON[i, 1] * f + _ROBERTSON[i + 1, 1] * (1 - f)
            v = _ROBERTSON[i, 2] * f + _ROBERTSON[i + 1, 2] * (1 - f)
            d1 = np.array([1.0, _ROBERTSON[i, 3]])
            d2 = np.array([1.0, _ROBERTSON[i + 1, 3]])
            d = d1 / np.linalg.norm(d1) * f + d2 / np.linalg.norm(d2) * (1 - f)
            d /= np.linalg.norm(d)
            u, v = u + d[0] * offset, v + d[1] * offset
            den = u - 4.0 * v + 2.0
            return 1.5 * u / den, v / den
    return D50_XY


def illuminant_weight(temp: float, t1: float, t2: float | None) -> float:
    """Blend factor for matrix 1 (vs 2) at a white of this temperature, linear in inverse temperature."""
    if t2 is None or t1 == t2:
        return 1.0
    if t1 > t2:
        return 1.0 - illuminant_weight(temp, t2, t1)
    if temp <= t1:
        return 1.0
    if temp >= t2:
        return 0.0
    return (1.0 / temp - 1.0 / t2) / (1.0 / t1 - 1.0 / t2)


def blend(mats: list[np.ndarray], g: float) -> np.ndarray:
    return mats[0] if len(mats) == 1 else g * mats[0] + (1 - g) * mats[1]


def _xyz_to_camera(profile, xy) -> np.ndarray:
    temp, _ = xy_to_temp_tint(xy)
    return blend(profile.color_matrices, illuminant_weight(temp, *profile.temperatures))


def neutral_to_xy(profile, neutral) -> tuple[float, float]:
    """White (xy) that the camera records as this neutral (DNG SDK NeutralToXY)."""
    last = D50_XY
    for i in range(30):
        cam_to_xyz = np.linalg.inv(_xyz_to_camera(profile, last))
        nxt = xyz_to_xy(cam_to_xyz @ np.asarray(neutral, float))
        if abs(nxt[0] - last[0]) + abs(nxt[1] - last[1]) < 1e-7:
            return nxt
        if i == 29:
            nxt = ((last[0] + nxt[0]) / 2, (last[1] + nxt[1]) / 2)
        last = nxt
    return last


def xy_to_neutral(profile, xy) -> np.ndarray:
    """Camera-space neutral (max 1) for a white point."""
    n = _xyz_to_camera(profile, xy) @ xy_to_xyz(xy)
    return n / n.max()


def bradford(src_xy, dst_xy) -> np.ndarray:
    m = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])
    s, d = m @ xy_to_xyz(src_xy), m @ xy_to_xyz(dst_xy)
    return np.linalg.inv(m) @ np.diag(d / s) @ m


def camera_to_prophoto(profile, neutral) -> tuple[np.ndarray, np.ndarray]:
    """(camera white clip levels, camera -> ProPhoto linear matrix) for a white balance (DNG SDK SetWhiteXY)."""
    xy = neutral_to_xy(profile, neutral)
    temp, _ = xy_to_temp_tint(xy)
    g = illuminant_weight(temp, *profile.temperatures)
    camera_white = np.clip(np.asarray(neutral, float) / np.max(neutral), 0.001, 1.0)
    if profile.forward_matrices:
        cam_to_pcs = blend(profile.forward_matrices, g) @ np.diag(1.0 / camera_white)
    else:
        pcs_to_camera = blend(profile.color_matrices, g) @ bradford(D50_XY, xy)
        pcs_to_camera /= np.max(pcs_to_camera @ D50_XYZ)
        cam_to_pcs = np.linalg.inv(pcs_to_camera)
    return camera_white, XYZ_D50_TO_PROPHOTO @ cam_to_pcs
