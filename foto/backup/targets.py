"""Backup destinations stored in the catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from foto.backup.base import Backend
from foto.backup.folder import FolderBackend
from foto.backup.rclone import RcloneBackend
from foto.catalog import Catalog

DEFAULT_KEEP_SNAPSHOTS = 10


@dataclass
class BackupTarget:
    id: int | None
    name: str
    kind: str  # "folder" | "rclone"
    config: dict = field(default_factory=dict)

    @property
    def location(self) -> str:
        return self.config.get("path") if self.kind == "folder" else self.config.get("remote", "")

    @property
    def keep_snapshots(self) -> int:
        return int(self.config.get("keep_snapshots", DEFAULT_KEEP_SNAPSHOTS))

    def backend(self) -> Backend:
        if self.kind == "folder":
            return FolderBackend(self.config["path"])
        if self.kind == "rclone":
            return RcloneBackend(self.config["remote"], self.config.get("rclone"))
        raise ValueError(f"unknown backup kind {self.kind}")


def list_targets(catalog: Catalog) -> list[BackupTarget]:
    rows = catalog.conn.execute("SELECT id, name, kind, config FROM backup_targets ORDER BY name").fetchall()
    return [BackupTarget(r[0], r[1], r[2], json.loads(r[3])) for r in rows]


def get_target(catalog: Catalog, target_id: int) -> BackupTarget | None:
    return next((t for t in list_targets(catalog) if t.id == target_id), None)


def save_target(catalog: Catalog, target: BackupTarget) -> int:
    blob = json.dumps(target.config)
    with catalog.conn:
        if target.id is None:
            target.id = catalog.conn.execute(
                "INSERT INTO backup_targets(name, kind, config) VALUES (?, ?, ?)", (target.name, target.kind, blob)
            ).lastrowid
        else:
            catalog.conn.execute(
                "UPDATE backup_targets SET name = ?, kind = ?, config = ? WHERE id = ?",
                (target.name, target.kind, blob, target.id),
            )
    return target.id


def delete_target(catalog: Catalog, target_id: int) -> None:
    """Forget a target and its records. Nothing on the destination is deleted."""
    with catalog.conn:
        catalog.conn.execute("DELETE FROM backup_targets WHERE id = ?", (target_id,))


def target_stats(catalog: Catalog, target_id: int) -> tuple[int, int, str | None]:
    """(images backed up, total images, last backup time)."""
    n, last = catalog.conn.execute(
        "SELECT COUNT(*), MAX(backed_up_at) FROM backup_records WHERE target_id = ?", (target_id,)
    ).fetchone()
    return n, catalog.count(), last
