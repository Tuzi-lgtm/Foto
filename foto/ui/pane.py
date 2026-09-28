"""One photo on screen, rendered the same way everywhere (loupe, compare, secondary, develop).

Raws are developed on the GPU from their sensor data: the rendered thumbnail
stands in for the fraction of a second the half-size decode takes, and
zooming past fit loads the full-size decode. Other files load thumbnail ->
preview -> full resolution. With `show_camera`, a raw shows the camera's own
JPEG instead, for comparison.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFrame, QVBoxLayout

from foto.catalog import ImageRecord
from foto.catalog.catalog import FLAG_PICK, FLAG_REJECT
from foto.color.ocio import ColorManager
from foto.develop import engine
from foto.imaging import decode as dec
from foto.imaging.service import ImageService
from foto.ui.image_view import ImageView

_RANK = {None: 0, dec.THUMB: 1, dec.PREVIEW: 2, dec.FULL: 3}


def describe(rec: ImageRecord) -> str:
    stars = "★" * rec.rating if rec.rating else ""
    flag = {FLAG_PICK: "  ⚑ Pick", FLAG_REJECT: "  ✕ Rejected"}.get(rec.flag, "")
    return f"{rec.filename}   {stars}{flag}"


class RenderPane(QFrame):
    activated = Signal()
    backgroundDoubleClicked = Signal()

    def __init__(self, service: ImageService, loader: engine.RawLoader, color: ColorManager, settings_for,
                 show_source: bool = False, parent=None):
        super().__init__(parent)
        self.service, self.loader = service, loader
        self.settings_for = settings_for  # image_id -> develop settings
        self.show_source = show_source  # overlay says what is on screen (profile / camera JPEG)
        self.view = ImageView(color, self)
        self.view.wantsFullRes.connect(self._want_full)
        self.view.clicked.connect(self.activated)
        self.view.backgroundDoubleClicked.connect(self.backgroundDoubleClicked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.view)
        self.record: ImageRecord | None = None
        self.show_camera = False
        self._raw_level: str | None = None  # engine.HALF / FULL once the raw render is on screen
        self._jpeg_level: str | None = None  # best image (thumbnail / preview / full) shown otherwise
        self.set_active(False)
        service.ready.connect(self._on_image)
        loader.ready.connect(self._on_raw)
        loader.failed.connect(self._on_raw_failed)

    def set_active(self, active: bool) -> None:
        self.setStyleSheet(
            "RenderPane { border: 2px solid %s; background: #262626; }" % ("#8a8a8a" if active else "#262626")
        )

    # -- public ----------------------------------------------------------

    def _develops(self, rec: ImageRecord | None) -> bool:
        return engine.RawLoader.supports(rec) and not self.show_camera

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
        self._load()
        self._refresh_overlay()

    def _load(self) -> None:
        rec = self.record
        if self._develops(rec):
            thumb = self.service.get(rec, dec.THUMB)  # rendered look, stands in while decoding
            if thumb is not None:
                self._set_image(thumb, dec.THUMB)
            else:
                self.view.set_image(None, keep_view=False)
            full = self.loader.get(rec, engine.FULL, request=False)
            raw = full or self.loader.get(rec, engine.HALF)
            if raw is not None:
                self._set_raw(raw, engine.FULL if full is not None else engine.HALF)
            return
        levels = (dec.FULL,) if engine.RawLoader.supports(rec) else (dec.FULL, dec.PREVIEW, dec.THUMB)
        for level in levels:  # camera JPEG of a raw: full size only (its thumbnail is Foto's render)
            image = self.service.get(rec, level, request=False)
            if image is not None:
                self._set_image(image, level)
                break
        else:
            self.view.set_image(None, keep_view=False)
        if engine.RawLoader.supports(rec):
            self.service.get(rec, dec.FULL)
        elif _RANK[self._jpeg_level] < _RANK[dec.PREVIEW]:
            if self._jpeg_level is None:
                self.service.get(rec, dec.THUMB)
            self.service.get(rec, dec.PREVIEW)

    def toggle_camera(self) -> None:
        """Flip between Foto's render and the camera's JPEG (keeps the zoom)."""
        self.show_camera = not self.show_camera
        if self.record is not None and engine.RawLoader.supports(self.record):
            self._raw_level = self._jpeg_level = None
            self._load()
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

    def _set_image(self, image: QImage, level: str) -> None:
        keep = self._jpeg_level is not None or self._raw_level is not None
        self._jpeg_level = level
        native = (image.width(), image.height()) if level == dec.FULL else self._native()
        self.view.set_image(image, native_size=native, keep_view=keep)

    def _set_raw(self, raw, level: str) -> None:
        rec = self.record
        params = engine.render_params_for(rec, raw, self.settings_for(rec.id))
        keep = self._jpeg_level is not None or self._raw_level is not None
        self._raw_level = level
        self.view.set_raw(engine.to_uint16(raw), params, native_size=engine.native_size(raw, self._native()),
                          keep_view=keep)
        self._refresh_overlay()

    def _on_image(self, image_id: int, level: str, image: QImage) -> None:
        rec = self.record
        if rec is None or image_id != rec.id:
            return
        if self._develops(rec):
            if self._raw_level is None and level == dec.THUMB:
                self._set_image(image, level)  # newer stand-in (the finished render replaces the camera JPEG)
            return
        if engine.RawLoader.supports(rec) and level != dec.FULL:
            return  # camera JPEG mode: only the embedded full-size JPEG
        if _RANK[level] > _RANK[self._jpeg_level]:
            self._set_image(image, level)

    def _on_raw(self, image_id: int, level: str, raw) -> None:
        if self.record is None or image_id != self.record.id or not self._develops(self.record):
            return
        if level == engine.FULL or self._raw_level != engine.FULL:
            self._set_raw(raw, level)

    def _on_raw_failed(self, image_id: int, level: str, message: str) -> None:
        if self.record is not None and image_id == self.record.id:
            self.view.error_text = f"Raw decode failed: {message}"[:200]
            self.view.update()

    def _want_full(self) -> None:
        rec = self.record
        if rec is None:
            return
        if self._develops(rec):
            if self._raw_level != engine.FULL:
                self.loader.get(rec, engine.FULL)
        elif self._jpeg_level != dec.FULL:
            self.service.get(rec, dec.FULL)

    def _refresh_overlay(self) -> None:
        rec = self.record
        if rec is None:
            return
        text = describe(rec)
        if self.show_source:
            if not engine.RawLoader.supports(rec):
                source = "Not a raw file: shown as is"
            elif self.show_camera:
                source = "Camera JPEG  (\\ for Foto's render)"
            elif self._raw_level is None:
                source = "Decoding raw…"
            else:
                source = engine.profile_label(rec, self.settings_for(rec.id)) + (
                    "" if self._raw_level == engine.FULL else "  ·  half size")
            text += "\n" + source
        self.view.overlay_text = text
        self.view.update()
