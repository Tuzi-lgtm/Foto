"""Left panel: library sources (all, recent imports, dates, folders, collections, tags)."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from itertools import groupby

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from foto.catalog import Catalog

SourceRole = Qt.UserRole + 1  # ("all" | "import" | "date" | "folder" | "collection" | "tag", id)


def _short(path: str) -> str:
    """Last two path components; the full path is in the tooltip."""
    parent, name = os.path.split(path.rstrip("/\\"))
    return os.path.join(os.path.basename(parent), name) if parent else path


def import_label(started_at: str, now: datetime | None = None) -> str:
    """"12 minutes ago" for today's imports, "January 25, 9:16 PM" before that. started_at is UTC."""
    when = datetime.fromisoformat(started_at).replace(tzinfo=timezone.utc).astimezone()
    now = now or datetime.now().astimezone()
    minutes = int((now - when).total_seconds() // 60)
    if minutes < 1:
        return "Just now"
    if minutes < 60:
        return f"{minutes} minute{'s' * (minutes != 1)} ago"
    if minutes < 24 * 60:
        return f"{minutes // 60} hour{'s' * (minutes >= 120)} ago"
    hour = when.hour % 12 or 12
    year = f", {when.year}" if when.year != now.year else ""
    return f"{when:%B} {when.day}{year}, {hour}:{when:%M} {'AM' if when.hour < 12 else 'PM'}"


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
        self._expanded: dict[tuple, bool] | None = None  # None until first build
        self.currentItemChanged.connect(self._on_current)
        self.refresh()

    def refresh(self) -> None:
        current = self.current_source()
        first = self._expanded is None
        if not first:
            self._expanded = {self._key(it): it.isExpanded() for it in self._iter_items() if it.childCount()}
        self.blockSignals(True)
        self.clear()
        total = self.catalog.count()
        self._all = self._item(None, "All Photographs", total, ("all", None))
        self._recent = self._section("Recently Added")
        for b in self.catalog.imports():
            it = self._item(self._recent, import_label(b.started_at), b.count, ("import", b.id))
            it.setToolTip(0, b.root)
        self._dates = self._section("By Date")
        self._build_dates()
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
        self._restore_expanded(first)
        self.resizeColumnToContents(1)
        self.blockSignals(False)
        self.select_source(*current)

    def _build_dates(self) -> None:
        """Year (newest first) > month > day, derived from capture time; nothing on disk changes."""
        days = [(date.fromisoformat(d), n) for d, n in self.catalog.day_counts() if _is_day(d)]
        for year, in_year in groupby(sorted(days, key=lambda x: -x[0].year), key=lambda x: x[0].year):
            in_year = list(in_year)
            y = self._item(self._dates, str(year), sum(n for _, n in in_year), ("date", f"{year:04}"))
            for month, in_month in groupby(in_year, key=lambda x: x[0].month):
                in_month = list(in_month)
                m = self._item(y, f"{in_month[0][0]:%B}", sum(n for _, n in in_month), ("date", f"{year:04}-{month:02}"))
                for d, n in in_month:
                    self._item(m, f"{d:%A}, {d:%b} {d.day}", n, ("date", d.isoformat()))

    def _restore_expanded(self, first: bool) -> None:
        """Sections start open and date nodes closed (except the newest year); after that, keep what the user did."""
        newest_year = self._dates.child(0)
        for it in self._iter_items():
            if not it.childCount():
                continue
            default = it.data(0, SourceRole) is None or (first and it is newest_year)
            it.setExpanded(default if first else self._expanded.get(self._key(it), default))
        if first:
            self._expanded = {}

    @staticmethod
    def _key(item) -> tuple:
        src = item.data(0, SourceRole)
        return tuple(src) if src else ("section", item.text(0))

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

    def current_source(self) -> tuple[str, int | str | None]:
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

    def item_source(self, item) -> tuple[str, int | str | None] | None:
        src = item.data(0, SourceRole) if item else None
        return tuple(src) if src else None

    def section_of(self, item) -> str | None:
        while item is not None and item.parent() is not None:
            item = item.parent()
        return {
            id(self._recent): "recent", id(self._dates): "dates", id(self._folders): "folders",
            id(self._collections): "collections", id(self._tags): "tags",
        }.get(id(item))


def _is_day(value: str | None) -> bool:
    try:
        date.fromisoformat(value or "")
        return True
    except ValueError:
        return False
