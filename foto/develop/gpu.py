"""Run the develop shader offscreen and read the result back (tests; later, fast export).

Needs a real OpenGL 4.1 context: returns None when none can be created
(e.g. Qt's offscreen platform), so callers fall back to the CPU pipeline.
"""

from __future__ import annotations

import numpy as np

from foto.develop.pipeline import RenderParams
from foto.develop.shader import DEVELOP_GLSL, develop_textures, develop_uniforms

_VERT = """
#version 410 core
out vec2 v_uv;
void main() {
    vec2 c = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1));
    v_uv = c;
    gl_Position = vec4(c * 2.0 - 1.0, 0.0, 1.0);
}
"""
_FRAG = "#version 410 core\n" + DEVELOP_GLSL + """
uniform sampler2D u_image;
in vec2 v_uv;
out vec4 fragColor;
void main() { fragColor = vec4(developMain(texture(u_image, v_uv).rgb), 1.0); }
"""


def render_gpu(rgb: np.ndarray, params: RenderParams) -> np.ndarray | None:
    """Camera RGB (float 0..1, H x W x 3) -> linear sRGB, rendered by the GLSL pipeline."""
    from OpenGL import GL
    from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat

    from foto.ui.image_view import _link

    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    if not ctx.create() or not ctx.makeCurrent(surface):
        return None
    h, w, _ = rgb.shape
    try:
        prog = _link(_VERT, _FRAG)
        GL.glUseProgram(prog)
        textures = []
        for unit, lut in enumerate(develop_textures(params), start=1):
            tex = GL.glGenTextures(1)
            target = GL.GL_TEXTURE_3D if lut.dims == 3 else GL.GL_TEXTURE_1D
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(target, tex)
            for pname in (GL.GL_TEXTURE_MIN_FILTER, GL.GL_TEXTURE_MAG_FILTER):
                GL.glTexParameteri(target, pname, GL.GL_NEAREST)
            internal, form = (GL.GL_RGB32F, GL.GL_RGB) if lut.channels == 3 else (GL.GL_R32F, GL.GL_RED)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            if target == GL.GL_TEXTURE_3D:
                GL.glTexImage3D(target, 0, internal, lut.width, lut.height, lut.depth, 0, form, GL.GL_FLOAT, lut.values)
            else:
                GL.glTexImage1D(target, 0, internal, lut.width, 0, form, GL.GL_FLOAT, lut.values)
            GL.glUniform1i(GL.glGetUniformLocation(prog, lut.sampler), unit)
            textures.append(tex)
        for name, kind, value in develop_uniforms(params, encode_srgb=False):
            loc = GL.glGetUniformLocation(prog, name)
            if kind == "float":
                GL.glUniform1f(loc, float(value))
            elif kind in ("bool", "int"):
                GL.glUniform1i(loc, int(value))
            elif kind == "vec3":
                GL.glUniform3f(loc, *value)
            elif kind == "ivec3":
                GL.glUniform3i(loc, *(int(v) for v in value))
            elif kind == "mat3":
                GL.glUniformMatrix3fv(loc, 1, GL.GL_TRUE, np.ascontiguousarray(value, np.float32))

        src = GL.glGenTextures(1)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, src)
        for pname in (GL.GL_TEXTURE_MIN_FILTER, GL.GL_TEXTURE_MAG_FILTER):
            GL.glTexParameteri(GL.GL_TEXTURE_2D, pname, GL.GL_NEAREST)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB32F, w, h, 0, GL.GL_RGB, GL.GL_FLOAT,
                        np.ascontiguousarray(rgb, np.float32))
        GL.glUniform1i(GL.glGetUniformLocation(prog, "u_image"), 0)

        fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        target_tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, target_tex)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA32F, w, h, 0, GL.GL_RGBA, GL.GL_FLOAT, None)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, target_tex, 0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, src)
        GL.glViewport(0, 0, w, h)
        vao = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(vao)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        out = GL.glReadPixels(0, 0, w, h, GL.GL_RGBA, GL.GL_FLOAT)
        result = np.frombuffer(out, np.float32).reshape(h, w, 4)[..., :3].copy()

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        GL.glDeleteFramebuffers(1, [fbo])
        GL.glDeleteTextures([src, target_tex, *textures])
        GL.glDeleteVertexArrays(1, [vao])
        GL.glDeleteProgram(prog)
        return result
    finally:
        ctx.doneCurrent()
        surface.destroy()
