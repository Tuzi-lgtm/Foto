"""SQLite connection setup and schema migrations.

Each entry in MIGRATIONS moves the schema one version forward; the current
version is kept in PRAGMA user_version.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: library
    """
    CREATE TABLE folders (
        id   INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE
    );
    CREATE TABLE images (
        id           INTEGER PRIMARY KEY,
        folder_id    INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
        path         TEXT NOT NULL UNIQUE,
        filename     TEXT NOT NULL,
        ext          TEXT NOT NULL,
        file_size    INTEGER NOT NULL,
        mtime_ns     INTEGER NOT NULL,
        capture_time TEXT,
        make         TEXT,
        model        TEXT,
        lens         TEXT,
        iso          INTEGER,
        shutter      REAL,
        aperture     REAL,
        focal        REAL,
        width        INTEGER,
        height       INTEGER,
        rating       INTEGER NOT NULL DEFAULT 0,
        flag         INTEGER NOT NULL DEFAULT 0,
        imported_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
    );
    CREATE INDEX images_folder ON images(folder_id);
    CREATE INDEX images_capture ON images(capture_time);

    CREATE TABLE tags (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE
    );
    CREATE TABLE image_tags (
        image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (image_id, tag_id)
    );
    CREATE INDEX image_tags_tag ON image_tags(tag_id);

    CREATE TABLE collections (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL
    );
    CREATE TABLE collection_images (
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        image_id      INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        added_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
        PRIMARY KEY (collection_id, image_id)
    );
    CREATE INDEX collection_images_image ON collection_images(image_id);

    CREATE TABLE edits (
        image_id   INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
        settings   TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
    );
    CREATE TABLE edit_history (
        id         INTEGER PRIMARY KEY,
        image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        settings   TEXT NOT NULL,
        label      TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now'))
    );
    CREATE INDEX edit_history_image ON edit_history(image_id);
    """,
    # 2: backup
    """
    CREATE TABLE backup_targets (
        id     INTEGER PRIMARY KEY,
        name   TEXT NOT NULL,
        kind   TEXT NOT NULL CHECK (kind IN ('folder', 'rclone')),
        config TEXT NOT NULL
    );
    CREATE TABLE backup_records (
        image_id     INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
        target_id    INTEGER NOT NULL REFERENCES backup_targets(id) ON DELETE CASCADE,
        remote_path  TEXT NOT NULL,
        file_size    INTEGER NOT NULL,
        mtime_ns     INTEGER NOT NULL,
        sha256       TEXT NOT NULL,
        backed_up_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
        PRIMARY KEY (image_id, target_id)
    );
    """,
    # 3: import batches ("Recently Added"). Existing images are grouped into
    # batches wherever imported_at jumps by more than five minutes.
    """
    CREATE TABLE imports (
        id         INTEGER PRIMARY KEY,
        root       TEXT NOT NULL,
        started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
    );
    ALTER TABLE images ADD COLUMN import_id INTEGER REFERENCES imports(id) ON DELETE SET NULL;
    CREATE INDEX images_import ON images(import_id);

    CREATE TEMP TABLE _batch AS
    WITH gaps AS (
        SELECT id, imported_at,
               COALESCE((julianday(imported_at) - julianday(LAG(imported_at) OVER w)) * 86400 > 300, 1) AS brk
        FROM images WINDOW w AS (ORDER BY imported_at, id)
    )
    SELECT id, imported_at, SUM(brk) OVER (ORDER BY imported_at, id) AS batch FROM gaps;
    INSERT INTO imports(id, root, started_at) SELECT batch, '', MIN(imported_at) FROM _batch GROUP BY batch;
    UPDATE images SET import_id = (SELECT batch FROM _batch WHERE _batch.id = images.id);
    DROP TABLE _batch;
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Catalog schema v{version} is newer than this Foto build (v{SCHEMA_VERSION})"
        )
    for i in range(version, SCHEMA_VERSION):
        # One transaction per step so a failed migration leaves the old version intact.
        conn.executescript(
            f"BEGIN;\n{MIGRATIONS[i]}\nPRAGMA user_version = {i + 1};\nCOMMIT;"
        )
