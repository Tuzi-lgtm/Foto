"""Shared backup types and helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePath
from typing import Callable, Protocol

CHUNK = 4 * 1024 * 1024
Progress = Callable[[str], None]


class BackupError(Exception):
    pass


@dataclass
class PutResult:
    sha256: str | None = None
    error: str | None = None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def remote_rel(path: str) -> str:
    """Stable remote location for an original: its absolute path, drive letter kept.

    /Users/me/Pictures/a.arw  -> Users/me/Pictures/a.arw
    D:\\Photos\\a.nef          -> D/Photos/a.nef
    \\\\nas\\photo\\a.cr3        -> nas/photo/a.cr3
    """
    p = PurePath(path)
    drive = p.drive.replace("\\", "/").strip("/").rstrip(":")
    parts = [x for x in drive.split("/") if x] + list(p.parts[1:])
    return "/".join(parts)


class Backend(Protocol):
    def describe(self) -> str: ...

    def check(self) -> None:
        """Raise BackupError if the destination can't be reached or written."""

    def put_files(self, items: list[tuple[str, str]], progress: Progress | None = None) -> dict[str, PutResult]:
        """Copy (local path, remote-relative path) pairs under originals/. Keyed by local path."""

    def put_snapshot(self, local: str, name: str) -> None: ...

    def list_snapshots(self) -> list[str]: ...

    def delete_snapshot(self, name: str) -> None: ...
