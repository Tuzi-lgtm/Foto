"""Main window: wires catalog, grid, loupe, compare, panels, import and backup."""

from __future__ import annotations

import os
import subprocess
import sys
from functools import partial

from PySide6.QtCore import QItemSelectionModel, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget, QFileDialog, QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from foto import edits
from foto.backup.targets import list_targets
from foto.catalog import Catalog, ImageRecord, LibraryFilter
from foto.catalog.catalog import FLAG_NONE, FLAG_PICK, FLAG_REJECT
from foto.color.ocio import ColorManager
from foto.config import CatalogPaths
from foto.imaging.cache import DiskCache
from foto.imaging.service import ImageService
from foto.ui.backup_dialog import BackupDialog
from foto.ui.compare import CompareView, LoupeView
from foto.ui.filterbar import FilterBar
from foto.ui.grid import GridView, ImageListModel
from foto.ui.inspector import Inspector
from foto.ui.sidebar import Sidebar
from foto.ui.workers import BackupWorker, ImportWorker

GRID, LOUPE, COMPARE = 0, 1, 2


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
        self.catalog = Catalog.open(paths.db)
        self.service = ImageService(DiskCache(paths.cache), self)
        self.color = ColorManager(parent=self)
        self._restore_color()
        self.filter = LibraryFilter()
        self.worker = None

        self.setWindowTitle(f"Foto — {paths.root.name}")
        self._build_widgets()
        self._build_actions()
        self._build_menus()
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

        self.loupe = LoupeView(self.service, self.color)
        self.compare = CompareView(self.service, self.color)
        self.compare.activeChanged.connect(lambda _: self._update_inspector())
        for view in (self.loupe, self.compare):
            view.backgroundDoubleClicked.connect(partial(self.set_mode, GRID))

        self.stack = QStackedWidget()
        for w in (self.grid, self.loupe, self.compare):
            self.stack.addWidget(w)

        self.filterbar = FilterBar()
        self.filterbar.changed.connect(self.refresh_grid)
        self.filterbar.cellSizeChanged.connect(self.grid.set_cell_size)

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.filterbar)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(center)

        self.sidebar = Sidebar(self.catalog)
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
        self.act_back = A("Back to Grid", partial(self.set_mode, GRID), "Esc")
        self.act_zoom = A("Toggle Fit / 1:1", self.toggle_zoom, "Z")
        self.act_prev = A("Previous Photo", partial(self.navigate, -1), "Left")
        self.act_next = A("Next Photo", partial(self.navigate, 1), "Right")
        self.act_swap = A("Swap Compare Sides", self.compare.swap, "Ctrl+Shift+S")
        self.act_sync = A("Sync Compare Zoom", self._toggle_sync, None, True)
        self.act_sync.setChecked(True)
        self.act_panels = A("Toggle Panels", self._toggle_panels, "Tab")
        self.act_exp_up = A("Viewer Exposure +½", partial(self._exposure, 0.5), "Ctrl+=")
        self.act_exp_dn = A("Viewer Exposure −½", partial(self._exposure, -0.5), "Ctrl+-")
        self.act_exp_0 = A("Reset Viewer Exposure", partial(self._exposure, None), "Ctrl+0")
        self.act_backup_dlg = A("Backup Targets…", self.backup_dialog)
        self.act_refresh = A("Refresh", self.refresh_all, "F5")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addActions([self.act_import, self.act_refresh])
        m.addSeparator()
        m.addAction(self.act_quit)

        m = mb.addMenu("&Photo")
        m.addActions(self.act_ratings)
        m.addSeparator()
        m.addActions([self.act_pick, self.act_reject, self.act_unflag])
        m.addSeparator()
        m.addActions([self.act_rot_l, self.act_rot_r, self.act_tag])

        self.collections_menu = mb.addMenu("&Collections")
        self.collections_menu.aboutToShow.connect(self._fill_collections_menu)

        m = mb.addMenu("&View")
        m.addActions([self.act_grid, self.act_loupe, self.act_compare, self.act_zoom])
        m.addActions([self.act_prev, self.act_next, self.act_swap, self.act_sync, self.act_panels])
        m.addSeparator()
        self.color_menu = m.addMenu("Color Management (OCIO)")
        self.color_menu.aboutToShow.connect(self._fill_color_menu)
        m.addActions([self.act_exp_up, self.act_exp_dn, self.act_exp_0])

        self.backup_menu = mb.addMenu("&Backup")
        self.backup_menu.aboutToShow.connect(self._fill_backup_menu)

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
        self.compare.refresh(by_id)
        self._update_inspector()

    # -- selection / modes ----------------------------------------------

    def target_records(self) -> list[ImageRecord]:
        """What a command acts on: grid selection, loupe image, or active compare side."""
        mode = self.stack.currentIndex()
        if mode == LOUPE:
            rec = self.loupe.pane.record
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
        elif mode == COMPARE:
            a, b = self._compare_pair()
            if a is None:
                return
            self.compare.show_records(a, b)
        self.stack.setCurrentIndex(mode)
        (self.grid if mode == GRID else self.stack.currentWidget()).setFocus()
        self._update_inspector()

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
        if self.stack.currentIndex() == LOUPE:
            self.loupe.show_record(current.data(Qt.UserRole + 1) if current.isValid() else None)
        self._update_inspector()

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
            pane.show_record(self.model.records[row])
            self._update_inspector()
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
        else:
            self.compare.toggle_zoom()

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
        else:
            rec = self.grid.current_record()
        self.inspector.show_record(rec)
        ids = self.target_ids()
        self.inspector.show_tags(self.catalog.tags_for(ids), len(ids))

    # -- commands --------------------------------------------------------

    def set_rating(self, rating: int) -> None:
        ids = self.target_ids()
        if ids:
            self.catalog.set_rating(ids, rating)
            self._reload(ids)

    def set_flag(self, flag: int) -> None:
        ids = self.target_ids()
        if ids:
            self.catalog.set_flag(ids, flag)
            self._reload(ids)

    def rotate(self, degrees: int) -> None:
        ids = self.target_ids()
        if ids:
            edits.rotate(self.catalog, ids, degrees)
            self._reload(ids)

    def focus_tags(self) -> None:
        self.docks[1].show()
        self.inspector.tag_edit.setFocus()

    def _add_tags(self, names: list[str]) -> None:
        ids = self.target_ids()
        if ids:
            self.catalog.add_tags(ids, names)
            self.sidebar.refresh()
            self.inspector.set_known_tags([t.name for t in self.catalog.tags()])
            self._update_inspector()

    def _remove_tag(self, name: str) -> None:
        ids = self.target_ids()
        if ids:
            self.catalog.remove_tag(ids, name)
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
        ids = ids if ids is not None else self.target_ids()
        if ids:
            self.catalog.add_to_collection(cid, ids)
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
            self.catalog.remove_from_collection(self.filter.source_id, self.target_ids())
            self.sidebar.refresh()
            self.refresh_grid()

    def _fill_collections_menu(self) -> None:
        m = self.collections_menu
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

    def _grid_menu(self, pos) -> None:
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
        m.exec(self.grid.viewport().mapToGlobal(pos))

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

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(10000)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("window_state", self.saveState())
        self.settings.setValue("cell_size", self.filterbar.size.value())
        self.service.shutdown()
        self.catalog.close()
        super().closeEvent(event)
