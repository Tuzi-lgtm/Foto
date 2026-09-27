"""GPU image viewer: textured quad, pan/zoom, OCIO display transform.

View state is resolution independent: `scale` (1.0 = fit to window) and
`center` (the image point, in 0..1 display coords, at the window centre).
That lets a thumbnail be swapped for a preview or full-res image without the
view jumping, and lets compare mode sync two views directly.
"""

from __future__ import annotations

import numpy as np
from OpenGL import GL
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from foto.color.ocio import ColorManager, ShaderBundle

GL_VERSION = (4, 1)
MAX_SCALE = 64.0

_VERT = """
#version 410 core
uniform vec4 u_rect;   // x0, y0, x1, y1 in NDC
uniform int u_rot;     // clockwise quarter turns
out vec2 v_uv;
void main() {
    vec2 c = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1));  // 0..1, y down
    vec2 uv = c;
    if (u_rot == 1) uv = vec2(c.y, 1.0 - c.x);
    else if (u_rot == 2) uv = vec2(1.0 - c.x, 1.0 - c.y);
    else if (u_rot == 3) uv = vec2(1.0 - c.y, c.x);
    v_uv = uv;
    gl_Position = vec4(mix(u_rect.x, u_rect.z, c.x), mix(u_rect.y, u_rect.w, c.y), 0.0, 1.0);
}
"""

_FRAG_HEAD = "#version 410 core\n"
_FRAG_MAIN = """
uniform sampler2D u_image;
in vec2 v_uv;
out vec4 fragColor;
void main() {
    vec4 c = texture(u_image, v_uv);
    fragColor = vec4(OCIOMain(vec4(c.rgb, 1.0)).rgb, 1.0);
}
"""


def set_default_gl_format() -> None:
    """Must run before QApplication is created (macOS needs a core profile)."""
    fmt = QSurfaceFormat()
    fmt.setVersion(*GL_VERSION)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(0)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)


def _compile(kind, source: str) -> int:
    shader = GL.glCreateShader(kind)
    GL.glShaderSource(shader, source)
    GL.glCompileShader(shader)
    if not GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS):
        log = GL.glGetShaderInfoLog(shader).decode(errors="replace")
        GL.glDeleteShader(shader)
        raise RuntimeError(log)
    return shader


def _link(vert: str, frag: str) -> int:
    vs, fs = _compile(GL.GL_VERTEX_SHADER, vert), _compile(GL.GL_FRAGMENT_SHADER, frag)
    prog = GL.glCreateProgram()
    GL.glAttachShader(prog, vs)
    GL.glAttachShader(prog, fs)
    GL.glLinkProgram(prog)
    GL.glDeleteShader(vs)
    GL.glDeleteShader(fs)
    if not GL.glGetProgramiv(prog, GL.GL_LINK_STATUS):
        log = GL.glGetProgramInfoLog(prog).decode(errors="replace")
        GL.glDeleteProgram(prog)
        raise RuntimeError(log)
    return prog


class ImageView(QOpenGLWidget):
    viewChanged = Signal(float, float, float)  # scale, cx, cy
    wantsFullRes = Signal()
    clicked = Signal()
    backgroundDoubleClicked = Signal()  # double-click beside the image, not on it

    def __init__(self, color: ColorManager, parent=None):
        super().__init__(parent)
        self.color = color
        self.color.changed.connect(self._color_changed)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMouseTracking(False)
        self._image: QImage | None = None
        self._image_dirty = False
        self._rotate = 0
        self._native_size: tuple[int, int] | None = None  # full-res pixels, unrotated
        self.scale = 1.0
        self.center = QPointF(0.5, 0.5)
        self._ratio: float | None = None  # zoom as a pixel ratio (1.0 = 1:1); re-applied when a sharper image arrives
        self._drag_pos = None
        self._prog = 0
        self._vao = 0
        self._tex = 0
        self._lut_ids: list[tuple[int, int, str]] = []  # (texture id, target, sampler)
        self._shader_dirty = True
        self.overlay_text = ""
        self.error_text = ""
        self.background = QColor(38, 38, 38)

    # -- public API ------------------------------------------------------

    def set_image(self, image: QImage | None, native_size: tuple[int, int] | None = None, keep_view=True):
        self._image = image
        self._image_dirty = True
        if native_size:
            self._native_size = native_size
        if not keep_view:
            self.reset_view(emit=False)
        elif self._ratio:
            self.scale = max(1.0, self._ratio * self.one_to_one_scale())
            self._clamp()
        self.update()

    def set_native_size(self, size: tuple[int, int] | None) -> None:
        self._native_size = size

    def set_rotation(self, degrees: int) -> None:
        self._rotate = (degrees // 90) % 4
        self._clamp()
        self.update()

    def texture_size(self) -> tuple[int, int] | None:
        return (self._image.width(), self._image.height()) if self._image is not None else None

    def reset_view(self, emit=True) -> None:
        self._ratio = None
        self.scale = 1.0
        self.center = QPointF(0.5, 0.5)
        self.update()
        if emit:
            self._emit_view()

    def set_view(self, scale: float, cx: float, cy: float) -> None:
        """Apply a view state from elsewhere (compare sync) without re-emitting."""
        self._ratio = None
        self.scale = scale
        self.center = QPointF(cx, cy)
        self._clamp()
        self._check_resolution()
        self.update()

    def one_to_one_scale(self) -> float:
        """Scale at which one full-res pixel covers one device pixel."""
        size = self._native_size or self.texture_size()
        fit = self._fit_size()
        if not size or not fit:
            return 1.0
        w, _ = self._rotated(size)
        return w / fit[0]

    def toggle_zoom(self) -> None:
        if abs(self.scale - 1.0) < 1e-3:
            self.zoom_to(1.0)
        else:
            self.zoom_to(None)

    def zoom_to(self, ratio: float | None) -> None:
        """None = fit; otherwise screen pixels per image pixel (1.0 = 100%). Never smaller than fit."""
        if ratio is None:
            self.scale = 1.0
            self.center = QPointF(0.5, 0.5)
        else:
            self.scale = max(1.0, ratio * self.one_to_one_scale())
        self._ratio = ratio
        self._after_view_change()

    def zoom_percent(self) -> float | None:
        """Current zoom as a percentage of full resolution; None when fitted."""
        if abs(self.scale - 1.0) < 1e-3:
            return None
        return 100.0 * self.scale / self.one_to_one_scale()

    # -- geometry --------------------------------------------------------

    def _rotated(self, size: tuple[int, int]) -> tuple[int, int]:
        return (size[1], size[0]) if self._rotate % 2 else size

    def _viewport(self) -> tuple[float, float]:
        dpr = self.devicePixelRatioF()
        return self.width() * dpr, self.height() * dpr

    def _fit_size(self) -> tuple[float, float] | None:
        """Displayed image size in device pixels at scale 1."""
        size = self.texture_size()
        if not size:
            return None
        iw, ih = self._rotated(size)
        vw, vh = self._viewport()
        if vw <= 0 or vh <= 0:
            return None
        s = min(vw / iw, vh / ih)
        return iw * s, ih * s

    def _rect(self) -> tuple[float, float, float, float] | None:
        """Image rect in device pixels (x0, y0, w, h), top-left origin."""
        fit = self._fit_size()
        if not fit:
            return None
        w, h = fit[0] * self.scale, fit[1] * self.scale
        vw, vh = self._viewport()
        return vw / 2 - self.center.x() * w, vh / 2 - self.center.y() * h, w, h

    def _clamp(self) -> None:
        self.scale = max(1.0, min(MAX_SCALE, self.scale))
        fit = self._fit_size()
        if not fit:
            return
        vw, vh = self._viewport()
        w, h = fit[0] * self.scale, fit[1] * self.scale
        cx, cy = self.center.x(), self.center.y()
        cx = 0.5 if w <= vw else min(max(cx, vw / 2 / w), 1 - vw / 2 / w)
        cy = 0.5 if h <= vh else min(max(cy, vh / 2 / h), 1 - vh / 2 / h)
        self.center = QPointF(cx, cy)

    def _after_view_change(self) -> None:
        self._clamp()
        self._check_resolution()
        self.update()
        self._emit_view()

    def _emit_view(self) -> None:
        self.viewChanged.emit(self.scale, self.center.x(), self.center.y())

    def _check_resolution(self) -> None:
        """Any zoom past fit asks for the full-resolution image."""
        if self.scale > 1.01 and self._image is not None:
            self.wantsFullRes.emit()

    # -- input -----------------------------------------------------------

    def wheelEvent(self, event):
        rect = self._rect()
        if not rect:
            return
        dpr = self.devicePixelRatioF()
        px, py = event.position().x() * dpr, event.position().y() * dpr
        x0, y0, w, h = rect
        u, v = (px - x0) / w, (py - y0) / h
        factor = 1.0015 ** event.angleDelta().y()
        self.scale = max(1.0, min(MAX_SCALE, self.scale * factor))
        self._ratio = None
        fit = self._fit_size()
        w2, h2 = fit[0] * self.scale, fit[1] * self.scale
        vw, vh = self._viewport()
        # Keep the image point under the cursor fixed.
        self.center = QPointF((vw / 2 - (px - u * w2)) / w2, (vh / 2 - (py - v * h2)) / h2)
        self._after_view_change()

    def mousePressEvent(self, event):
        self.clicked.emit()
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.position()

    def mouseMoveEvent(self, event):
        rect = self._rect()
        if self._drag_pos is None or not rect:
            return
        dpr = self.devicePixelRatioF()
        d = (event.position() - self._drag_pos) * dpr
        self._drag_pos = event.position()
        self.center = QPointF(self.center.x() - d.x() / rect[2], self.center.y() - d.y() / rect[3])
        self._after_view_change()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, event):
        if self._on_image(event.position()):
            self.toggle_zoom()
        else:
            self.backgroundDoubleClicked.emit()

    def _on_image(self, pos: QPointF) -> bool:
        rect = self._rect()
        if not rect:
            return False
        dpr = self.devicePixelRatioF()
        x0, y0, w, h = rect
        return x0 <= pos.x() * dpr <= x0 + w and y0 <= pos.y() * dpr <= y0 + h

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp()

    # -- GL --------------------------------------------------------------

    def _color_changed(self) -> None:
        self._shader_dirty = True
        self.update()

    def initializeGL(self):
        self._vao = GL.glGenVertexArrays(1)
        self._tex = GL.glGenTextures(1)
        self.context().aboutToBeDestroyed.connect(self._cleanup)

    def _cleanup(self):
        self.makeCurrent()
        self._free_luts()
        if self._prog:
            GL.glDeleteProgram(self._prog)
        if self._tex:
            GL.glDeleteTextures([self._tex])
        if self._vao:
            GL.glDeleteVertexArrays(1, [self._vao])
        self._prog = self._tex = self._vao = 0
        self.doneCurrent()

    def _free_luts(self):
        if self._lut_ids:
            GL.glDeleteTextures([t for t, _, _ in self._lut_ids])
        self._lut_ids = []

    def _build_program(self):
        bundle = self.color.shader()
        try:
            prog = _link(_VERT, _FRAG_HEAD + bundle.source + _FRAG_MAIN)
            self.error_text = ""
        except RuntimeError as exc:
            self.error_text = f"OCIO shader failed, showing raw pixels: {exc}"[:300]
            bundle = ShaderBundle("vec4 OCIOMain(vec4 inPixel) { return inPixel; }\n")
            prog = _link(_VERT, _FRAG_HEAD + bundle.source + _FRAG_MAIN)
        if self._prog:
            GL.glDeleteProgram(self._prog)
        self._prog = prog
        self._free_luts()
        GL.glUseProgram(prog)
        for i, lut in enumerate(bundle.textures, start=1):
            self._lut_ids.append((self._upload_lut(i, lut), self._lut_target(lut), lut.sampler))
            GL.glUniform1i(GL.glGetUniformLocation(prog, lut.sampler), i)
        for name, kind, value in bundle.uniforms:
            loc = GL.glGetUniformLocation(prog, name)
            if kind == "float":
                GL.glUniform1f(loc, float(value))
            elif kind == "bool":
                GL.glUniform1i(loc, int(bool(value)))
            elif kind == "vec3":
                GL.glUniform3f(loc, *map(float, value))
        GL.glUniform1i(GL.glGetUniformLocation(prog, "u_image"), 0)
        self._shader_dirty = False

    @staticmethod
    def _lut_target(lut):
        if lut.dims == 3:
            return GL.GL_TEXTURE_3D
        return GL.GL_TEXTURE_1D if lut.dims == 1 and lut.height <= 1 else GL.GL_TEXTURE_2D

    def _upload_lut(self, unit: int, lut) -> int:
        tex = GL.glGenTextures(1)
        target = self._lut_target(lut)
        GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
        GL.glBindTexture(target, tex)
        filt = GL.GL_LINEAR if lut.linear else GL.GL_NEAREST
        GL.glTexParameteri(target, GL.GL_TEXTURE_MIN_FILTER, filt)
        GL.glTexParameteri(target, GL.GL_TEXTURE_MAG_FILTER, filt)
        for wrap in (GL.GL_TEXTURE_WRAP_S, GL.GL_TEXTURE_WRAP_T, GL.GL_TEXTURE_WRAP_R):
            GL.glTexParameteri(target, wrap, GL.GL_CLAMP_TO_EDGE)
        internal, fmt = (GL.GL_R32F, GL.GL_RED) if lut.channels == 1 else (GL.GL_RGB32F, GL.GL_RGB)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        data = np.ascontiguousarray(lut.values, dtype=np.float32)
        if target == GL.GL_TEXTURE_3D:
            GL.glTexImage3D(target, 0, internal, lut.width, lut.height, lut.depth, 0, fmt, GL.GL_FLOAT, data)
        elif target == GL.GL_TEXTURE_1D:
            GL.glTexImage1D(target, 0, internal, lut.width, 0, fmt, GL.GL_FLOAT, data)
        else:
            GL.glTexImage2D(target, 0, internal, lut.width, lut.height, 0, fmt, GL.GL_FLOAT, data)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        return tex

    def _upload_image(self):
        self._image_dirty = False
        if self._image is None:
            return
        img = self._image.convertToFormat(QImage.Format_RGBA8888)
        data = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._tex)
        GL.glPixelStorei(GL.GL_UNPACK_ROW_LENGTH, img.bytesPerLine() // 4)
        GL.glTexImage2D(
            GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, img.width(), img.height(), 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data
        )
        GL.glPixelStorei(GL.GL_UNPACK_ROW_LENGTH, 0)
        GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)

    def paintGL(self):
        c = self.background
        GL.glClearColor(c.redF(), c.greenF(), c.blueF(), 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        if self._shader_dirty:
            self._build_program()
        if self._image_dirty:
            self._upload_image()
            self._clamp()

        rect = self._rect()
        if self._image is not None and rect:
            vw, vh = self._viewport()
            x0, y0, w, h = rect
            ndc = (x0 / vw * 2 - 1, 1 - y0 / vh * 2, (x0 + w) / vw * 2 - 1, 1 - (y0 + h) / vh * 2)
            GL.glUseProgram(self._prog)
            GL.glUniform4f(GL.glGetUniformLocation(self._prog, "u_rect"), *ndc)
            GL.glUniform1i(GL.glGetUniformLocation(self._prog, "u_rot"), self._rotate)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self._tex)
            for i, (tex, target, _) in enumerate(self._lut_ids, start=1):
                GL.glActiveTexture(GL.GL_TEXTURE0 + i)
                GL.glBindTexture(target, tex)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindVertexArray(self._vao)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            GL.glBindVertexArray(0)
            GL.glUseProgram(0)

        if self.overlay_text or self.error_text:
            self._paint_overlay()

    def _paint_overlay(self):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 1.05)
        p.setFont(f)
        r = self.rect().adjusted(10, 8, -10, -8)
        if self.overlay_text:
            p.setPen(QColor(0, 0, 0, 160))
            p.drawText(r.translated(1, 1), Qt.AlignLeft | Qt.AlignTop, self.overlay_text)
            p.setPen(QColor(230, 230, 230))
            p.drawText(r, Qt.AlignLeft | Qt.AlignTop, self.overlay_text)
        if self.error_text:
            p.setPen(QColor(255, 120, 100))
            p.drawText(r, Qt.AlignLeft | Qt.AlignBottom | Qt.TextWordWrap, self.error_text)
        p.end()
