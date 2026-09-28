"""Main window: wires catalog, grid, loupe, compare, panels, import and backup."""

from __future__ import annotations

import os
import subprocess
import sys
from functools import partial

from PySide6.QtCore import QFile, QItemSelection, QItemSelectionModel, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QDockWidget, QFileDialog, QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from foto import edits
from foto.backup.targets import list_targets
from foto.catalog import Catalog, ImageRecord, LibraryFilter
from foto.catalog.catalog import FLAG_NONE, FLAG_PICK, FLAG_REJECT
from foto.color.ocio import ColorManager
from foto.config import CatalogPaths
from foto.develop.engine import RawLoader
from foto.imaging import decode as dec
from foto.imaging.cache import DiskCache
from foto.imaging.service import ImageService
from foto.prefs import GB, Prefs
from foto.ui.backup_dialog import BackupDialog
from foto.ui.bottombar import BottomBar
from foto.ui.compare import CompareView, LoupeView
from foto.ui.develop_view import DevelopView
from foto.ui.filmstrip import Filmstrip
from foto.ui.filterbar import FilterBar
from foto.ui.grid import GridView, ImageListModel
from foto.ui.inspector import Inspector
from foto.ui.preferences import PreferencesDialog
from foto.ui import secondary as sec
from foto.ui.sidebar import Sidebar
from foto.ui.workers import BackupWorker, ImportWorker
from foto.undo import UndoStack

GRID, LOUPE, COMPARE, DEVELOP = 0, 1, 2, 3


def reveal_in_file_manager(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


class MainWindow(QMainWindow):
    def __init__(self, paths: CatalogPaths):
        super().__init__()
        self.paths = paths
        self.settings = QSettings()
        self.prefs = Prefs(self.settings)
        self.catalog = Catalog.open(paths.db)
        self.undo = UndoStack(self.catalog)
        self.service = ImageService(self._make_cache(), self)
        self.color = ColorManager(self.prefs.ocio_config or None, parent=self)
        self._restore_color()
        self._apply_performance()
        self.filter = LibraryFilter()
        self.worker = None
        self._copied_settings: dict | None = None

        self.setWindowTitle(f"Foto — {paths.root.name}")
        self._build_widgets()
        self._build_actions()
        self._build_menus()
        self._build_bottombar()
        self._restore_state()
        self.refresh_grid()
        if self.color.error:
            self.statusBar().showMessage(self.color.error, 8000)

    # -- layout ----------------------------------------------------------

    def _build_widgets(self) -> None:
        self.model = ImageListModel(self.service, self)
        self.grid = GridView()
        self.grid.setModel(self.model)
        self.grid.activated.connect(lambda _: self.set_mode(LOUPE))
        self.grid.customContextMenuRequested.connect(self._grid_menu)
        self.grid.selectionModel().currentChanged.connect(self._current_changed)
        self.grid.selectionModel().selectionChanged.connect(lambda *_: self._update_inspector())

        self.filmstrip = Filmstrip()
        self.filmstrip.setModel(self.model)
        self.filmstrip.setSelectionModel(self.grid.selectionModel())
        self.filmstrip.customContextMenuRequested.connect(lambda pos: self._grid_menu(pos, self.filmstrip))
        self.secondary: sec.SecondaryWindow | None = None

        self.loupe = LoupeView(self.service, self.color)
        self.compare = CompareView(self.service, self.color)
        self.compare.activeChanged.connect(self._compare_active_changed)
        for view in (self.loupe, self.compare):
            view.backgroundDoubleClicked.connect(partial(self.set_mode, GRID))

        self.raw_loader = RawLoader(self)
        self.develop = DevelopView(self.service, self.raw_loader, self.color, self.catalog.get_edit)
        self.develop.backgroundDoubleClicked.connect(partial(self.set_mode, GRID))

        self.stack = QStackedWidget()
        for w in (self.grid, self.loupe, self.compare, self.develop):
            self.stack.addWidget(w)

        self.filterbar = FilterBar()
        self.filterbar.changed.connect(self.refresh_grid)
        self.filterbar.cellSizeChanged.connect(self.grid.set_cell_size)

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.filterbar)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.stack)
        self.splitter.addWidget(self.filmstrip)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([800, 120])
        self.filmstrip.hide()  # only in loupe / compare
        lay.addWidget(self.splitter, 1)
        self._center_layout = lay
        self.setCentralWidget(center)

        self.sidebar = Sidebar(self.catalog, event_gap_hours=self.prefs.event_gap_hours)
        self.sidebar.sourceChanged.connect(self._source_changed)
        self.sidebar.customContextMenuRequested.connect(self._sidebar_menu)
        self.sidebar.target_collection = self.settings.value("target_collection", None, type=int) or None
        left = QDockWidget("Library", self)
        left.setObjectName("library_dock")
        left.setWidget(self.sidebar)
        self.addDockWidget(Qt.LeftDockWidgetArea, left)

        self.inspector = Inspector()
        self.inspector.addTags.connect(self._add_tags)
        self.inspector.removeTag.connect(self._remove_tag)
        right = QDockWidget("Inspector", self)
        right.setObjectName("inspector_dock")
        right.setWidget(self.inspector)
        self.addDockWidget(Qt.RightDockWidgetArea, right)
        self.docks = (left, right)

        self.progress = QProgressBar(maximumWidth=220)
        self.progress_label = QLabel()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(lambda: self.worker and self.worker.cancel())
        for w in (self.progress_label, self.progress, self.cancel_button):
            self.statusBar().addPermanentWidget(w)
            w.hide()

    def _action(self, text, slot, shortcut=None, checkable=False) -> QAction:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        self.addAction(a)  # shortcuts work even when the menu bar is hidden
        return a

    def _build_actions(self) -> None:
        A = self._action
        self.act_import = A("Import Folder…", self.import_dialog, "Ctrl+I")
        self.act_quit = A("Quit", self.close, QKeySequence.Quit)
        self.act_ratings = [A(f"Rating {n}" if n else "No Rating", partial(self.set_rating, n), str(n)) for n in range(6)]
        self.act_pick = A("Pick", partial(self.set_flag, FLAG_PICK), "P")
        self.act_reject = A("Reject", partial(self.set_flag, FLAG_REJECT), "X")
        self.act_unflag = A("Unflag", partial(self.set_flag, FLAG_NONE), "U")
        self.act_rot_l = A("Rotate Left", partial(self.rotate, -90), "Ctrl+[")
        self.act_rot_r = A("Rotate Right", partial(self.rotate, 90), "Ctrl+]")
        self.act_target = A("Add to Target Collection", self.add_to_target, "B")
        self.act_new_coll = A("New Collection…", self.new_collection, "Ctrl+N")
        self.act_tag = A("Add Tags…", self.focus_tags, "Ctrl+K")
        self.act_grid = A("Grid", partial(self.set_mode, GRID), "G")
        self.act_loupe = A("Loupe", partial(self.set_mode, LOUPE), "E")
        self.act_compare = A("Compare", partial(self.set_mode, COMPARE), "C")
        self.act_develop = A("Develop", partial(self.set_mode, DEVELOP), "D")
        self.act_camera_jpeg = A("Compare with Camera JPEG", self._toggle_camera_jpeg, "\\")
        self.act_back = A("Back to Grid", partial(self.set_mode, GRID), "Esc")
        self.act_zoom = A("Toggle Fit / 1:1", self.toggle_zoom, "Z")
        self.act_prev = A("Previous Photo", partial(self.navigate, -1), "Left")
        self.act_next = A("Next Photo", partial(self.navigate, 1), "Right")
        self.act_swap = A("Swap Compare Sides", self.compare.swap, "Ctrl+Shift+S")
        self.act_sync = A("Sync Compare Zoom", self._toggle_sync, None, True)
        self.act_sync.setChecked(True)
        self.act_panels = A("Toggle Panels", self._toggle_panels, "Tab")
        self.act_filmstrip = A("Show Filmstrip", self._update_filmstrip, "F6", True)
        self.act_filmstrip.setChecked(True)
        self.act_secondary = A("Show Secondary Window", self.toggle_secondary, "F11", True)
        self.act_sec_grid = A("Grid", partial(self.show_secondary, sec.GRID), "Shift+G", True)
        self.act_sec_detail = A("Detail", partial(self.show_secondary, sec.DETAIL), "Shift+E", True)
        group = QActionGroup(self)
        group.addAction(self.act_sec_grid)
        group.addAction(self.act_sec_detail)
        self.act_exp_up = A("Viewer Exposure +½", partial(self._exposure, 0.5), "Ctrl+=")
        self.act_exp_dn = A("Viewer Exposure −½", partial(self._exposure, -0.5), "Ctrl+-")
        self.act_exp_0 = A("Reset Viewer Exposure", partial(self._exposure, None), "Ctrl+0")
        self.act_backup_dlg = A("Backup Targets…", self.backup_dialog)
        self.act_refresh = A("Refresh", self.refresh_all, "F5")

        self.act_undo = A("Undo", self.undo_last, QKeySequence.Undo)
        self.act_redo = A("Redo", self.redo_last)
        self.act_redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.act_revert = A("Revert Changes", self.revert_edits)
        self.act_copy = A("Copy Edit Settings", self.copy_settings, QKeySequence.Copy)
        self.act_paste = A("Paste Edit Settings", self.paste_settings, QKeySequence.Paste)
        self.act_select_all = A("Select All", self.select_all, QKeySequence.SelectAll)
        self.act_select_none = A("Select None", self.select_none, "Ctrl+D")
        self.act_select_inverse = A("Select Inverse", self.select_inverse, "Ctrl+Shift+I")
        self.act_remove = A("Remove from Catalog…", self.remove_photos, QKeySequence.Delete)
        self.act_prefs = A("Preferences…", self.preferences, "Ctrl+,")
        self.act_prefs.setMenuRole(QAction.PreferencesRole)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addActions([self.act_import, self.act_refresh])
        m.addSeparator()
        m.addAction(self.act_quit)

        self.edit_menu = m = mb.addMenu("&Edit")
        m.aboutToShow.connect(self._update_edit_menu)
        m.addActions([self.act_undo, self.act_redo])
        m.addSeparator()
        m.addAction(self.act_revert)
        m.addSeparator()
        m.addActions([self.act_copy, self.act_paste])
        m.addSeparator()
        m.addActions([self.act_select_all, self.act_select_none, self.act_select_inverse])
        m.addSeparator()
        m.addActions([self.act_next, self.act_prev])
        m.addSeparator()
        self.edit_collections_menu = m.addMenu("Collections")
        self.edit_collections_menu.aboutToShow.connect(
            lambda: self._fill_collections_menu(self.edit_collections_menu))
        m.addSeparator()
        m.addAction(self.act_remove)
        m.addSeparator()
        m.addAction(self.act_prefs)
        self._update_edit_menu()

        m = mb.addMenu("&Photo")
        m.addActions(self.act_ratings)
        m.addSeparator()
        m.addActions([self.act_pick, self.act_reject, self.act_unflag])
        m.addSeparator()
        m.addActions([self.act_rot_l, self.act_rot_r, self.act_tag])

        self.collections_menu = mb.addMenu("&Collections")
        self.collections_menu.aboutToShow.connect(lambda: self._fill_collections_menu(self.collections_menu))

        m = mb.addMenu("&View")
        m.addActions([self.act_grid, self.act_loupe, self.act_compare, self.act_develop, self.act_zoom,
                      self.act_camera_jpeg])
        m.addActions([self.act_prev, self.act_next, self.act_swap, self.act_sync, self.act_panels, self.act_filmstrip])
        m.addSeparator()
        second = m.addMenu("Secondary Window")
        second.addAction(self.act_secondary)
        second.addSeparator()
        second.addActions([self.act_sec_grid, self.act_sec_detail])
        m.addSeparator()
        self.color_menu = m.addMenu("Color Management (OCIO)")
        self.color_menu.aboutToShow.connect(self._fill_color_menu)
        m.addActions([self.act_exp_up, self.act_exp_dn, self.act_exp_0])

        self.backup_menu = mb.addMenu("&Backup")
        self.backup_menu.aboutToShow.connect(self._fill_backup_menu)

    def _build_bottombar(self) -> None:
        self.act_rot_l.setIconText("⟲")
        self.act_rot_r.setIconText("⟳")
        self.act_copy.setIconText("Copy Settings")
        self.act_paste.setIconText("Paste Settings")
        self.bottombar = BottomBar(
            [(GRID, "Grid", "G"), (LOUPE, "Loupe", "E"), (COMPARE, "Compare", "C"), (DEVELOP, "Develop", "D")],
            [self.act_secondary, self.act_sec_grid, self.act_sec_detail],
            [self.act_rot_l, self.act_rot_r, self.act_copy, self.act_paste],
        )
        self.bottombar.modeClicked.connect(self.set_mode)
        self.bottombar.ratingClicked.connect(self.set_rating)
        self.bottombar.flagClicked.connect(self.set_flag)
        self.bottombar.zoomRequested.connect(self.zoom_to)
        for view in (self.loupe.pane.view, *(p.view for p in self.compare.panes), self.develop.view):
            view.viewChanged.connect(lambda *_: self._update_zoom())
        self._center_layout.addWidget(self.bottombar)
        self._sync_bar()

    def _active_view(self):
        mode = self.stack.currentIndex()
        if mode == LOUPE:
            return self.loupe.pane.view
        if mode == COMPARE:
            return self.compare.panes[self.compare.active].view
        if mode == DEVELOP:
            return self.develop.view
        return None

    def zoom_to(self, ratio) -> None:
        view = self._active_view()
        if view is not None:
            view.zoom_to(ratio)

    def _update_zoom(self) -> None:
        view = self._active_view()
        if view is not None and hasattr(self, "bottombar"):
            self.bottombar.show_zoom(view.zoom_percent())

    def _sync_bar(self) -> None:
        mode = self.stack.currentIndex()
        self.bottombar.set_mode(mode, mode != GRID)
        self._update_zoom()

    # -- data refresh ----------------------------------------------------

    def refresh_all(self) -> None:
        self.sidebar.refresh()
        self.refresh_grid()

    def refresh_grid(self) -> None:
        current = self.grid.current_record()
        selected = {r.id for r in self.grid.selected_records()}
        self.filterbar.apply_to(self.filter)
        records = self.catalog.query(self.filter)
        self.model.set_records(records)
        self.filterbar.count.setText(f"{len(records)} photos")
        self.inspector.set_known_tags([t.name for t in self.catalog.tags()])
        sel = self.grid.selectionModel()
        for image_id in selected:
            row = self.model.row_of(image_id)
            if row is not None:
                sel.select(self.model.index(row), QItemSelectionModel.Select)
        row = self.model.row_of(current.id) if current else None
        if row is None and records:
            row = 0
        if row is not None:
            flags = QItemSelectionModel.NoUpdate if selected else QItemSelectionModel.ClearAndSelect
            sel.setCurrentIndex(self.model.index(row), flags)
            self.grid.scrollTo(self.model.index(row))
        self._update_inspector()

    def _source_changed(self, kind: str, source_id) -> None:
        self.filter.source, self.filter.source_id = kind, source_id
        self.refresh_grid()

    def _reload(self, ids) -> None:
        """Re-read changed images from the catalog and push them to every view."""
        recs = [r for r in (self.catalog.get(i) for i in ids) if r]
        self.model.update_records(recs)
        by_id = {r.id: r for r in recs}
        cur = self.loupe.pane.record
        if cur and cur.id in by_id:
            self.loupe.show_record(by_id[cur.id])
        cur = self.develop.record
        if cur and cur.id in by_id:
            self.develop.show_record(by_id[cur.id])
            self.develop.refresh_settings()
        self.compare.refresh(by_id)
        self._update_inspector()

    # -- selection / modes ----------------------------------------------

    def target_records(self) -> list[ImageRecord]:
        """What a command acts on: grid selection, loupe image, or active compare side."""
        mode = self.stack.currentIndex()
        if mode in (LOUPE, DEVELOP):
            rec = self.loupe.pane.record if mode == LOUPE else self.develop.record
            return [rec] if rec else []
        if mode == COMPARE:
            rec = self.compare.active_record()
            return [rec] if rec else []
        recs = self.grid.selected_records()
        if not recs and self.grid.current_record():
            recs = [self.grid.current_record()]
        return recs

    def target_ids(self) -> list[int]:
        return [r.id for r in self.target_records()]

    def set_mode(self, mode: int) -> None:
        if mode == LOUPE:
            self.loupe.show_record(self.grid.current_record())
        elif mode == DEVELOP:
            self.develop.show_record(self.grid.current_record())
        elif mode == COMPARE:
            a, b = self._compare_pair()
            if a is None:
                self._sync_bar()  # stay put; undo the toolbar's button change
                return
            self.compare.show_records(a, b)
        self.stack.setCurrentIndex(mode)
        (self.grid if mode == GRID else self.stack.currentWidget()).setFocus()
        self._update_filmstrip()
        self._update_inspector()
        self._sync_bar()

    def _update_filmstrip(self) -> None:
        show = self.stack.currentIndex() != GRID and self.act_filmstrip.isChecked()
        self.filmstrip.setVisible(show)
        if show:
            self.filmstrip.center_on(self.grid.currentIndex())

    def _compare_pair(self) -> tuple[ImageRecord | None, ImageRecord | None]:
        sel = self.grid.selected_records()
        if len(sel) >= 2:
            return sel[0], sel[1]
        cur = self.grid.current_record()
        if cur is None:
            return None, None
        row = self.model.row_of(cur.id)
        nxt = self.model.records[row + 1] if row + 1 < len(self.model.records) else None
        return cur, nxt

    def _current_changed(self, current, _previous) -> None:
        mode = self.stack.currentIndex()
        rec = current.data(Qt.UserRole + 1) if current.isValid() else None
        if mode == LOUPE:
            self.loupe.show_record(rec)
        elif mode == DEVELOP:
            self.develop.show_record(rec)
        elif mode == COMPARE and rec is not None:
            self.compare.panes[self.compare.active].show_record(rec)  # filmstrip click / arrows: active side
        if self.filmstrip.isVisible() and current.isValid():
            if self.filmstrip.hasFocus():
                self.filmstrip.scrollTo(current)  # don't jump away from where the user clicked
            else:
                self.filmstrip.center_on(current)
        self._update_inspector()

    def _compare_active_changed(self, rec) -> None:
        """Clicking a compare side makes it current, so the filmstrip shows which photo is active."""
        row = self.model.row_of(rec.id) if rec is not None else None
        if self.stack.currentIndex() == COMPARE and row is not None:
            self.grid.selectionModel().setCurrentIndex(self.model.index(row), QItemSelectionModel.NoUpdate)
        self._update_inspector()
        self._update_zoom()

    def navigate(self, delta: int) -> None:
        if not self.model.records:
            return
        mode = self.stack.currentIndex()
        if mode == COMPARE:
            pane = self.compare.panes[self.compare.active]
            if pane.record is None:
                return
            row = self.model.row_of(pane.record.id)
            row = 0 if row is None else max(0, min(len(self.model.records) - 1, row + delta))
            # Moving "current" (not the selection) swaps the active side via _current_changed.
            self.grid.selectionModel().setCurrentIndex(self.model.index(row), QItemSelectionModel.NoUpdate)
            return
        row = self.grid.currentIndex().row()
        row = max(0, min(len(self.model.records) - 1, row + delta))
        idx = self.model.index(row)
        self.grid.selectionModel().setCurrentIndex(idx, QItemSelectionModel.ClearAndSelect)
        self.grid.scrollTo(idx)

    def toggle_zoom(self) -> None:
        mode = self.stack.currentIndex()
        if mode == GRID:
            self.set_mode(LOUPE)
            QTimer.singleShot(0, self.loupe.toggle_zoom)
        elif mode == LOUPE:
            self.loupe.toggle_zoom()
        elif mode == DEVELOP:
            self.develop.toggle_zoom()
        else:
            self.compare.toggle_zoom()

    def _toggle_camera_jpeg(self) -> None:
        if self.stack.currentIndex() != DEVELOP:
            self.set_mode(DEVELOP)
        self.develop.toggle_camera()

    def _toggle_sync(self, on: bool) -> None:
        self.compare.sync = on

    def _toggle_panels(self) -> None:
        visible = not self.docks[0].isVisible()
        for d in self.docks:
            d.setVisible(visible)

    def _update_inspector(self) -> None:
        mode = self.stack.currentIndex()
        if mode == COMPARE:
            rec = self.compare.active_record()
        elif mode == LOUPE:
            rec = self.loupe.pane.record
        elif mode == DEVELOP:
            rec = self.develop.record
        else:
            rec = self.grid.current_record()
        self.inspector.show_record(rec)
        if hasattr(self, "bottombar"):
            self.bottombar.show_record(rec)
        ids = self.target_ids()
        self.inspector.show_tags(self.catalog.tags_for(ids), len(ids))

    # -- commands --------------------------------------------------------

    def _command(self, label: str, command, ids=None) -> list[int]:
        """Run a change to the target photos as one undoable step."""
        ids = self.target_ids() if ids is None else ids
        if ids:
            self.undo.run(label, ids, lambda: command(ids))
            self._update_edit_menu()
        return ids

    def set_rating(self, rating: int) -> None:
        if ids := self._command("Rating", lambda ids: self.catalog.set_rating(ids, rating)):
            self._reload(ids)

    def set_flag(self, flag: int) -> None:
        label = {FLAG_PICK: "Pick", FLAG_REJECT: "Reject"}.get(flag, "Unflag")
        if ids := self._command(label, lambda ids: self.catalog.set_flag(ids, flag)):
            self._reload(ids)

    def rotate(self, degrees: int) -> None:
        label = "Rotate Right" if degrees > 0 else "Rotate Left"
        if ids := self._command(label, lambda ids: edits.rotate(self.catalog, ids, degrees)):
            self._reload(ids)

    def revert_edits(self) -> None:
        def revert(ids):
            for i in ids:
                if self.catalog.get_edit(i):
                    self.catalog.set_edit(i, {}, "Revert")

        if ids := self._command("Revert", revert):
            self._reload(ids)

    def copy_settings(self) -> None:
        recs = self.target_records()
        if recs:
            self._copied_settings = self.catalog.get_edit(recs[0].id)
            self.statusBar().showMessage(f"Copied edit settings from {recs[0].filename}", 3000)
            self._update_edit_menu()

    def paste_settings(self) -> None:
        if self._copied_settings is None:
            return
        settings = self._copied_settings

        def paste(ids):
            for i in ids:
                self.catalog.set_edit(i, dict(settings), "Paste settings")

        if ids := self._command("Paste Settings", paste):
            self._reload(ids)

    def undo_last(self) -> None:
        self._after_undo(self.undo.undo())

    def redo_last(self) -> None:
        self._after_undo(self.undo.redo())

    def _after_undo(self, step) -> None:
        if step is None:
            return
        self.sidebar.refresh()  # tag / collection counts
        if self.filter.source in ("collection", "tag"):
            self.refresh_grid()
        self._reload(list(step.before))
        self._update_edit_menu()

    # -- selection -------------------------------------------------------

    def select_all(self) -> None:
        self.grid.selectAll()

    def select_none(self) -> None:
        self.grid.selectionModel().clearSelection()

    def select_inverse(self) -> None:
        n = self.model.rowCount()
        if n:
            everything = QItemSelection(self.model.index(0), self.model.index(n - 1))
            self.grid.selectionModel().select(everything, QItemSelectionModel.Toggle)

    def _update_edit_menu(self) -> None:
        undo, redo = self.undo.undo_label(), self.undo.redo_label()
        self.act_undo.setText(f"Undo {undo}" if undo else "Undo")
        self.act_undo.setEnabled(undo is not None)
        self.act_redo.setText(f"Redo {redo}" if redo else "Redo")
        self.act_redo.setEnabled(redo is not None)
        self.act_paste.setEnabled(self._copied_settings is not None)
        n = len(self.target_ids())
        self.act_remove.setText(f"Remove {n} Photos from Catalog…" if n > 1 else "Remove Photo from Catalog…")
        self.act_remove.setEnabled(n > 0)

    # -- removing photos -------------------------------------------------

    def remove_photos(self) -> None:
        recs = self.target_records()
        if not recs:
            return
        what = f"{len(recs)} photos" if len(recs) > 1 else f"“{recs[0].filename}”"
        box = QMessageBox(QMessageBox.Question, "Remove from catalog",
                          f"Remove {what} from the catalog?\n\nTheir ratings, tags, collection entries and "
                          "edits are removed too. This cannot be undone.", QMessageBox.Cancel, self)
        trash = QCheckBox("Also move the files to the Recycle Bin")
        box.setCheckBox(trash)
        remove = box.addButton("Remove", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not remove:
            return
        failed = set()
        if trash.isChecked():
            failed = {r.path for r in recs if not QFile.moveToTrash(r.path)}
        self.catalog.remove_images([r.id for r in recs if r.path not in failed])
        self.refresh_all()
        if failed:
            QMessageBox.warning(self, "Not moved to Recycle Bin",
                                "These files could not be moved and were kept in the catalog:\n"
                                + "\n".join(sorted(failed)[:20]))

    # -- preferences -----------------------------------------------------

    def preferences(self) -> None:
        old_cache, old_ocio = self.prefs.cache_dir(self.paths), self.prefs.ocio_config
        if PreferencesDialog(self.prefs, self.paths, self.service.cache, self).exec():
            if self.prefs.cache_dir(self.paths) != old_cache:
                self.service.set_cache(self._make_cache())
            self.service.cache.limit_bytes = self.prefs.cache_limit_gb * GB
            self.service.trim_cache()
            self._apply_performance()
            if self.sidebar.event_gap_hours != self.prefs.event_gap_hours:
                self.sidebar.event_gap_hours = self.prefs.event_gap_hours
                self.sidebar.refresh()
            if self.prefs.ocio_config != old_ocio:
                self.color.load_config(self.prefs.ocio_config or None)
                if self.color.error:
                    QMessageBox.warning(self, "OCIO config", self.color.error)

    def _make_cache(self) -> DiskCache:
        return DiskCache(self.prefs.cache_dir(self.paths), limit_bytes=self.prefs.cache_limit_gb * GB)

    def _apply_performance(self) -> None:
        self.service.set_threads(self.prefs.decode_threads)
        self.service.memory_limits[dec.THUMB] = self.prefs.thumbs_in_memory

    def focus_tags(self) -> None:
        self.docks[1].show()
        self.inspector.tag_edit.setFocus()

    def _add_tags(self, names: list[str]) -> None:
        if self._command("Add Tags", lambda ids: self.catalog.add_tags(ids, names)):
            self.sidebar.refresh()
            self.inspector.set_known_tags([t.name for t in self.catalog.tags()])
            self._update_inspector()

    def _remove_tag(self, name: str) -> None:
        if self._command("Remove Tag", lambda ids: self.catalog.remove_tag(ids, name)):
            self.sidebar.refresh()
            self._update_inspector()

    def new_collection(self, add_ids=None) -> int | None:
        name, ok = QInputDialog.getText(self, "New collection", "Name")
        if not ok or not name.strip():
            return None
        cid = self.catalog.create_collection(name.strip())
        if add_ids:
            self.catalog.add_to_collection(cid, add_ids)
        self.sidebar.refresh()
        return cid

    def add_to_collection(self, cid: int, ids=None) -> None:
        if ids := self._command("Add to Collection", lambda ids: self.catalog.add_to_collection(cid, ids), ids):
            self.sidebar.refresh()
            self.statusBar().showMessage(f"Added {len(ids)} to collection", 3000)

    def add_to_target(self) -> None:
        cid = self.sidebar.target_collection
        if cid is None:
            cid = self.new_collection()
            if cid is None:
                return
            self._set_target_collection(cid)
        self.add_to_collection(cid)

    def _set_target_collection(self, cid: int | None) -> None:
        self.sidebar.target_collection = cid
        self.settings.setValue("target_collection", cid or 0)
        self.sidebar.refresh()

    def remove_from_collection(self) -> None:
        if self.filter.source == "collection":
            cid = self.filter.source_id
            self._command("Remove from Collection", lambda ids: self.catalog.remove_from_collection(cid, ids))
            self.sidebar.refresh()
            self.refresh_grid()

    def _fill_collections_menu(self, m: QMenu) -> None:
        m.clear()
        m.addAction(self.act_new_coll)
        m.addAction(self.act_target)
        m.addSeparator()
        add = m.addMenu("Add Selection To")
        for c in self.catalog.collections():
            add.addAction(c.name, partial(self.add_to_collection, c.id))
        if self.filter.source == "collection":
            m.addAction("Remove Selection From This Collection", self.remove_from_collection)

    # -- context menus ---------------------------------------------------

    def _grid_menu(self, pos, view=None) -> None:
        view = view or self.grid
        recs = self.grid.selected_records()
        if not recs:
            return
        m = QMenu(self)
        add = m.addMenu("Add to Collection")
        for c in self.catalog.collections():
            add.addAction(c.name, partial(self.add_to_collection, c.id))
        add.addSeparator()
        add.addAction("New Collection…", lambda: self.new_collection([r.id for r in recs]))
        if self.filter.source == "collection":
            m.addAction("Remove from This Collection", self.remove_from_collection)
        m.addSeparator()
        m.addActions([self.act_rot_l, self.act_rot_r])
        m.addSeparator()
        backup = m.addMenu("Back Up Selection To")
        for t in list_targets(self.catalog):
            backup.addAction(t.name, partial(self.start_backup, t.id, [r.id for r in recs]))
        backup.setEnabled(not backup.isEmpty())
        m.addAction("Show in File Manager", lambda: reveal_in_file_manager(recs[0].path))
        m.exec(view.viewport().mapToGlobal(pos))

    def _sidebar_menu(self, pos) -> None:
        item = self.sidebar.itemAt(pos)
        section = self.sidebar.section_of(item)
        src = self.sidebar.item_source(item)
        m = QMenu(self)
        if section == "folders" and src:
            path = self.catalog.folder_path(src[1])
            m.addAction("Import New Files", lambda: self.start_import(path))
            m.addAction("Show in File Manager", lambda: reveal_in_file_manager(path))
            m.addAction("Remove from Catalog…", lambda: self._remove_folder(src[1], path))
        elif section == "collections":
            m.addAction("New Collection…", self.new_collection)
            if src:
                m.addAction("Set as Target Collection (B)", lambda: self._set_target_collection(src[1]))
                m.addAction("Rename…", lambda: self._rename_collection(src[1], item.text(0)))
                m.addAction("Delete", lambda: self._delete_collection(src[1]))
        elif section == "tags" and src:
            m.addAction("Delete Tag", lambda: self._delete_tag(src[1]))
        elif section == "events" and src and src[0] == "event":
            m.addAction("Rename Event…", lambda: self._rename_event(src[1], item.text(0)))
            m.addAction("Reset Name", lambda: self._name_event(src[1], ""))
        if not m.isEmpty():
            m.exec(self.sidebar.viewport().mapToGlobal(pos))

    def _remove_folder(self, folder_id: int, path: str) -> None:
        msg = f"Remove “{path}” and its subfolders from the catalog?\nFiles on disk are not touched."
        if QMessageBox.question(self, "Remove folder", msg) == QMessageBox.Yes:
            self.catalog.remove_folder(folder_id)
            self.refresh_all()

    def _rename_collection(self, cid: int, old: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename collection", "Name", text=old.rstrip(" +"))
        if ok and name.strip():
            self.catalog.rename_collection(cid, name.strip())
            self.sidebar.refresh()

    def _delete_collection(self, cid: int) -> None:
        if QMessageBox.question(self, "Delete collection", "Delete this collection? Photos stay in the catalog.") == QMessageBox.Yes:
            self.catalog.delete_collection(cid)
            if self.sidebar.target_collection == cid:
                self._set_target_collection(None)
            self.refresh_all()

    def _rename_event(self, key: str, current: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename event", "Name (leave empty to show the date)", text=current)
        if ok:
            self._name_event(key, name)

    def _name_event(self, key: str, name: str) -> None:
        start, _, end = key.partition("|")
        self.catalog.name_event(start, end, name)
        self.sidebar.refresh()

    def _delete_tag(self, tag_id: int) -> None:
        self.catalog.delete_tag(tag_id)
        self.refresh_all()

    # -- color -----------------------------------------------------------

    def _fill_color_menu(self) -> None:
        m = self.color_menu
        m.clear()
        info = m.addAction(f"Config: {self.color.config_path}")
        info.setEnabled(False)
        for title, options, current, setter in (
            ("Display", self.color.displays(), self.color.display, self.color.set_display),
            ("View", self.color.views(), self.color.view, self.color.set_view),
            ("Preview Input Space", self.color.input_spaces(), self.color.input_space, self.color.set_input),
        ):
            sub = m.addMenu(title)
            group = QActionGroup(sub)
            for name in options:
                a = sub.addAction(name, partial(self._set_color, setter, name))
                a.setCheckable(True)
                a.setChecked(name == current)
                group.addAction(a)

    def _set_color(self, setter, value) -> None:
        setter(value)
        self._save_color()

    def _exposure(self, delta) -> None:
        self.color.set_exposure(0.0 if delta is None else self.color.exposure + delta)
        self.statusBar().showMessage(f"Viewer exposure {self.color.exposure:+.1f} EV (display only)", 2000)

    def _restore_color(self) -> None:
        s = self.settings
        if s.value("ocio/input") in self.color.input_spaces():
            self.color.input_space = s.value("ocio/input")
        if s.value("ocio/display") in self.color.displays():
            self.color.display = s.value("ocio/display")
            if s.value("ocio/view") in self.color.views():
                self.color.view = s.value("ocio/view")

    def _save_color(self) -> None:
        self.settings.setValue("ocio/input", self.color.input_space)
        self.settings.setValue("ocio/display", self.color.display)
        self.settings.setValue("ocio/view", self.color.view)

    # -- import / backup -------------------------------------------------

    def import_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Import folder", self.settings.value("last_import", ""))
        if folder:
            self.settings.setValue("last_import", folder)
            self.start_import(folder)

    def start_import(self, folder: str) -> None:
        if not self._can_start():
            return
        self._start_worker(ImportWorker(self.paths.db, folder, self), f"Importing {os.path.basename(folder)}…")

    def _fill_backup_menu(self) -> None:
        m = self.backup_menu
        m.clear()
        m.addAction(self.act_backup_dlg)
        m.addSeparator()
        targets = list_targets(self.catalog)
        for t in targets:
            m.addAction(f"Back Up Everything to {t.name}", partial(self.start_backup, t.id, None))
        for t in targets:
            m.addAction(f"Back Up Selection to {t.name}", lambda t=t: self.start_backup(t.id, self.target_ids()))
        if not targets:
            m.addAction("No targets yet — add one above").setEnabled(False)

    def backup_dialog(self) -> None:
        BackupDialog(self.catalog, self).exec()

    def start_backup(self, target_id: int, image_ids=None) -> None:
        if not self._can_start():
            return
        self._start_worker(BackupWorker(self.paths.db, target_id, image_ids, self), "Backing up…")

    def _can_start(self) -> bool:
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Busy", "Wait for the current import or backup to finish.")
            return False
        return True

    def _start_worker(self, worker, label: str) -> None:
        self.worker = worker
        self.progress_label.setText(label)
        self.progress.setRange(0, 0)
        for w in (self.progress_label, self.progress, self.cancel_button):
            w.show()
        worker.progress.connect(self._on_progress)
        worker.finishedWith.connect(self._on_worker_done)
        worker.start()

    def _on_progress(self, done: int, total: int, path: str) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress.setRange(0, 0)
        self.progress.setFormat(f"{done}" + (f"/{total}" if total else ""))
        self.statusBar().showMessage(os.path.basename(path))

    def _on_worker_done(self, result, error: str) -> None:
        for w in (self.progress_label, self.progress, self.cancel_button):
            w.hide()
        kind = "Backup" if isinstance(self.worker, BackupWorker) else "Import"
        self.worker = None
        if error:
            QMessageBox.warning(self, f"{kind} failed", error)
        elif kind == "Import":
            self.statusBar().showMessage(
                f"Imported {result.added} new ({result.skipped} already in catalog, {result.failed} unreadable)", 8000
            )
        else:
            self.statusBar().showMessage(f"Backup: {result.summary()}", 10000)
            if result.errors:
                QMessageBox.warning(self, "Backup problems", "\n".join(result.errors[:30]))
        self.refresh_all()
        if kind == "Import" and not error and result.import_id:
            self.sidebar.select_source("import", result.import_id)  # show what just came in

    # -- secondary window ------------------------------------------------

    def _ensure_secondary(self) -> sec.SecondaryWindow:
        if self.secondary is None:
            self.secondary = sec.SecondaryWindow(
                self.model, self.grid.selectionModel(), self.service, self.color, self.settings, self)
            # Ratings, flags, arrows and the like work while the secondary window has focus too.
            self.secondary.addActions([a for a in self.actions() if a is not self.act_back])
            self.secondary.modeChanged.connect(lambda _: self._sync_secondary_actions())
            self.secondary.visibilityChanged.connect(lambda _: self._sync_secondary_actions())
        return self.secondary

    def show_secondary(self, mode: int | None = None) -> None:
        win = self._ensure_secondary()
        if mode is not None:
            win.set_mode(mode)
        if not win.isVisible():
            win.show_on_other_screen(self)
        self._sync_secondary_actions()

    def toggle_secondary(self) -> None:
        if self.secondary is not None and self.secondary.isVisible():
            self.secondary.close()
        else:
            self.show_secondary()

    def _sync_secondary_actions(self) -> None:
        win = self.secondary
        self.act_secondary.setChecked(bool(win and win.isVisible()))
        mode = win.mode() if win else int(self.settings.value("secondary/mode", sec.DETAIL))
        (self.act_sec_grid if mode == sec.GRID else self.act_sec_detail).setChecked(True)

    # -- window state ----------------------------------------------------

    def _restore_state(self) -> None:
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(1500, 950)
        state = self.settings.value("window_state")
        if state:
            self.restoreState(state)
        size = int(self.settings.value("cell_size", 200))
        self.filterbar.size.setValue(size)
        self.grid.set_cell_size(size)
        split = self.settings.value("filmstrip/splitter")
        if split:
            self.splitter.restoreState(split)
        self.act_filmstrip.setChecked(self.settings.value("filmstrip/visible", True, type=bool))
        self._sync_secondary_actions()
        if self.settings.value("secondary/visible", False, type=bool):
            QTimer.singleShot(0, self.show_secondary)

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(10000)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("window_state", self.saveState())
        self.settings.setValue("cell_size", self.filterbar.size.value())
        self.settings.setValue("filmstrip/splitter", self.splitter.saveState())
        self.settings.setValue("filmstrip/visible", self.act_filmstrip.isChecked())
        if self.secondary is not None:
            self.secondary.save_state()  # remembers whether it was open
            self.secondary.hide()  # hide, not close: closing would record it as closed
        self.service.shutdown()
        self.raw_loader.shutdown()
        self.catalog.close()
        super().closeEvent(event)
