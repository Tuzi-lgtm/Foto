"""App preferences (per user, not per catalog), stored with QSettings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from foto.config import CatalogPaths

GB = 1024**3
CACHE_LIMITS_GB = (1, 2, 5, 10, 20, 50, 100, 0)  # 0 = unlimited
DEFAULT_CACHE_LIMIT_GB = 20
DEFAULT_THUMBS_IN_MEMORY = 800


class Prefs:
    def __init__(self, settings: QSettings | None = None):
        self.s = settings or QSettings()

    def _str(self, key: str) -> str:
        return str(self.s.value(key, "") or "")

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.s.value(key, default))
        except (TypeError, ValueError):
            return default

    # -- cache -----------------------------------------------------------

    @property
    def cache_location(self) -> str:
        """Custom cache folder; "" = inside the catalog folder."""
        return self._str("cache/location")

    @cache_location.setter
    def cache_location(self, path: str) -> None:
        self.s.setValue("cache/location", path or "")

    def cache_dir(self, paths: CatalogPaths) -> Path:
        return Path(self.cache_location) if self.cache_location else paths.cache

    @property
    def cache_limit_gb(self) -> int:
        return self._int("cache/limit_gb", DEFAULT_CACHE_LIMIT_GB)

    @cache_limit_gb.setter
    def cache_limit_gb(self, gb: int) -> None:
        self.s.setValue("cache/limit_gb", int(gb))

    # -- general ---------------------------------------------------------

    @property
    def default_catalog(self) -> str:
        """Catalog opened when Foto starts without --catalog; "" = the built-in default."""
        return self._str("general/default_catalog")

    @default_catalog.setter
    def default_catalog(self, path: str) -> None:
        self.s.setValue("general/default_catalog", path or "")

    # -- performance -----------------------------------------------------

    @property
    def decode_threads(self) -> int:
        return self._int("performance/decode_threads", 0)  # 0 = automatic

    @decode_threads.setter
    def decode_threads(self, n: int) -> None:
        self.s.setValue("performance/decode_threads", int(n))

    @property
    def thumbs_in_memory(self) -> int:
        return self._int("performance/thumbs_in_memory", DEFAULT_THUMBS_IN_MEMORY)

    @thumbs_in_memory.setter
    def thumbs_in_memory(self, n: int) -> None:
        self.s.setValue("performance/thumbs_in_memory", int(n))

    # -- color -----------------------------------------------------------

    @property
    def ocio_config(self) -> str:
        """OCIO config file; "" = $OCIO if set, else the built-in config."""
        return self._str("ocio/config")

    @ocio_config.setter
    def ocio_config(self, path: str) -> None:
        self.s.setValue("ocio/config", path or "")
