"""Backup through rclone: Google Cloud Storage, Google Drive, S3, B2, SFTP/WebDAV
(e.g. a Synology NAS over the internet) and anything else rclone supports.

Remotes are set up once with `rclone config`; Foto only stores the remote
name and path (e.g. "gcs:my-bucket/foto"), never credentials.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import PurePath

from foto.backup.base import BackupError, Progress, PutResult, remote_rel, sha256_file


def find_rclone(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if os.path.exists(explicit) else shutil.which(explicit)
    return shutil.which("rclone")


def list_remotes(binary: str | None = None) -> list[str]:
    exe = find_rclone(binary)
    if not exe:
        return []
    out = subprocess.run([exe, "listremotes"], capture_output=True, text=True, timeout=30)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


class RcloneBackend:
    def __init__(self, remote: str, binary: str | None = None, extra_args: list[str] | None = None):
        self.remote = remote.rstrip("/")
        self.binary = find_rclone(binary)
        self.extra_args = list(extra_args or [])

    def describe(self) -> str:
        return f"rclone {self.remote}"

    def _run(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
        if not self.binary:
            raise BackupError("rclone not found. Install it from rclone.org and run `rclone config`.")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(
            [self.binary, *args, *self.extra_args], capture_output=True, text=True, timeout=timeout, **kwargs
        )

    def _checked(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
        proc = self._run(*args, timeout=timeout)
        if proc.returncode != 0:
            raise BackupError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"rclone {args[0]} failed")
        return proc

    def check(self) -> None:
        self._checked("mkdir", self.remote, timeout=120)

    def put_files(self, items, progress: Progress | None = None) -> dict[str, PutResult]:
        results: dict[str, PutResult] = {}
        # rclone copies relative to a source root, so group files by drive/anchor.
        groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for local, rel in items:
            try:
                sha = sha256_file(local)
            except OSError as exc:
                results[local] = PutResult(error=str(exc))
                continue
            results[local] = PutResult(sha256=sha)
            groups[PurePath(local).anchor].append((local, rel))

        for anchor, group in groups.items():
            prefix = remote_rel(anchor)  # "" for "/", "D" for "D:\\"
            dst = f"{self.remote}/originals" + (f"/{prefix}" if prefix else "")
            by_rel = {PurePath(local).relative_to(anchor).as_posix(): local for local, _ in group}
            with tempfile.TemporaryDirectory() as tmp:
                listing = os.path.join(tmp, "files.txt")
                with open(listing, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(by_rel) + "\n")
                copy = self._run("copy", anchor, dst, "--files-from-raw", listing, "--no-traverse")
                status = self._verify(anchor, dst, listing)
            for rel_from_anchor, local in by_rel.items():
                if progress:
                    progress(local)
                if status.get(rel_from_anchor) != "=":
                    reason = copy.stderr.strip().splitlines()[-1] if copy.returncode and copy.stderr.strip() else ""
                    results[local] = PutResult(error=f"not verified on remote ({status.get(rel_from_anchor, '?')}) {reason}".strip())
        return results

    def _verify(self, src: str, dst: str, listing: str) -> dict[str, str]:
        """rclone check, one line per file: '=' same, '-' missing remotely, '*' differs, '!' error."""
        proc = self._run("check", src, dst, "--files-from-raw", listing, "--one-way", "--combined", "-")
        status = {}
        for line in proc.stdout.splitlines():
            if len(line) > 2 and line[1] == " ":
                status[line[2:]] = line[0]
        return status

    def put_snapshot(self, local: str, name: str) -> None:
        self._checked("copyto", local, f"{self.remote}/catalog/{name}")

    def list_snapshots(self) -> list[str]:
        proc = self._run("lsf", f"{self.remote}/catalog", "--files-only", "--include", "catalog-*.db")
        return sorted(line.strip() for line in proc.stdout.splitlines() if line.strip()) if proc.returncode == 0 else []

    def delete_snapshot(self, name: str) -> None:
        self._checked("deletefile", f"{self.remote}/catalog/{name}")
