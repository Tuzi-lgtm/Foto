"""Preferences: cache, general, performance and color settings."""

from __future__ import annotations

import os
import shutil

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMessageBox, QProgressBar, QPushButton, QSpinBox, QStackedWidget,
    QVBoxLayout, QWidget,
)

from foto.color.ocio import BUILTIN_CONFIG
from foto.config import CatalogPaths, default_catalog_dir
from foto.imaging.cache import DiskCache
from foto.imaging.service import auto_threads
from foto.prefs import CACHE_LIMITS_GB, GB, Prefs
from foto.ui.gpu import gpu_info


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #9a9a9a;")
    return label


def _heading(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setStyleSheet("font-weight: 600; font-size: 11px; letter-spacing: 1px;")
    return label


class _PathRow(QWidget):
    """Read-only path with Browse… and Reset; empty means the default shown as placeholder."""

    def __init__(self, value: str, default: str, browse, parent=None):
        super().__init__(parent)
        self.edit = QLineEdit(value)
        self.edit.setReadOnly(True)
        self.edit.setPlaceholderText(default)
        pick = QPushButton("Browse…")
        pick.clicked.connect(lambda: (path := browse()) and self.edit.setText(path))
        reset = QPushButton("Reset")
        reset.clicked.connect(self.edit.clear)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit, 1)
        lay.addWidget(pick)
        lay.addWidget(reset)

    def value(self) -> str:
        return self.edit.text()


class PreferencesDialog(QDialog):
    PAGES = ("Cache", "General", "Performance", "Color")

    def __init__(self, prefs: Prefs, paths: CatalogPaths, cache: DiskCache, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.resize(760, 480)
        self.prefs, self.paths, self.cache = prefs, paths, cache

        self.nav = QListWidget()
        self.nav.addItems(self.PAGES)
        self.nav.setFixedWidth(150)
        self.pages = QStackedWidget()
        for build in (self._cache_page, self._general_page, self._performance_page, self._color_page):
            self.pages.addWidget(build())
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Done")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        body = QHBoxLayout()
        body.addWidget(self.nav)
        body.addWidget(self.pages, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(body, 1)
        lay.addWidget(buttons)

    # -- pages -----------------------------------------------------------

    def _page(self) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 8, 8, 8)
        return w, lay

    def _cache_page(self) -> QWidget:
        w, lay = self._page()
        self.usage_bar = QProgressBar()
        self.usage_bar.setTextVisible(False)
        self.usage_bar.setMaximumHeight(6)
        self.usage_text = QLabel("Measuring…")
        self.disk_text = QLabel()
        self.disk_text.setAlignment(Qt.AlignRight)
        stats = QGridLayout()
        self.stat_labels = {}
        for row, (key, title) in enumerate((("thumb", "Thumbnails"), ("preview", "Previews"))):
            stats.addWidget(QLabel(title), row, 0)
            self.stat_labels[key] = QLabel("…")
            self.stat_labels[key].setAlignment(Qt.AlignRight)
            stats.addWidget(self.stat_labels[key], row, 1)
        stats.setColumnStretch(2, 1)
        top = QHBoxLayout()
        top.addWidget(self.usage_text)
        top.addStretch(1)
        top.addWidget(self.disk_text)
        lay.addLayout(top)
        lay.addWidget(self.usage_bar)
        lay.addLayout(stats)
        lay.addSpacing(16)

        form = QFormLayout()
        self.cache_limit = QComboBox()
        for gb in CACHE_LIMITS_GB:
            self.cache_limit.addItem(f"{gb} GB" if gb else "Unlimited", gb)
        idx = self.cache_limit.findData(self.prefs.cache_limit_gb)
        self.cache_limit.setCurrentIndex(idx if idx >= 0 else self.cache_limit.findData(20))
        self.cache_limit.currentIndexChanged.connect(self._show_usage)
        clear = QPushButton("Clear Cache")
        clear.clicked.connect(self._clear_cache)
        limit_row = QHBoxLayout()
        limit_row.addWidget(self.cache_limit)
        limit_row.addStretch(1)
        limit_row.addWidget(clear)
        form.addRow("Cache size limit", limit_row)
        self.cache_path = _PathRow(
            self.prefs.cache_location, str(self.paths.cache),
            lambda: QFileDialog.getExistingDirectory(self, "Cache location", self.cache_path.value() or str(self.paths.cache)),
        )
        form.addRow("Cache location", self.cache_path)
        lay.addLayout(form)
        lay.addWidget(_note(
            "Thumbnails and previews are rebuilt from your originals whenever they're missing, so clearing or "
            "limiting the cache never loses anything; it only trades disk space for speed. When the limit is "
            "reached, the least recently viewed previews are removed first. A new location starts empty; the "
            "old cache folder is left as it is (clear it first if you want the space back)."
        ))
        lay.addStretch(1)
        QTimer.singleShot(0, self._measure)
        return w

    def _general_page(self) -> QWidget:
        w, lay = self._page()
        form = QFormLayout()
        current = QLineEdit(str(self.paths.root))
        current.setReadOnly(True)
        form.addRow("Current catalog", current)
        self.default_catalog = _PathRow(
            self.prefs.default_catalog, str(default_catalog_dir()),
            lambda: QFileDialog.getExistingDirectory(self, "Default catalog folder", str(self.paths.root.parent)),
        )
        form.addRow("Default catalog", self.default_catalog)
        use_current = QPushButton("Use Current Catalog")
        use_current.clicked.connect(lambda: self.default_catalog.edit.setText(str(self.paths.root)))
        form.addRow("", use_current)
        lay.addLayout(form)
        lay.addWidget(_note(
            "The default catalog opens when Foto starts without --catalog. Changes apply the next time Foto starts."
        ))
        lay.addStretch(1)
        return w

    def _performance_page(self) -> QWidget:
        w, lay = self._page()
        lay.addWidget(_heading("GPU"))
        info = gpu_info()
        if isinstance(info, str):
            lay.addWidget(QLabel(info))
        else:
            gpu = QLabel(info.summary())
            gpu.setTextInteractionFlags(Qt.TextSelectableByMouse)
            gpu.setToolTip("\n".join((info.vendor, info.renderer, info.version)))
            lay.addWidget(gpu)
            if info.software:
                lay.addWidget(_note(
                    "⚠ This is software rendering, so the viewer will be slow. Install or update the graphics "
                    "driver, or avoid running Foto over Remote Desktop."
                ))
            else:
                lay.addWidget(_note(
                    "The viewer draws and color-manages photos on this GPU. On a laptop with two GPUs, choose "
                    "High performance for Python under Windows Settings › System › Display › Graphics."
                    if os.name == "nt" else "The viewer draws and color-manages photos on this GPU."
                ))
        lay.addSpacing(16)
        lay.addWidget(_heading("Decoding"))
        form = QFormLayout()
        self.threads = QSpinBox()
        self.threads.setRange(0, max(2, os.cpu_count() or 4))
        self.threads.setSpecialValueText(f"Automatic ({auto_threads()})")
        self.threads.setValue(self.prefs.decode_threads)
        form.addRow("Decoding threads", self.threads)
        self.thumbs = QSpinBox()
        self.thumbs.setRange(200, 10000)
        self.thumbs.setSingleStep(100)
        self.thumbs.setValue(self.prefs.thumbs_in_memory)
        form.addRow("Thumbnails kept in memory", self.thumbs)
        lay.addLayout(form)
        lay.addWidget(_note(
            "More threads build thumbnails faster but make the rest of the computer busier. Each thumbnail in "
            "memory takes about 0.3 MB; more of them means less reloading when scrolling back and forth."
        ))
        lay.addStretch(1)
        return w

    def _color_page(self) -> QWidget:
        w, lay = self._page()
        form = QFormLayout()
        default = os.environ.get("OCIO") or f"Built-in ({BUILTIN_CONFIG})"
        self.ocio = _PathRow(
            self.prefs.ocio_config, default,
            lambda: QFileDialog.getOpenFileName(self, "OCIO config", "", "OCIO config (*.ocio *.ocioz);;All files (*)")[0],
        )
        form.addRow("OCIO config", self.ocio)
        lay.addLayout(form)
        lay.addWidget(_note(
            "Display, view and preview input space are chosen under View › Color Management. They are kept when "
            "you switch configs if the new config has them."
        ))
        lay.addStretch(1)
        return w

    # -- cache actions ---------------------------------------------------

    def _measure(self) -> None:
        self._usage = self.cache.usage()
        self._show_usage()

    def _show_usage(self) -> None:
        usage = getattr(self, "_usage", None)
        if usage is None:
            return
        used = sum(size for _, size in usage.values())
        for key, (files, size) in usage.items():
            self.stat_labels[key].setText(f"{files:,} files · {human(size)}")
        limit = (self.cache_limit.currentData() or 0) * GB
        self.usage_text.setText(f"{human(used)} used" + (f" of {human(limit)} limit" if limit else ""))
        self.usage_bar.setRange(0, 1000)
        self.usage_bar.setValue(int(1000 * min(1.0, used / limit)) if limit else 0)
        probe = self.cache.root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        disk = shutil.disk_usage(probe)
        self.disk_text.setText(f"{human(disk.free)} free on {probe.anchor or probe}")

    def _clear_cache(self) -> None:
        msg = f"Delete all cached thumbnails and previews in\n{self.cache.root}?\n\nThey are rebuilt as you browse."
        if QMessageBox.question(self, "Clear cache", msg) == QMessageBox.Yes:
            self.cache.clear()
            self._measure()

    # -- result ----------------------------------------------------------

    def accept(self) -> None:
        p = self.prefs
        p.cache_limit_gb = self.cache_limit.currentData()
        p.cache_location = self.cache_path.value()
        p.default_catalog = self.default_catalog.value()
        p.decode_threads = self.threads.value()
        p.thumbs_in_memory = self.thumbs.value()
        p.ocio_config = self.ocio.value()
        super().accept()
