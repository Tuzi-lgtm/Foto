"""Which GPU and driver the viewer's OpenGL context actually runs on."""

from __future__ import annotations

from dataclasses import dataclass

# Renderers that mean no hardware acceleration (driver missing or a remote session).
_SOFTWARE = ("llvmpipe", "softpipe", "swrast", "gdi generic", "microsoft basic render", "software")
# GL_NVX_gpu_memory_info: dedicated video memory in KB (NVIDIA only; other vendors don't report a total).
_NVX_DEDICATED_MEMORY = 0x9047


@dataclass
class GpuInfo:
    vendor: str
    renderer: str
    version: str  # GL version string; on NVIDIA/AMD it ends with the driver version
    memory_mb: int | None = None

    @property
    def software(self) -> bool:
        return any(s in self.renderer.lower() for s in _SOFTWARE)

    def summary(self) -> str:
        """e.g. "NVIDIA RTX A6000 · driver 597.06 · 48.0 GB · OpenGL 4.1"."""
        gl, _, rest = self.version.partition(" ")
        parts = [self.renderer.split("/")[0]]
        if rest.split():
            parts.append(f"driver {rest.split()[-1]}")
        if self.memory_mb:
            parts.append(f"{self.memory_mb / 1024:.1f} GB")
        parts.append(f"OpenGL {'.'.join(gl.split('.')[:2])}")
        return " · ".join(parts)


_cached: GpuInfo | str | None = None


def gpu_info() -> GpuInfo | str:
    """GpuInfo for a context in the app's default format, or an error message. Probed once."""
    global _cached
    if _cached is None:
        _cached = _probe()
    return _cached


def _probe() -> GpuInfo | str:
    from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat

    surface = QOffscreenSurface()
    surface.setFormat(QSurfaceFormat.defaultFormat())
    surface.create()
    ctx = QOpenGLContext()
    ctx.setFormat(QSurfaceFormat.defaultFormat())
    if not ctx.create() or not ctx.makeCurrent(surface):
        return "No OpenGL context could be created, so the viewer can't use the GPU."
    try:
        from OpenGL import GL

        def text(name) -> str:
            value = GL.glGetString(name)
            return value.decode(errors="replace") if isinstance(value, bytes) else str(value or "")

        info = GpuInfo(text(GL.GL_VENDOR), text(GL.GL_RENDERER), text(GL.GL_VERSION))
        info.memory_mb = _memory_mb()
        return info
    except Exception as exc:
        return f"Could not query the GPU: {exc}"
    finally:
        ctx.doneCurrent()
        surface.destroy()


def _memory_mb() -> int | None:
    import numpy as np
    from OpenGL import GL

    try:
        kb = int(np.atleast_1d(GL.glGetIntegerv(_NVX_DEDICATED_MEMORY))[0])
    except Exception:  # extension missing: PyOpenGL raises GL_INVALID_ENUM
        return None
    return kb // 1024 if kb > 0 else None
