"""Where the catalog and caches live."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_catalog_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif os.uname().sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "Foto" / "Default.fotocat"


@dataclass(frozen=True)
class CatalogPaths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "catalog.db"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    def ensure(self) -> "CatalogPaths":
        self.cache.mkdir(parents=True, exist_ok=True)
        return self
