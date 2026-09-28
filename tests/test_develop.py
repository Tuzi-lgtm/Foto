import os
import struct
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from foto.develop import colormath as cm
from foto.develop import pipeline as pl
from foto.develop.dcp import HueSatTable, Profile, camera_key, installed_profiles, load_profile


def write_dcp(path, *, look=None, curve=None, offset=-0.5):
    """Minimal .dcp: one IFD with the tags Foto reads (little-endian, "IIRC" magic)."""
    entries = []  # (tag, type, count, payload bytes)

    def srational(values):
        return b"".join(struct.pack("<ii", int(round(v * 10000)), 10000) for v in values)

    fm = cm.PROPHOTO_TO_XYZ_D50.ravel()
    cm1 = np.linalg.inv(cm.PROPHOTO_TO_XYZ_D50).ravel()  # "camera" == ProPhoto, a convenient identity camera
    name = b"Test Neutral\0"
    entries += [(50708, 2, 8, b"TestCam\0"), (50721, 10, 9, srational(cm1)), (50722, 10, 9, srational(cm1)),
                (50778, 3, 1, struct.pack("<H", 17) + b"\0\0"), (50779, 3, 1, struct.pack("<H", 21) + b"\0\0"),
                (50936, 2, len(name), name), (50964, 10, 9, srational(fm)), (50965, 10, 9, srational(fm)),
                (51109, 10, 1, srational([offset]))]
    if curve is not None:
        entries.append((50940, 11, curve.size, np.asarray(curve, "<f4").tobytes()))
    if look is not None:
        v, h, s, _ = look.shape
        entries += [(50981, 4, 3, struct.pack("<III", h, s, v)), (50982, 11, look.size, np.asarray(look, "<f4").tobytes()),
                    (51108, 4, 1, struct.pack("<I", 1))]
    entries.sort()
    data_at = 8 + 2 + 12 * len(entries) + 4
    ifd, blob = b"", b""
    for tag, typ, count, payload in entries:
        if len(payload) <= 4:
            ifd += struct.pack("<HHI", tag, typ, count) + payload.ljust(4, b"\0")
        else:
            ifd += struct.pack("<HHII", tag, typ, count, data_at + len(blob))
            blob += payload + (b"\0" if len(payload) % 2 else b"")
    path.write_bytes(b"IIRC" + struct.pack("<I", 8) + struct.pack("<H", len(entries)) + ifd + b"\0\0\0\0" + blob)
    return path


def identity_look(h=6, s=4, v=3):
    t = np.zeros((v, h, s, 3), np.float32)
    t[..., 1] = t[..., 2] = 1.0
    return t


def test_parse_dcp(tmp_path):
    curve = np.array([[0, 0], [0.5, 0.6], [1, 1]], np.float32)
    p = load_profile(write_dcp(tmp_path / "t.dcp", look=identity_look(), curve=curve))
    assert (p.name, p.camera, p.illuminants, p.temperatures) == ("Test Neutral", "TestCam", (17, 21), (2856, 6504))
    assert p.look_table.data.shape == (3, 6, 4, 3) and p.look_table.srgb_encoded
    assert p.baseline_exposure_offset == pytest.approx(-0.5)
    assert np.allclose(p.forward_matrices[0], cm.PROPHOTO_TO_XYZ_D50, atol=1e-4)
    assert p.tone_curve.shape == (3, 2)


def test_camera_names_match():
    assert camera_key("Canon EOS R5m2") == camera_key("Canon EOS R5 Mark II")
    assert camera_key("Canon EOS 5D Mark III") == "canoneos5dm3"
    assert camera_key("NIKON Z 8") != camera_key("NIKON Z 9")


def test_temperature_roundtrip():
    temp, tint = cm.xy_to_temp_tint(cm.temp_tint_to_xy(5500, 10))
    assert temp == pytest.approx(5500, rel=1e-3) and tint == pytest.approx(10, abs=0.05)
    d65_temp, d65_tint = cm.xy_to_temp_tint((0.3127, 0.3290))
    assert d65_temp == pytest.approx(6504, abs=15) and abs(d65_tint) == pytest.approx(9.8, abs=0.5)  # D65 sits off the Planckian locus
    assert cm.illuminant_weight(2856, 2856, 6504) == 1.0 and cm.illuminant_weight(7000, 2856, 6504) == 0.0


def _neutral_profile(tmp_path, **kw) -> Profile:
    return load_profile(write_dcp(tmp_path / "n.dcp", **kw))


def test_render_keeps_grays_gray_and_clips_white(tmp_path):
    prof = _neutral_profile(tmp_path, offset=0.0)
    neutral = np.array([0.5, 1.0, 0.7])  # camera records white like this
    p = pl.render_params(prof, neutral)
    grays = np.array([[neutral * k for k in (0.05, 0.2, 0.6, 1.0, 1.5)]], np.float32)
    out = pl.render(grays, p)
    assert np.allclose(out[..., 0], out[..., 1], atol=2e-3) and np.allclose(out[..., 1], out[..., 2], atol=2e-3)
    assert np.all(np.diff(out[0, :, 1]) >= 0)  # monotone
    np.testing.assert_allclose(out[0, -1], [1, 1, 1], atol=1e-3)  # beyond sensor clip: clean white, no magenta


def test_exposure_is_a_linear_gain(tmp_path):
    prof = _neutral_profile(tmp_path, offset=0.0)
    x = np.array([[[0.05, 0.1, 0.07]]], np.float32)
    lin = pl.render(x, pl.render_params(prof, [1, 1, 1], exposure=1.0))
    ref = pl.render(x * 2, pl.render_params(prof, [1, 1, 1]))
    np.testing.assert_allclose(lin, ref, atol=1e-5)


def test_identity_look_table_is_a_no_op():
    rng = np.random.default_rng(1)
    x = rng.random((50, 3)).astype(np.float32)
    table = HueSatTable(6, 4, 3, identity_look(), srgb_encoded=True)
    np.testing.assert_allclose(pl.apply_hue_sat(x, table), x, atol=1e-5)


def test_hue_shift_rotates_hue():
    t = identity_look(h=6, s=4, v=1)
    t[..., 0] = 120.0  # +120 degrees everywhere
    out = pl.apply_hue_sat(np.array([[1.0, 0.0, 0.0]]), HueSatTable(6, 4, 1, t))
    np.testing.assert_allclose(out, [[0.0, 1.0, 0.0]], atol=1e-5)  # red -> green


def test_rgb_tone_preserves_hue_ratios():
    lut = pl.spline_lut(np.array([[0, 0], [0.25, 0.4], [1, 1]]))
    x = np.array([[0.2, 0.1, 0.05]])
    y = pl.apply_rgb_tone(x, lut)
    # Middle channel keeps its relative position between min and max.
    assert (y[0, 1] - y[0, 2]) / (y[0, 0] - y[0, 2]) == pytest.approx((0.1 - 0.05) / (0.2 - 0.05), rel=1e-6)


GPU_CHECK = textwrap.dedent("""
    import sys, numpy as np
    sys.path.insert(0, {tests!r})
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication([])
    from foto.develop import pipeline as pl
    from foto.develop.dcp import load_profile
    from foto.develop.gpu import render_gpu
    from test_develop import write_dcp, identity_look
    from pathlib import Path
    look = identity_look(h=12, s=5, v=4)
    look[..., 0] = np.linspace(-10, 10, 12)[None, :, None]  # varying hue shifts
    look[..., 1] = 0.8
    prof = load_profile(write_dcp(Path({tmp!r}) / "g.dcp", look=look, curve=np.array([[0, 0], [0.3, 0.45], [1, 1]], np.float32)))
    rng = np.random.default_rng(3)
    x = rng.random((64, 64, 3)).astype(np.float32) * 1.2
    p = pl.render_params(prof, [0.6, 1.0, 0.8], exposure=0.5)
    gpu = render_gpu(x, p)
    if gpu is None:
        print("NO_GL"); sys.exit(0)
    print("MAXDIFF", float(np.abs(gpu - pl.render(x, p)).max()))
""")


def test_gpu_matches_cpu(tmp_path):
    """The GLSL port renders what the CPU reference renders (needs a real GPU; skipped otherwise)."""
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    script = GPU_CHECK.format(tests=os.path.dirname(__file__), tmp=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120)
    if "NO_GL" in out.stdout or out.returncode != 0 and "MAXDIFF" not in out.stdout:
        pytest.skip(f"no OpenGL 4.1 context: {out.stderr.strip()[-200:]}")
    diff = float(out.stdout.split("MAXDIFF")[1])
    assert diff < 1e-4


SAMPLE = r"F:\Photos\test\CU1A6226.CR3"


@pytest.mark.skipif(not os.path.exists(SAMPLE) or not installed_profiles("Canon", "Canon EOS R5m2"),
                    reason="needs the sample CR3 and Lightroom's Canon profiles")
def test_real_raw_with_camera_neutral():
    from foto.develop.engine import resolve_profile
    from foto.catalog import ImageRecord

    rec = ImageRecord(1, 1, SAMPLE, "x", ".cr3", 1, 1, None, "Canon", "Canon EOS R5m2", *([None] * 7), 0, 0)
    prof = resolve_profile(rec)
    assert prof is not None and prof.name == "Camera Neutral"
    raw = pl.decode_linear(SAMPLE)
    assert raw.rgb.dtype == np.uint16 and raw.rgb.shape[1] > raw.rgb.shape[0]
    out = pl.render(raw.rgb[::16, ::16], pl.render_params(prof, raw.as_shot_neutral))
    assert 0.0 <= out.min() and out.max() <= 1.0 and out.mean() > 0.005


def test_dng_baseline_exposure(tmp_path):
    from conftest import make_dng
    from foto.develop.baseline import DNG_OFFSET, baseline_exposure, dng_baseline

    path = make_dng(tmp_path / "b.dng")
    assert dng_baseline(str(path)) is None
    tagged = make_dng(tmp_path / "t.dng", extra_tags=[(50730, "2i", 1, (135, 100))])
    assert dng_baseline(str(tagged)) == pytest.approx(1.35)
    assert baseline_exposure(str(tagged), "Foto", "TestCam") == pytest.approx(1.35 + DNG_OFFSET)


@pytest.mark.skipif(not os.path.exists(SAMPLE), reason="needs the sample CR3")
def test_canon_htp_from_maker_notes():
    from foto.develop.baseline import BASELINES, HTP_EXTRA, baseline_exposure, canon_htp

    assert canon_htp(SAMPLE) == 2  # the R5 II samples were shot with HTP "Enhanced"
    assert baseline_exposure(SAMPLE, "Canon", "Canon EOS R5m2") == pytest.approx(BASELINES["canoneosr5m2"] + HTP_EXTRA)


def test_encoded_look_table_uses_linear_hue_and_saturation():
    """sRGB-encoded tables encode only the value axis (DNG RefBaselineHueSatMap).

    Encoding the whole colour first made clipped yellows look nearly neutral and picked up
    the table's near-neutral highlight entries, turning lamps and faces blue."""
    t = identity_look(h=6, s=5, v=3)
    t[:, :, 1, 0] = 180.0  # low-saturation entries: flip hue
    x = np.array([[1.0, 1.0, 0.5]])  # linear s = 0.5 -> sat index 2, well past the flipped entries
    np.testing.assert_allclose(pl.apply_hue_sat(x, HueSatTable(6, 5, 3, t, srgb_encoded=True)), x, atol=1e-5)
