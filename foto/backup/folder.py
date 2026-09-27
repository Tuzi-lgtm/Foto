"""Backup to any mounted folder: Synology/NAS SMB share, external drive, etc."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from foto.backup.base import CHUNK, BackupError, Progress, PutResult, sha256_file


class FolderBackend:
    def __init__(self, root: str):
        self.root = Path(root)

    def describe(self) -> str:
        return str(self.root)

    def check(self) -> None:
        if not self.root.is_dir():
            raise BackupError(f"{self.root} is not reachable (is the share mounted?)")
        probe = self.root / f".foto-write-test-{os.getpid()}"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise BackupError(f"{self.root} is not writable: {exc}") from exc

    def put_files(self, items, progress: Progress | None = None) -> dict[str, PutResult]:
        results = {}
        for local, rel in items:
            if progress:
                progress(local)
            try:
                results[local] = PutResult(sha256=self._copy_verified(local, self.root / "originals" / rel))
            except OSError as exc:
                results[local] = PutResult(error=str(exc))
            except BackupError as exc:
                results[local] = PutResult(error=str(exc))
        return results

    @staticmethod
    def _copy_verified(src: str, dst: Path) -> str:
        """Copy through a temp file, hash what was read, then re-read the copy to verify."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".foto-partial")
        h = hashlib.sha256()
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while chunk := fin.read(CHUNK):
                h.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        digest = h.hexdigest()
        if sha256_file(str(tmp)) != digest:
            tmp.unlink(missing_ok=True)
            raise BackupError(f"verification failed for {dst}")
        shutil.copystat(src, tmp)
        os.replace(tmp, dst)
        return digest

    def _snapshots_dir(self) -> Path:
        return self.root / "catalog"

    def put_snapshot(self, local: str, name: str) -> None:
        d = self._snapshots_dir()
        d.mkdir(parents=True, exist_ok=True)
        self._copy_verified(local, d / name)

    def list_snapshots(self) -> list[str]:
        d = self._snapshots_dir()
        return sorted(p.name for p in d.glob("catalog-*.db")) if d.is_dir() else []

    def delete_snapshot(self, name: str) -> None:
        (self._snapshots_dir() / name).unlink(missing_ok=True)
