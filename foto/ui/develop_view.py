"""Develop view: the photo rendered from its raw data (camera profile on the GPU).

While the raw decodes, the camera's embedded JPEG stands in. Zooming past fit
loads the full-resolution raw. Non-raw files are shown as they are for now.
Backslash toggles the camera's own JPEG, for comparing against Foto's render.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QVBoxLayout, QWidget

from foto.catalog import ImageRecord
from foto.color.ocio import ColorManager
from foto.develop import engine
from foto.imaging import decode as dec
from foto.imaging.service import ImageService
from foto.ui.compare import describe
from foto.ui.image_view import ImageView

_RANK = {None: 0, dec.THUMB: 1, dec.PREVIEW: 2, dec.FULL: 3}


class DevelopView(QWidget):
    backgroundDoubleClicked = Signal()

    def __init__(self, service: ImageService, loader: engine.RawLoader, color: ColorManager, settings_for, parent=None):
        super().__init__(parent)
        self.service, self.loader = service, loader
        self.settings_for = settings_for  # image_id -> develop settings dict
        self.view = ImageView(color, self)
        self.view.wantsFullRes.connect(self._want_full)
        self.view.backgroundDoubleClicked.connect(self.backgroundDoubleClicked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.view)
        self.record: ImageRecord | None = None
        self.show_camera = False
        self._raw_level: str | None = None  # engine.HALF / FULL once the raw is on screen
        self._jpeg_level: str | None = None
        service.ready.connect(self._on_jpeg)
        loader.ready.connect(self._on_raw)
        loader.failed.connect(self._on_raw_failed)

    # -- public ----------------------------------------------------------

    def show_record(self, rec: ImageRecord | None) -> None:
        same = rec is not None and self.record is not None and rec.id == self.record.id
        self.record = rec
        if rec is None:
            self._raw_level = self._jpeg_level = None
            self.view.set_image(None)
            self.view.overlay_text = ""
            self.view.update()
            return
        self.view.set_rotation(rec.rotate)
        if same:
            self._refresh_overlay()
            return
        self._raw_level = self._jpeg_level = None
        self.view.set_native_size(None)
        image, level = self.service.best_available(rec)
        if image is not None:
            self._set_jpeg(image, level)
        else:
            self.view.set_image(None, keep_view=False)
        if _RANK[self._jpeg_level] < _RANK[dec.PREVIEW]:
            self.service.get(rec, dec.PREVIEW)
        if engine.RawLoader.supports(rec) and not self.show_camera:
            raw = self.loader.get(rec, engine.FULL, request=False) or self.loader.get(rec, engine.HALF)
            if raw is not None:
                self._set_raw(raw, engine.FULL if self.loader.get(rec, engine.FULL, request=False) is raw else engine.HALF)
        self._refresh_overlay()

    def toggle_camera(self) -> None:
        """Flip between Foto's render and the camera's JPEG (keeps the zoom)."""
        self.show_camera = not self.show_camera
        rec = self.record
        if rec is None:
            return
        if self.show_camera:
            image, level = self.service.best_available(rec)
            self._raw_level = None
            if image is not None:
                self._set_jpeg(image, level, force=True)
        else:
            raw = self.loader.get(rec, engine.FULL, request=False)
            level = engine.FULL
            if raw is None:
                raw, level = self.loader.get(rec, engine.HALF), engine.HALF
            if raw is not None:
                self._set_raw(raw, level)
        self._refresh_overlay()

    def toggle_zoom(self) -> None:
        self.view.toggle_zoom()

    def refresh_settings(self) -> None:
        """Re-render with the record's current develop settings."""
        if self._raw_level and self.record is not None:
            raw = self.loader.get(self.record, self._raw_level, request=False)
            if raw is not None:
                self.view.set_develop(engine.render_params_for(self.record, raw, self.settings_for(self.record.id)))

    # -- loading ---------------------------------------------------------

    def _native(self):
        rec = self.record
        return (rec.width, rec.height) if rec and rec.width and rec.height else None

    def _set_jpeg(self, image: QImage, level: str, force: bool = False) -> None:
        if self._raw_level and not force:
            return  # the raw render is already up
        first = self._jpeg_level is None and not force
        self._jpeg_level = level
        native = (image.width(), image.height()) if level == dec.FULL else self._native()
        self.view.set_image(image, native_size=native, keep_view=not first)

    def _set_raw(self, raw, level: str) -> None:
        rec = self.record
        params = engine.render_params_for(rec, raw, self.settings_for(rec.id))
        keep = self._jpeg_level is not None or self._raw_level is not None
        self._raw_level = level
        self.view.set_raw(engine.to_uint16(raw), params, native_size=engine.native_size(raw, self._native()), keep_view=keep)

    def _on_jpeg(self, image_id: int, level: str, image: QImage) -> None:
        rec = self.record
        if rec is None or image_id != rec.id or _RANK[level] <= _RANK[self._jpeg_level]:
            return
        if self._raw_level is None:
            self._set_jpeg(image, level, force=self.show_camera and self._jpeg_level is not None)
        else:
            self._jpeg_level = level

    def _on_raw(self, image_id: int, level: str, raw) -> None:
        if self.record is None or image_id != self.record.id or self.show_camera:
            return
        if level == engine.FULL or self._raw_level != engine.FULL:
            self._set_raw(raw, level)
            self._refresh_overlay()

    def _on_raw_failed(self, image_id: int, level: str, message: str) -> None:
        if self.record is not None and image_id == self.record.id:
            self.view.error_text = f"Raw decode failed: {message}"[:200]
            self.view.update()

    def _want_full(self) -> None:
        rec = self.record
        if rec is None:
            return
        if engine.RawLoader.supports(rec) and not self.show_camera:
            if self._raw_level != engine.FULL:
                self.loader.get(rec, engine.FULL)
        elif self._jpeg_level != dec.FULL:
            self.service.get(rec, dec.FULL)

    def _refresh_overlay(self) -> None:
        rec = self.record
        if rec is None:
            return
        if not engine.RawLoader.supports(rec):
            source = "Not a raw file: shown as is"
        elif self.show_camera:
            source = "Camera JPEG  (\\ for Foto's render)"
        elif self._raw_level is None:
            source = "Decoding raw…"
        else:
            source = engine.profile_label(rec, self.settings_for(rec.id)) + ("" if self._raw_level == engine.FULL else "  ·  half size")
        self.view.overlay_text = f"{describe(rec)}\n{source}"
        self.view.update()
