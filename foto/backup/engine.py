"""Incremental backup of originals plus a catalog snapshot.

A file is (re)sent when it has no record for the target, or its size/mtime
on disk changed since the last backup. Records are written per batch, so an
interrupted run resumes where it stopped. Nothing is ever deleted remotely
except catalog snapshots beyond the keep count.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Sequence

from foto.backup.base import Backend, BackupError, remote_rel
from foto.backup.targets import BackupTarget
from foto.catalog import Catalog

BATCH = 50


@dataclass
class BackupResult:
    copied: int = 0
    unchanged: int = 0
    missing: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    snapshot: str | None = None
    cancelled: bool = False

    def summary(self) -> str:
        parts = [f"{self.copied} copied", f"{self.unchanged} already backed up"]
        if self.missing:
            parts.append(f"{self.missing} originals offline")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.snapshot:
            parts.append("catalog snapshot saved")
        if self.cancelled:
            parts.append("cancelled")
        return ", ".join(parts)


@dataclass
class _Todo:
    image_id: int
    path: str
    rel: str
    size: int
    mtime_ns: int


class BackupEngine:
    def __init__(self, catalog: Catalog, target: BackupTarget, backend: Backend | None = None):
        self.catalog = catalog
        self.target = target
        self.backend = backend or target.backend()

    def plan(self, image_ids: Sequence[int] | None = None) -> tuple[list[_Todo], int, int]:
        """(files to send, unchanged count, offline count)."""
        sql = (
            "SELECT i.id, i.path, b.file_size, b.mtime_ns FROM images i "
            "LEFT JOIN backup_records b ON b.image_id = i.id AND b.target_id = ?"
        )
        rows = self.catalog.conn.execute(sql, (self.target.id,)).fetchall()
        wanted = set(image_ids) if image_ids is not None else None
        todo, unchanged, missing = [], 0, 0
        for image_id, path, rec_size, rec_mtime in rows:
            if wanted is not None and image_id not in wanted:
                continue
            try:
                st = os.stat(path)
            except OSError:
                missing += 1
                continue
            if rec_size == st.st_size and rec_mtime == st.st_mtime_ns:
                unchanged += 1
                continue
            todo.append(_Todo(image_id, path, remote_rel(path), st.st_size, st.st_mtime_ns))
        return todo, unchanged, missing

    def run(
        self,
        image_ids: Sequence[int] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        snapshot: bool = True,
    ) -> BackupResult:
        self.backend.check()
        todo, unchanged, missing = self.plan(image_ids)
        result = BackupResult(unchanged=unchanged, missing=missing)
        total, done = len(todo), 0

        for start in range(0, total, BATCH):
            if cancelled and cancelled():
                result.cancelled = True
                break
            batch = todo[start : start + BATCH]

            def tick(path: str) -> None:
                nonlocal done
                done += 1
                if progress:
                    progress(done, total, path)

            outcome = self.backend.put_files([(t.path, t.rel) for t in batch], tick)
            records = []
            for t in batch:
                res = outcome.get(t.path)
                if res and res.sha256 and not res.error:
                    records.append((t.image_id, self.target.id, "originals/" + t.rel, t.size, t.mtime_ns, res.sha256))
                    result.copied += 1
                else:
                    result.failed += 1
                    result.errors.append(f"{t.path}: {res.error if res else 'no result'}")
            self._record(records)

        if snapshot and not result.cancelled:
            result.snapshot = self.snapshot_catalog()
        return result

    def _record(self, rows) -> None:
        with self.catalog.conn:
            self.catalog.conn.executemany(
                "INSERT INTO backup_records(image_id, target_id, remote_path, file_size, mtime_ns, sha256) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(image_id, target_id) DO UPDATE SET "
                "remote_path = excluded.remote_path, file_size = excluded.file_size, "
                "mtime_ns = excluded.mtime_ns, sha256 = excluded.sha256, "
                "backed_up_at = strftime('%Y-%m-%dT%H:%M:%S', 'now')",
                rows,
            )

    def snapshot_catalog(self) -> str:
        """Consistent copy of the live catalog via SQLite's online backup API."""
        name = f"catalog-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, name)
            dst = sqlite3.connect(local)
            try:
                self.catalog.conn.backup(dst)
            finally:
                dst.close()
            self.backend.put_snapshot(local, name)
        self._prune_snapshots()
        return name

    def _prune_snapshots(self) -> None:
        snaps = self.backend.list_snapshots()
        for old in snaps[: max(0, len(snaps) - self.target.keep_snapshots)]:
            try:
                self.backend.delete_snapshot(old)
            except (OSError, BackupError):
                pass
