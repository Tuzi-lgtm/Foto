import numpy as np

from foto.color.ocio import ColorManager


def test_default_view_is_identity_for_srgb(qapp):
    cm = ColorManager(config_path="ocio://default")
    assert cm.view == "Un-tone-mapped"
    rgb = np.array([[0.1, 0.5, 0.9]], dtype=np.float32)
    np.testing.assert_allclose(cm.apply_cpu(rgb), rgb, atol=1e-4)
    shader = cm.shader()
    assert "OCIOMain" in shader.source


def test_exposure_and_lut_view(qapp):
    cm = ColorManager(config_path="ocio://default")
    cm.set_exposure(1.0)
    rgb = np.array([[0.2, 0.2, 0.2]], dtype=np.float32)
    assert cm.apply_cpu(rgb)[0, 0] > 0.25
    aces = [v for v in cm.views() if v.startswith("ACES")]
    if aces:
        cm.set_view(aces[0])
        bundle = cm.shader()
        assert bundle.textures and all(t.values.size >= t.width for t in bundle.textures)


def test_bad_config_falls_back(qapp, tmp_path):
    cm = ColorManager(config_path=str(tmp_path / "missing.ocio"))
    assert cm.error and cm.config_path == "ocio://default"
