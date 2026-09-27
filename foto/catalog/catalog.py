"""Catalog API: everything Foto knows about your photos lives here.

Originals are never touched; ratings, flags, tags, collections and edit
settings are stored only in the catalog database.

A Catalog wraps one SQLite connection and must only be used from the thread
that opened it. Background workers open their own Catalog on the same file.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from foto.catalog import db

FLAG_REJECT, FLAG_NONE, FLAG_PICK = -1, 0, 1


@dataclass
class ImageRecord:
    id: int
    folder_id: int
    path: str
    filename: str
    ext: str
    file_size: int
    mtime_ns: int
    capture_time: str | None
    make: str | None
    model: str | None
    lens: str | None
    iso: int | None
    shutter: float | None
    aperture: float | None
    focal: float | None
    width: int | None
    height: int | None
    rating: int
    flag: int
    rotate: int = 0
    backed_up: bool = False


@dataclass
class LibraryFilter:
    source: str = "all"  # all | folder | collection | tag | import | date
    source_id: int | str | None = None  # date: "YYYY", "YYYY-MM" or "YYYY-MM-DD"
    min_rating: int = 0  # 0 = any
    unrated_only: bool = False
    flag: str = "all"  # all | picked | unflagged | rejected | not_rejected
    backup: str = "any"  # any | backed_up | not_backed_up
    text: str = ""
    sort: str = "capture_time"  # capture_time | filename | imported | rating
    descending: bool = False


@dataclass
class NamedCount:
    id: int
    name: str
    count: int


@dataclass
class ImportBatch:
    id: int
    root: str
    started_at: str  # UTC, ISO 8601
    count: int


# Columns selected for ImageRecord, in dataclass order.
_IMAGE_COLS = (
    "i.id, i.folder_id, i.path, i.filename, i.ext, i.file_size, i.mtime_ns, "
    "i.capture_time, i.make, i.model, i.lens, i.iso, i.shutter, i.aperture, i.focal, "
    "i.width, i.height, i.rating, i.flag, "
    "COALESCE(json_extract(e.settings, '$.rotate'), 0) AS rotate, "
    "EXISTS (SELECT 1 FROM backup_records b WHERE b.image_id = i.id) AS backed_up"
)
_IMAGE_FROM = "images i LEFT JOIN edits e ON e.image_id = i.id"
_SHOT_AT = "COALESCE(i.capture_time, i.imported_at)"

_SORTS = {
    "capture_time": ["COALESCE(i.capture_time, i.imported_at)", "i.filename"],
    "filename": ["i.filename COLLATE NOCASE", "i.id"],
    "imported": ["i.imported_at", "i.id"],
    "rating": ["i.rating", "COALESCE(i.capture_time, i.imported_at)"],
}

_FLAG_SQL = {
    "picked": f"i.flag = {FLAG_PICK}",
    "unflagged": f"i.flag = {FLAG_NONE}",
    "rejected": f"i.flag = {FLAG_REJECT}",
    "not_rejected": f"i.flag != {FLAG_REJECT}",
}


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _chunks(ids: Sequence[int], size: int = 500) -> Iterable[Sequence[int]]:
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


class Catalog:
    def __init__(self, conn: sqlite3.Connection, path: Path | None = None):
        self.conn = conn
        self.path = path

    @classmethod
    def open(cls, path: Path | str) -> "Catalog":
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return cls(db.connect(path), None if str(path) == ":memory:" else Path(path))

    def close(self) -> None:
        self.conn.close()

    # -- folders ---------------------------------------------------------

    def add_folder(self, path: str) -> int:
        path = os.path.normpath(path)
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO folders(path) VALUES (?)", (path,))
        return self.conn.execute("SELECT id FROM folders WHERE path = ?", (path,)).fetchone()[0]

    def folders(self) -> list[NamedCount]:
        rows = self.conn.execute(
            "SELECT f.id, f.path, COUNT(i.id) FROM folders f "
            "LEFT JOIN images i ON i.folder_id = f.id GROUP BY f.id ORDER BY f.path"
        ).fetchall()
        return [NamedCount(*r) for r in rows]

    def folder_path(self, folder_id: int) -> str | None:
        row = self.conn.execute("SELECT path FROM folders WHERE id = ?", (folder_id,)).fetchone()
        return row[0] if row else None

    def _folder_tree_ids(self, folder_id: int) -> list[int]:
        """The folder plus every folder below it on disk."""
        root = self.folder_path(folder_id)
        if root is None:
            return []
        prefix = _like_escape(root.rstrip("/\\") + os.sep) + "%"
        rows = self.conn.execute(
            "SELECT id FROM folders WHERE id = ? OR path LIKE ? ESCAPE '\\'", (folder_id, prefix)
        ).fetchall()
        return [r[0] for r in rows]

    def remove_folder(self, folder_id: int) -> None:
        """Forget a folder tree. Files on disk are left alone."""
        ids = self._folder_tree_ids(folder_id)
        with self.conn:
            self.conn.execute(f"DELETE FROM folders WHERE id IN ({_placeholders(len(ids))})", ids)

    # -- images ----------------------------------------------------------

    def known_paths(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT path FROM images")}

    def add_images(self, rows: Iterable[dict]) -> int:
        """Insert image rows (dicts keyed by column name). Existing paths are skipped."""
        cols = (
            "folder_id", "path", "filename", "ext", "file_size", "mtime_ns", "capture_time",
            "make", "model", "lens", "iso", "shutter", "aperture", "focal", "width", "height", "import_id",
        )
        sql = f"INSERT OR IGNORE INTO images({','.join(cols)}) VALUES ({_placeholders(len(cols))})"
        before = self.conn.total_changes
        with self.conn:
            self.conn.executemany(sql, ([r.get(c) for c in cols] for r in rows))
        return self.conn.total_changes - before

    def get(self, image_id: int) -> ImageRecord | None:
        row = self.conn.execute(
            f"SELECT {_IMAGE_COLS} FROM {_IMAGE_FROM} WHERE i.id = ?", (image_id,)
        ).fetchone()
        return self._record(row) if row else None

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]

    def query(self, f: LibraryFilter | None = None) -> list[ImageRecord]:
        f = f or LibraryFilter()
        where: list[str] = []
        args: list = []

        if f.source == "folder" and f.source_id is not None:
            ids = self._folder_tree_ids(f.source_id) or [-1]
            where.append(f"i.folder_id IN ({_placeholders(len(ids))})")
            args += ids
        elif f.source == "collection" and f.source_id is not None:
            where.append("i.id IN (SELECT image_id FROM collection_images WHERE collection_id = ?)")
            args.append(f.source_id)
        elif f.source == "tag" and f.source_id is not None:
            where.append("i.id IN (SELECT image_id FROM image_tags WHERE tag_id = ?)")
            args.append(f.source_id)
        elif f.source == "import" and f.source_id is not None:
            where.append("i.import_id = ?")
            args.append(f.source_id)
        elif f.source == "date" and f.source_id:
            where.append(f"substr({_SHOT_AT}, 1, ?) = ?")
            args += [len(f.source_id), f.source_id]

        if f.unrated_only:
            where.append("i.rating = 0")
        elif f.min_rating > 0:
            where.append("i.rating >= ?")
            args.append(f.min_rating)

        if f.flag in _FLAG_SQL:
            where.append(_FLAG_SQL[f.flag])

        if f.backup == "backed_up":
            where.append("EXISTS (SELECT 1 FROM backup_records b WHERE b.image_id = i.id)")
        elif f.backup == "not_backed_up":
            where.append("NOT EXISTS (SELECT 1 FROM backup_records b WHERE b.image_id = i.id)")

        for word in f.text.split():
            like = "%" + _like_escape(word) + "%"
            where.append(
                "(i.filename LIKE ? ESCAPE '\\' OR i.model LIKE ? ESCAPE '\\' "
                "OR i.lens LIKE ? ESCAPE '\\' OR i.id IN (SELECT it.image_id FROM image_tags it "
                "JOIN tags t ON t.id = it.tag_id WHERE t.name LIKE ? ESCAPE '\\'))"
            )
            args += [like] * 4

        sql = f"SELECT {_IMAGE_COLS} FROM {_IMAGE_FROM}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        order = _SORTS.get(f.sort, _SORTS["capture_time"])
        sql += " ORDER BY " + ", ".join(col + (" DESC" if f.descending else "") for col in order)
        return [self._record(r) for r in self.conn.execute(sql, args)]

    @staticmethod
    def _record(row: sqlite3.Row) -> ImageRecord:
        rec = ImageRecord(*row)
        rec.rotate = int(rec.rotate or 0)
        rec.backed_up = bool(rec.backed_up)
        return rec

    def set_rating(self, ids: Sequence[int], rating: int) -> None:
        rating = max(0, min(5, int(rating)))
        self._update_many("rating", rating, ids)

    def set_flag(self, ids: Sequence[int], flag: int) -> None:
        if flag not in (FLAG_REJECT, FLAG_NONE, FLAG_PICK):
            raise ValueError(f"bad flag {flag}")
        self._update_many("flag", flag, ids)

    def remove_images(self, ids: Sequence[int]) -> None:
        """Forget images (with their tags, collection entries, edits, backup records). Files are not touched."""
        with self.conn:
            for chunk in _chunks(list(ids)):
                self.conn.execute(f"DELETE FROM images WHERE id IN ({_placeholders(len(chunk))})", chunk)

    # -- undo snapshots --------------------------------------------------

    def snapshot(self, ids: Sequence[int]) -> dict[int, dict]:
        """Everything a user command can change about these images, for undo/redo."""
        snap = {}
        for image_id in ids:
            row = self.conn.execute("SELECT rating, flag FROM images WHERE id = ?", (image_id,)).fetchone()
            if row is None:
                continue
            snap[image_id] = {
                "rating": row[0],
                "flag": row[1],
                "edit": self.get_edit(image_id),
                "tags": sorted(r[0] for r in self.conn.execute(
                    "SELECT t.name FROM image_tags it JOIN tags t ON t.id = it.tag_id WHERE it.image_id = ?",
                    (image_id,))),
                "collections": sorted(r[0] for r in self.conn.execute(
                    "SELECT collection_id FROM collection_images WHERE image_id = ?", (image_id,))),
            }
        return snap

    def restore(self, snap: dict[int, dict], label: str) -> None:
        """Put images back to a snapshot. Images or collections deleted since then are skipped;
        edit changes go through set_edit, so they are recorded in the edit history too."""
        for image_id, state in snap.items():
            now = self.snapshot([image_id]).get(image_id)
            if now is None:
                continue
            with self.conn:
                self.conn.execute(
                    "UPDATE images SET rating = ?, flag = ? WHERE id = ?", (state["rating"], state["flag"], image_id)
                )
                self.conn.executemany(
                    "INSERT OR IGNORE INTO collection_images(collection_id, image_id) "
                    "SELECT id, ? FROM collections WHERE id = ?",
                    [(image_id, c) for c in state["collections"]],
                )
                self.conn.executemany(
                    "DELETE FROM collection_images WHERE collection_id = ? AND image_id = ?",
                    [(c, image_id) for c in set(now["collections"]) - set(state["collections"])],
                )
            if state["edit"] != now["edit"]:
                self.set_edit(image_id, state["edit"], label)
            for name in {t.lower(): t for t in now["tags"]}.keys() - {t.lower() for t in state["tags"]}:
                self.remove_tag([image_id], name)
            self.add_tags([image_id], state["tags"])

    def _update_many(self, column: str, value, ids: Sequence[int]) -> None:
        with self.conn:
            for chunk in _chunks(list(ids)):
                self.conn.execute(
                    f"UPDATE images SET {column} = ? WHERE id IN ({_placeholders(len(chunk))})",
                    [value, *chunk],
                )

    # -- dates and import batches ----------------------------------------

    def day_counts(self) -> list[tuple[str, int]]:
        """("YYYY-MM-DD", count) for every day with photos, by capture time, oldest first."""
        rows = self.conn.execute(
            f"SELECT substr({_SHOT_AT}, 1, 10) AS day, COUNT(*) FROM images i GROUP BY day ORDER BY day"
        )
        return [(r[0], r[1]) for r in rows]

    def begin_import(self, root: str) -> int:
        with self.conn:
            return self.conn.execute("INSERT INTO imports(root) VALUES (?)", (root,)).lastrowid

    def imports(self, limit: int = 10) -> list[ImportBatch]:
        """Most recent import batches that still have photos, newest first."""
        rows = self.conn.execute(
            "SELECT m.id, m.root, m.started_at, COUNT(i.id) FROM imports m "
            "JOIN images i ON i.import_id = m.id GROUP BY m.id ORDER BY m.started_at DESC, m.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [ImportBatch(*r) for r in rows]

    # -- tags ------------------------------------------------------------

    def tags(self) -> list[NamedCount]:
        rows = self.conn.execute(
            "SELECT t.id, t.name, COUNT(it.image_id) FROM tags t "
            "LEFT JOIN image_tags it ON it.tag_id = t.id GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
        ).fetchall()
        return [NamedCount(*r) for r in rows]

    def tags_for(self, ids: Sequence[int]) -> dict[str, int]:
        """Tag name -> how many of the given images carry it."""
        counts: dict[str, int] = {}
        for chunk in _chunks(list(ids)):
            for name, n in self.conn.execute(
                "SELECT t.name, COUNT(*) FROM image_tags it JOIN tags t ON t.id = it.tag_id "
                f"WHERE it.image_id IN ({_placeholders(len(chunk))}) GROUP BY t.id",
                chunk,
            ):
                counts[name] = counts.get(name, 0) + n
        return dict(sorted(counts.items(), key=lambda kv: kv[0].lower()))

    def add_tags(self, ids: Sequence[int], names: Iterable[str]) -> None:
        names = [n.strip() for n in names if n.strip()]
        with self.conn:
            for name in names:
                self.conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
                tag_id = self.conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
                self.conn.executemany(
                    "INSERT OR IGNORE INTO image_tags(image_id, tag_id) VALUES (?, ?)",
                    [(i, tag_id) for i in ids],
                )

    def remove_tag(self, ids: Sequence[int], name: str) -> None:
        row = self.conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if not row:
            return
        with self.conn:
            self.conn.executemany(
                "DELETE FROM image_tags WHERE image_id = ? AND tag_id = ?", [(i, row[0]) for i in ids]
            )

    def delete_tag(self, tag_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))

    # -- collections -----------------------------------------------------

    def collections(self) -> list[NamedCount]:
        rows = self.conn.execute(
            "SELECT c.id, c.name, COUNT(ci.image_id) FROM collections c "
            "LEFT JOIN collection_images ci ON ci.collection_id = c.id "
            "GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
        ).fetchall()
        return [NamedCount(*r) for r in rows]

    def create_collection(self, name: str) -> int:
        with self.conn:
            return self.conn.execute("INSERT INTO collections(name) VALUES (?)", (name,)).lastrowid

    def rename_collection(self, collection_id: int, name: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE collections SET name = ? WHERE id = ?", (name, collection_id))

    def delete_collection(self, collection_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))

    def add_to_collection(self, collection_id: int, ids: Sequence[int]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO collection_images(collection_id, image_id) VALUES (?, ?)",
                [(collection_id, i) for i in ids],
            )

    def remove_from_collection(self, collection_id: int, ids: Sequence[int]) -> None:
        with self.conn:
            self.conn.executemany(
                "DELETE FROM collection_images WHERE collection_id = ? AND image_id = ?",
                [(collection_id, i) for i in ids],
            )

    # -- non-destructive edits ------------------------------------------

    def get_edit(self, image_id: int) -> dict:
        row = self.conn.execute("SELECT settings FROM edits WHERE image_id = ?", (image_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_edit(self, image_id: int, settings: dict, label: str) -> None:
        """Store new edit settings and append them to the image's history."""
        blob = json.dumps(settings, sort_keys=True)
        with self.conn:
            self.conn.execute(
                "INSERT INTO edits(image_id, settings) VALUES (?, ?) "
                "ON CONFLICT(image_id) DO UPDATE SET settings = excluded.settings, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')",
                (image_id, blob),
            )
            self.conn.execute(
                "INSERT INTO edit_history(image_id, settings, label) VALUES (?, ?, ?)",
                (image_id, blob, label),
            )

    def edit_history(self, image_id: int) -> list[tuple[str, str, dict]]:
        """(created_at, label, settings), oldest first."""
        return [
            (r[0], r[1], json.loads(r[2]))
            for r in self.conn.execute(
                "SELECT created_at, label, settings FROM edit_history WHERE image_id = ? ORDER BY id",
                (image_id,),
            )
        ]
