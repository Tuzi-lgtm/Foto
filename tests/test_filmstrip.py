import pytest

from foto.catalog import ImageRecord
from foto.importer import import_folder


def _rec(w, h, rotate=0):
    return ImageRecord(1, 1, "p", "p", ".jpg", 1, 1, None, None, None, None, None, None, None, None, w, h, 0, 0, rotate)


def test_display_aspect():
    from foto.ui.filmstrip import display_aspect

    assert display_aspect(_rec(6000, 4000)) == 1.5
    assert display_aspect(_rec(6000, 4000, rotate=90)) == pytest.approx(4000 / 6000)
    assert display_aspect(_rec(20000, 1000)) == 3.0  # panoramas are clamped
    assert display_aspect(_rec(None, None)) == 1.5


@pytest.fixture
def window(qapp, tmp_path, photo_tree):
    from PySide6.QtCore import QSettings

    from foto.catalog import Catalog
    from foto.config import CatalogPaths
    from foto.ui.main_window import MainWindow

    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "settings"))
    paths = CatalogPaths(tmp_path / "T.fotocat").ensure()
    cat = Catalog.open(paths.db)
    import_folder(cat, str(photo_tree))
    cat.close()
    win = MainWindow(paths)
    win.resize(1200, 800)
    win.show()
    yield win
    win.close()
    QSettings.setDefaultFormat(QSettings.NativeFormat)


def test_filmstrip_follows_mode_and_selects(window):
    from foto.ui.main_window import COMPARE, GRID, LOUPE

    win = window
    assert not win.filmstrip.isVisible()
    win.set_mode(LOUPE)
    assert win.filmstrip.isVisible()
    assert win.filmstrip.selectionModel() is win.grid.selectionModel()

    third = win.model.index(2)
    win.filmstrip.setCurrentIndex(third)  # a click in the strip
    assert win.loupe.pane.record.id == win.model.records[2].id

    win.act_filmstrip.trigger()  # F6 hides it
    assert not win.filmstrip.isVisible()
    win.act_filmstrip.trigger()
    assert win.filmstrip.isVisible()

    win.set_mode(GRID)
    assert not win.filmstrip.isVisible()


def test_filmstrip_resizes_thumbnails(window):
    from foto.ui.main_window import LOUPE

    win = window
    win.set_mode(LOUPE)
    delegate = win.filmstrip.itemDelegate()
    small = delegate.sizeHint(None, win.model.index(0))
    win.splitter.setSizes([400, 250])
    qapp_process()
    big = delegate.sizeHint(None, win.model.index(0))
    assert big.height() > small.height() and big.width() > small.width()


def test_compare_arrows_and_strip_change_active_side(window):
    from foto.ui.main_window import COMPARE

    win = window
    win.grid.setCurrentIndex(win.model.index(0))
    win.set_mode(COMPARE)
    left, right = (p.record.id for p in win.compare.panes)
    assert (left, right) == (win.model.records[0].id, win.model.records[1].id)

    win.compare.set_active(1)
    win.navigate(1)
    assert win.compare.panes[1].record.id == win.model.records[2].id
    assert win.compare.panes[0].record.id == left  # the other side stays put
    assert win.grid.currentIndex().row() == 2  # filmstrip marks the active photo

    win.filmstrip.setCurrentIndex(win.model.index(0))
    assert win.compare.panes[1].record.id == left


def test_secondary_window_shares_selection(window):
    from foto.ui import secondary as sec

    win = window
    win.show_secondary(sec.DETAIL)
    second = win.secondary
    assert second.isVisible() and win.act_secondary.isChecked() and win.act_sec_detail.isChecked()

    win.grid.setCurrentIndex(win.model.index(1))
    assert second.detail.pane.record.id == win.model.records[1].id  # follows the main window

    win.show_secondary(sec.GRID)
    second.grid.setCurrentIndex(win.model.index(2))  # pick on the second screen
    assert win.grid.currentIndex().row() == 2

    second.close()
    assert not win.act_secondary.isChecked()
    assert not win.settings.value("secondary/visible", type=bool)


def qapp_process():
    from PySide6.QtWidgets import QApplication

    QApplication.processEvents()


def test_camera_name():
    from foto.ui.inspector import camera_name

    assert camera_name("Canon", "Canon EOS R5m2") == "Canon EOS R5m2"
    assert camera_name("NIKON CORPORATION", "NIKON Z 8") == "NIKON Z 8"
    assert camera_name("SONY", "ILCE-7RM5") == "SONY ILCE-7RM5"
    assert camera_name(None, "X100V") == "X100V"


def test_bottom_bar(window):
    from foto.catalog.catalog import FLAG_NONE, FLAG_PICK
    from foto.ui.main_window import COMPARE, GRID, LOUPE

    win, bar = window, window.bottombar
    win.grid.setCurrentIndex(win.model.index(0))
    rec_id = win.model.records[0].id

    bar.stars[2].click()  # 3 stars
    assert win.catalog.get(rec_id).rating == 3 and bar.stars[2].property("lit") and not bar.stars[3].property("lit")
    bar.stars[2].click()  # same star again clears
    assert win.catalog.get(rec_id).rating == 0
    bar.pick.click()
    assert win.catalog.get(rec_id).flag == FLAG_PICK and bar.pick.isChecked()
    bar.pick.click()
    assert win.catalog.get(rec_id).flag == FLAG_NONE
    assert win.act_undo.text() == "Undo Unflag"  # toolbar changes are undoable like the keys

    assert not bar.zoom.isEnabled()
    bar.mode_buttons.button(LOUPE).click()
    assert win.stack.currentIndex() == LOUPE and bar.zoom.isEnabled()
    from PySide6.QtTest import QTest

    for _ in range(100):  # the preview decodes in the background
        if win.loupe.pane.view.texture_size():
            break
        QTest.qWait(50)
    win.zoom_to(4.0)  # test photos are small, so fit is already above 100%
    assert bar.zoom.text().startswith("400%")
    bar.fit.click()
    assert bar.zoom.text().startswith("Fit")

    win.set_mode(GRID)
    win.model.set_records([])  # nothing to compare
    bar.mode_buttons.button(COMPARE).click()
    assert win.stack.currentIndex() == GRID and bar.mode_buttons.checkedId() == GRID


def test_develop_mode(window):
    from foto.ui.main_window import DEVELOP

    win = window
    win.grid.setCurrentIndex(win.model.index(1))
    win.bottombar.mode_buttons.button(DEVELOP).click()
    assert win.stack.currentIndex() == DEVELOP and win.filmstrip.isVisible()
    assert win.develop.record.id == win.model.records[1].id
    assert "Not a raw file" in win.develop.view.overlay_text  # test photos are JPEGs
    win.set_rating(2)  # commands act on the photo in Develop
    assert win.catalog.get(win.model.records[1].id).rating == 2
    win.filmstrip.setCurrentIndex(win.model.index(0))
    assert win.develop.record.id == win.model.records[0].id
