import os

import pytest

from conftest import make_jpeg
from foto.catalog import Catalog
from foto.imaging import decode as dec
from foto.imaging.cache import DiskCache, cache_key
from foto.importer import import_folder


def test_decode_levels(qapp, tmp_path):
    p = make_jpeg(tmp_path / "big.jpg", size=(3000, 2000))
    thumb = dec.decode(str(p), dec.THUMB)
    assert max(thumb.width(), thumb.height()) == 320
    preview = dec.decode(str(p), dec.PREVIEW)
    assert preview.width() == 2560 and abs(preview.height() - 1707) <= 1
    full = dec.decode(str(p), dec.FULL)
    assert (full.width(), full.height()) == (3000, 2000)


def test_exif_orientation_applied(qapp, tmp_path):
    p = make_jpeg(tmp_path / "rot.jpg", size=(600, 400), orientation=6)
    img = dec.decode(str(p), dec.THUMB)
    assert img.height() > img.width()


def test_disk_cache_roundtrip(qapp, tmp_path):
    p = make_jpeg(tmp_path / "x.jpg")
    st = os.stat(p)
    key = cache_key(str(p), st.st_size, st.st_mtime_ns)
    cache = DiskCache(tmp_path / "cache")
    assert cache.load(key, dec.THUMB) is None
    cache.store(key, dec.THUMB, dec.decode(str(p), dec.THUMB))
    assert cache.load(key, dec.THUMB).width() == 320
    assert key != cache_key(str(p), st.st_size, st.st_mtime_ns + 1)


def test_service_generates_and_caches(qapp, tmp_path, photo_tree):
    from PySide6.QtCore import QEventLoop, QTimer

    from foto.imaging.service import ImageService

    cat = Catalog.open(tmp_path / "c.db")
    import_folder(cat, str(photo_tree))
    recs = cat.query()
    svc = ImageService(DiskCache(tmp_path / "cache"))
    got = {}
    loop = QEventLoop()

    def on_ready(image_id, level, image):
        got[image_id] = image
        if len(got) == len(recs):
            loop.quit()

    svc.ready.connect(on_ready)
    for r in recs:
        assert svc.get(r, dec.THUMB) is None
    QTimer.singleShot(10000, loop.quit)
    loop.exec()
    assert len(got) == len(recs)
    assert all(svc.get(r, dec.THUMB) is not None for r in recs)  # memory hit
    assert len(list((tmp_path / "cache" / "thumb").rglob("*.jpg"))) == len(recs)
    svc.shutdown()
    cat.close()


@pytest.mark.parametrize("preview", [True, False])
def test_synthetic_dng(qapp, tmp_path, preview):
    from conftest import make_dng
    from foto.importer import read_file

    p = str(make_dng(tmp_path / "x.dng", preview=preview))
    thumb = dec.decode(p, dec.THUMB)
    assert (thumb.width(), thumb.height()) == (213, 320)  # camera orientation applied
    # Top-left after a 90° CW turn is the source's bottom-left: no red, full green + blue.
    c = thumb.pixelColor(3, 3)
    assert c.red() < 60 and c.green() > 180 and c.blue() > 180
    full = dec.decode(p, dec.FULL)
    assert (full.width(), full.height()) == (1200, 1800)
    meta = read_file(p)
    assert (meta["model"], meta["width"], meta["height"]) == ("TestCam", 1200, 1800)
    assert not meta["capture_time"].startswith("1970")


RAW = os.environ.get("FOTO_SAMPLE_RAW")


@pytest.mark.skipif(not RAW, reason="set FOTO_SAMPLE_RAW to a raw file (ARW/CR3/NEF/RAF/DNG)")
def test_raw_decode(qapp):
    thumb = dec.decode(RAW, dec.THUMB)
    assert max(thumb.width(), thumb.height()) == 320
    preview = dec.decode(RAW, dec.PREVIEW)
    assert max(preview.width(), preview.height()) >= 1000
