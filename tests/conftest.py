import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def make_jpeg(path, size=(640, 427), color=(200, 120, 60), when="2024:05:01 10:20:30", model="TestCam X1",
              orientation=None):
    from PIL import Image

    img = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x010F] = "Foto"  # Make
    exif[0x0110] = model  # Model
    if orientation:
        exif[0x0112] = orientation
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = when  # DateTimeOriginal
    ifd[0x8827] = 400  # ISO
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", exif=exif.tobytes(), quality=90)
    return path


@pytest.fixture
def photo_tree(tmp_path):
    root = tmp_path / "photos"
    make_jpeg(root / "2024" / "a.jpg", when="2024:05:01 10:00:00")
    make_jpeg(root / "2024" / "b.jpg", when="2024:05:01 09:00:00", color=(20, 90, 200))
    make_jpeg(root / "2024" / "trip" / "c.jpg", when="2024:06:01 12:00:00", model="Other Z9", size=(400, 600))
    (root / "2024" / "notes.txt").write_text("not a photo")
    (root / ".hidden").mkdir()
    make_jpeg(root / ".hidden" / "skip.jpg")
    return root


@pytest.fixture
def catalog(tmp_path):
    from foto.catalog import Catalog

    cat = Catalog.open(tmp_path / "cat" / "catalog.db")
    yield cat
    cat.close()


def make_dng(path, width=1800, height=1200, orientation=6, preview=True, extra_tags=()):
    """Tiny synthetic DNG: RGGB CFA gradient, optional 8-bit RGB preview in IFD0."""
    import numpy as np
    tifffile = pytest.importorskip("tifffile")

    y, x = np.mgrid[0:height, 0:width]
    chans = {"r": x / width, "g": y / height, "b": 1 - x / width}
    cfa = np.zeros((height, width), np.uint16)
    for (dy, dx), c in {(0, 0): "r", (0, 1): "g", (1, 0): "g", (1, 1): "b"}.items():
        cfa[dy::2, dx::2] = chans[c][dy::2, dx::2] * 4000 + 64
    one = (10000, 10000)
    raw_tags = [
        (33421, "H", 2, (2, 2)), (33422, "B", 4, (0, 1, 1, 2)), (50710, "B", 3, (0, 1, 2)),
        (50711, "H", 1, 1), (50714, "H", 1, 64), (50717, "H", 1, 4095),
    ]
    main_tags = [
        (50706, "B", 4, (1, 4, 0, 0)), (50707, "B", 4, (1, 1, 0, 0)), (50708, "s", 0, "Foto TestCam"),
        (271, "s", 0, "Foto"), (272, "s", 0, "TestCam"), (274, "H", 1, orientation),
        (50721, "2i", 9, (*one, 0, 1, 0, 1, 0, 1, *one, 0, 1, 0, 1, 0, 1, *one)),
        (50778, "H", 1, 21), (50728, "2I", 3, (1, 1, 1, 1, 1, 1)), *extra_tags,
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(path) as tif:
        if preview:
            rgb = (np.stack([chans["r"], chans["g"], chans["b"]], -1)[::4, ::4] * 255).astype(np.uint8)
            tif.write(rgb, photometric="rgb", subfiletype=1, subifds=1, extratags=main_tags)
            tif.write(cfa, photometric=32803, subfiletype=0, extratags=raw_tags)
        else:
            tif.write(cfa, photometric=32803, subfiletype=0, extratags=main_tags + raw_tags)
    return path


def make_cr3(path, make="Canon", model="Canon EOS R5m2", lens="RF24-70mm F2.8 L IS USM", when="2026:06:08 23:38:10"):
    """Minimal CR3 container: ftyp + moov/uuid(Canon) holding CMT1 (IFD0) and CMT2 (Exif IFD) TIFFs.
    No image data, so only metadata can be read from it."""
    import struct

    from PIL import Image

    def box(kind, payload):
        return struct.pack(">I4s", 8 + len(payload), kind) + payload

    def tiff(tags):
        exif = Image.Exif()
        for tag, value in tags.items():
            exif[tag] = value
        return exif.tobytes()[6:]  # drop the JPEG "Exif\0\0" prefix

    cmt1 = tiff({0x010F: make, 0x0110: model, 0x0132: when})
    cmt2 = tiff({0x9003: when, 0x8827: 3200, 0xA434: lens})
    canon = bytes.fromhex("85c0b687820f11e08111f4ce462b6a48") + box(b"CMT1", cmt1) + box(b"CMT2", cmt2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(box(b"ftyp", b"crx \x00\x00\x00\x01crx isom") + box(b"moov", box(b"uuid", canon)) + box(b"mdat", b""))
    return path
