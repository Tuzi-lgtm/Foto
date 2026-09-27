import os
import time

import pytest

from foto import edits
from foto.catalog.catalog import FLAG_PICK
from foto.importer import import_folder
from foto.undo import UndoStack


@pytest.fixture
def isolated_settings(tmp_path):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "settings"))
    yield QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
    QSettings.setDefaultFormat(QSettings.NativeFormat)


def test_undo_redo_every_kind_of_change(catalog, photo_tree):
    import_folder(catalog, str(photo_tree))
    a, b, _ = sorted(catalog.query(), key=lambda r: r.filename)
    cid = catalog.create_collection("Best")
    undo = UndoStack(catalog)
    ids = [a.id, b.id]

    undo.run("Rating", ids, lambda: catalog.set_rating(ids, 4))
    undo.run("Pick", ids, lambda: catalog.set_flag(ids, FLAG_PICK))
    undo.run("Rotate Right", ids, lambda: edits.rotate(catalog, ids, 90))
    undo.run("Add Tags", ids, lambda: catalog.add_tags(ids, ["beach"]))
    undo.run("Add to Collection", ids, lambda: catalog.add_to_collection(cid, ids))
    undo.run("Rating", ids, lambda: catalog.set_rating(ids, 4))  # no change: not recorded
    assert undo.undo_label() == "Add to Collection"

    labels = []
    while (step := undo.undo()) is not None:
        labels.append(step.label)
    assert labels == ["Add to Collection", "Add Tags", "Rotate Right", "Pick", "Rating"]
    rec = catalog.get(a.id)
    assert (rec.rating, rec.flag, rec.rotate) == (0, 0, 0)
    assert catalog.tags_for(ids) == {} and catalog.collections()[0].count == 0
    assert catalog.edit_history(a.id)[-1][1] == "Undo rotate right"  # undo is itself in the edit history

    while undo.redo() is not None:
        pass
    rec = catalog.get(a.id)
    assert (rec.rating, rec.flag, rec.rotate) == (4, FLAG_PICK, 90)
    assert catalog.tags_for(ids) == {"beach": 2} and catalog.collections()[0].count == 2

    undo.undo()
    undo.run("Rating", ids, lambda: catalog.set_rating(ids, 1))
    assert undo.redo_label() is None  # a new command drops the redo history


def test_undo_skips_removed_photos(catalog, photo_tree):
    import_folder(catalog, str(photo_tree))
    a, b, _ = sorted(catalog.query(), key=lambda r: r.filename)
    undo = UndoStack(catalog)
    undo.run("Rating", [a.id, b.id], lambda: catalog.set_rating([a.id, b.id], 3))
    catalog.remove_images([a.id])
    assert catalog.count() == 2 and (photo_tree / "2024" / "a.jpg").exists()
    undo.undo()
    assert catalog.get(b.id).rating == 0 and catalog.get(a.id) is None


def test_cache_trim_drops_least_recently_used(qapp, tmp_path):
    from PySide6.QtGui import QImage

    from foto.imaging.cache import DiskCache

    cache = DiskCache(tmp_path / "cache")
    image = QImage(300, 200, QImage.Format_RGB888)
    image.fill(0x808080)
    for i in range(10):
        cache.store(f"{i:02d}" * 20, "thumb", image)
        os.utime(cache.path_for(f"{i:02d}" * 20, "thumb"), (time.time() - 1000 + i, time.time() - 1000 + i))
    cache.store("ff" * 20, "preview", image)
    usage = cache.usage()
    assert usage["thumb"][0] == 10 and usage["preview"][0] == 1
    one = usage["preview"][1]

    freed = cache.trim(limit_bytes=int(one * 6))
    assert freed > 0 and cache.size_bytes() <= one * 6 * 0.9 + one
    assert cache.load("09" * 20, "thumb") is not None  # newest kept
    assert cache.load("00" * 20, "thumb") is None  # oldest gone
    assert cache.trim(limit_bytes=0) == 0  # unlimited

    (tmp_path / "cache" / "keep.txt").write_text("not ours")
    cache.clear()
    assert cache.size_bytes() == 0 and (tmp_path / "cache" / "keep.txt").exists()


def test_prefs_roundtrip(tmp_path, isolated_settings):
    from foto.config import CatalogPaths
    from foto.prefs import Prefs

    p = Prefs(isolated_settings)
    paths = CatalogPaths(tmp_path / "Main.fotocat")
    assert p.cache_dir(paths) == paths.cache and p.cache_limit_gb == 20 and p.decode_threads == 0
    p.cache_location = str(tmp_path / "fast")
    p.cache_limit_gb = 0
    p.default_catalog = str(paths.root)
    isolated_settings.sync()
    again = Prefs(isolated_settings)
    assert again.cache_dir(paths) == tmp_path / "fast"
    assert (again.cache_limit_gb, again.default_catalog) == (0, str(paths.root))


def test_switch_ocio_config_keeps_choices(tmp_path):
    from foto.color.ocio import BUILTIN_CONFIG, ColorManager

    cm = ColorManager(BUILTIN_CONFIG)
    view = cm.view
    seen = []
    cm.changed.connect(lambda: seen.append(1))
    cm.load_config(str(tmp_path / "missing.ocio"))
    assert cm.error and cm.config_path == BUILTIN_CONFIG and cm.view == view and seen == [1]


def test_main_window_edit_menu(qapp, tmp_path, photo_tree, isolated_settings):
    from foto.catalog import Catalog
    from foto.config import CatalogPaths
    from foto.ui.main_window import MainWindow

    paths = CatalogPaths(tmp_path / "T.fotocat").ensure()
    cat = Catalog.open(paths.db)
    import_folder(cat, str(photo_tree))
    cat.close()

    win = MainWindow(paths)
    try:
        menus = [a.text() for a in win.menuBar().actions()]
        assert menus[:3] == ["&File", "&Edit", "&Photo"]

        win.select_all()
        win.set_rating(5)
        assert {r.rating for r in win.catalog.query()} == {5}
        win._update_edit_menu()
        assert win.act_undo.text() == "Undo Rating" and win.act_remove.text() == "Remove 3 Photos from Catalog…"
        win.undo_last()
        assert {r.rating for r in win.catalog.query()} == {0}
        assert win.act_redo.text() == "Redo Rating"

        win.select_none()
        win.grid.setCurrentIndex(win.model.index(0))
        win.select_inverse()
        assert len(win.grid.selected_records()) == 2

        first = win.model.records[0]
        win.select_none()
        win.rotate(90)  # acts on the current photo
        win.copy_settings()
        win.select_all()
        win.paste_settings()
        assert {r.rotate for r in win.catalog.query()} == {90}
        win.revert_edits()
        assert {r.rotate for r in win.catalog.query()} == {0}
        assert first.id in {r.id for r in win.catalog.query()}
    finally:
        win.close()
