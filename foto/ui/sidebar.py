"""Left panel: library sources (all, folders, collections, tags)."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from foto.catalog import Catalog

SourceRole = Qt.UserRole + 1  # ("all" | "folder" | "collection" | "tag", id)


def _short(path: str) -> str:
    """Last two path components; the full path is in the tooltip."""
    parent, name = os.path.split(path.rstrip("/\\"))
    return os.path.join(os.path.basename(parent), name) if parent else path


class Sidebar(QTreeWidget):
    sourceChanged = Signal(str, object)  # kind, id

    def __init__(self, catalog: Catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.setHeaderHidden(True)
        self.setColumnCount(2)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setRootIsDecorated(True)
        self.setMinimumWidth(230)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, self.header().ResizeMode.Stretch)
        self.target_collection: int | None = None
        self.currentItemChanged.connect(self._on_current)
        self.refresh()

    def refresh(self) -> None:
        current = self.current_source()
        self.blockSignals(True)
        self.clear()
        total = self.catalog.count()
        self._all = self._item(None, "All Photographs", total, ("all", None))
        self._folders = self._section("Folders")
        for f in self.catalog.folders():
            it = self._item(self._folders, _short(f.name), f.count, ("folder", f.id))
            it.setToolTip(0, f.name)
        self._collections = self._section("Collections")
        for c in self.catalog.collections():
            name = c.name + ("  +" if c.id == self.target_collection else "")
            self._item(self._collections, name, c.count, ("collection", c.id))
        self._tags = self._section("Tags")
        for t in self.catalog.tags():
            self._item(self._tags, t.name, t.count, ("tag", t.id))
        self.expandAll()
        self.resizeColumnToContents(1)
        self.blockSignals(False)
        self.select_source(*current)

    def _section(self, title: str) -> QTreeWidgetItem:
        it = QTreeWidgetItem(self, [title])
        it.setFlags(Qt.ItemIsEnabled)
        f = it.font(0)
        f.setBold(True)
        it.setFont(0, f)
        return it

    def _item(self, parent, name, count, source) -> QTreeWidgetItem:
        it = QTreeWidgetItem(parent or self, [name, str(count)])
        it.setData(0, SourceRole, source)
        it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        return it

    def current_source(self) -> tuple[str, int | None]:
        it = self.currentItem()
        src = it.data(0, SourceRole) if it else None
        return tuple(src) if src else ("all", None)

    def select_source(self, kind: str, source_id) -> None:
        for it in self._iter_items():
            src = it.data(0, SourceRole)
            if src and src[0] == kind and src[1] == source_id:
                self.setCurrentItem(it)
                return
        self.setCurrentItem(self._all)

    def _iter_items(self):
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            it = stack.pop(0)
            yield it
            stack.extend(it.child(i) for i in range(it.childCount()))

    def _on_current(self, item, _prev) -> None:
        src = item.data(0, SourceRole) if item else None
        if src:
            self.sourceChanged.emit(src[0], src[1])

    def item_source(self, item) -> tuple[str, int | None] | None:
        src = item.data(0, SourceRole) if item else None
        return tuple(src) if src else None

    def section_of(self, item) -> str | None:
        while item is not None and item.parent() is not None:
            item = item.parent()
        return {id(self._folders): "folders", id(self._collections): "collections", id(self._tags): "tags"}.get(
            id(item)
        )
