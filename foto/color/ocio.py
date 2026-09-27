"""OCIO display pipeline for the viewer.

Loads $OCIO if set, otherwise OCIO's built-in CG config. Produces GLSL plus
the LUT textures OCIO needs, so the viewer can apply the display transform
on the GPU. No OpenGL calls here, so this is testable headless.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import PyOpenColorIO as OCIO
from PySide6.QtCore import QObject, Signal

BUILTIN_CONFIG = "ocio://default"
# Previews are display-referred 8-bit JPEGs, so default to plain sRGB in and
# an un-tone-mapped view out: pixels look exactly as the camera rendered them.
PREFERRED_INPUTS = ("sRGB Encoded Rec.709 (sRGB)", "sRGB - Texture", "srgb_tx", "sRGB")
PREFERRED_VIEWS = ("Un-tone-mapped", "Standard", "Raw")


@dataclass
class LutTexture:
    sampler: str
    dims: int  # 1, 2 or 3
    width: int
    height: int
    depth: int
    channels: int  # 1 or 3
    linear: bool
    values: np.ndarray  # float32, flat


@dataclass
class ShaderBundle:
    """GLSL defining `vec4 OCIOMain(vec4)`, plus what it samples."""

    source: str
    textures: list[LutTexture] = field(default_factory=list)
    uniforms: list[tuple[str, str, object]] = field(default_factory=list)  # name, type, value


PASSTHROUGH = ShaderBundle("vec4 OCIOMain(vec4 inPixel) { return inPixel; }\n")


def _pick(options: list[str], preferred: tuple[str, ...]) -> str:
    for name in preferred:
        if name in options:
            return name
    return options[0] if options else ""


class ColorManager(QObject):
    changed = Signal()

    def __init__(self, config_path: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.config_path = config_path or os.environ.get("OCIO") or BUILTIN_CONFIG
        self.error: str | None = None
        try:
            self.config = OCIO.Config.CreateFromFile(self.config_path)
        except Exception as exc:
            self.error = f"{self.config_path}: {exc}; using built-in config"
            self.config_path = BUILTIN_CONFIG
            self.config = OCIO.Config.CreateFromFile(BUILTIN_CONFIG)
        self.input_space = _pick(self.input_spaces(), PREFERRED_INPUTS)
        self.display = self.config.getDefaultDisplay()
        self.view = _pick(self.views(self.display), PREFERRED_VIEWS)
        self.exposure = 0.0  # stops, applied in scene/input space before the view

    # -- choices ---------------------------------------------------------

    def input_spaces(self) -> list[str]:
        return [cs.getName() for cs in self.config.getColorSpaces()]

    def displays(self) -> list[str]:
        return list(self.config.getDisplays())

    def views(self, display: str | None = None) -> list[str]:
        return list(self.config.getViews(display or self.display))

    def set_input(self, name: str) -> None:
        self.input_space = name
        self.changed.emit()

    def set_display(self, display: str) -> None:
        self.display = display
        if self.view not in self.views(display):
            self.view = _pick(self.views(display), PREFERRED_VIEWS)
        self.changed.emit()

    def set_view(self, view: str) -> None:
        self.view = view
        self.changed.emit()

    def set_exposure(self, stops: float) -> None:
        self.exposure = stops
        self.changed.emit()

    # -- processing ------------------------------------------------------

    def processor(self):
        dvt = OCIO.DisplayViewTransform(src=self.input_space, display=self.display, view=self.view)
        group = OCIO.GroupTransform()
        if self.exposure:
            # Exposure in linear light: decode input to the scene reference, scale, then view.
            ref = OCIO.ROLE_SCENE_LINEAR
            gain = 2.0 ** self.exposure
            group.appendTransform(OCIO.ColorSpaceTransform(src=self.input_space, dst=ref))
            group.appendTransform(OCIO.MatrixTransform.Scale([gain, gain, gain, 1.0]))
            dvt.setSrc(ref)
        group.appendTransform(dvt)
        return self.config.getProcessor(group)

    def apply_cpu(self, rgb: np.ndarray) -> np.ndarray:
        """Apply the current transform to float32 RGB in [0, 1]. Used for tests/export."""
        out = np.ascontiguousarray(rgb, dtype=np.float32).copy()
        self.processor().getDefaultCPUProcessor().applyRGB(out)
        return out

    def shader(self) -> ShaderBundle:
        try:
            return build_shader(self.processor())
        except Exception as exc:
            self.error = str(exc)
            return PASSTHROUGH


def build_shader(processor) -> ShaderBundle:
    gpu = processor.getDefaultGPUProcessor()
    desc = OCIO.GpuShaderDesc.CreateShaderDesc(language=OCIO.GPU_LANGUAGE_GLSL_4_0)
    desc.setFunctionName("OCIOMain")
    desc.setResourcePrefix("ocio")
    gpu.extractGpuShaderInfo(desc)

    textures: list[LutTexture] = []
    for t in desc.getTextures():
        channels = 3 if t.channel == OCIO.GpuShaderDesc.TEXTURE_RGB_CHANNEL else 1
        dims = 1 if t.dimensions == OCIO.GpuShaderDesc.TEXTURE_1D else 2
        textures.append(
            LutTexture(
                sampler=t.samplerName,
                dims=dims,
                width=t.width,
                height=t.height,
                depth=1,
                channels=channels,
                linear=t.interpolation != OCIO.INTERP_NEAREST,
                values=np.asarray(t.getValues(), dtype=np.float32).ravel(),
            )
        )
    for t in desc.get3DTextures():
        textures.append(
            LutTexture(
                sampler=t.samplerName,
                dims=3,
                width=t.edgeLen,
                height=t.edgeLen,
                depth=t.edgeLen,
                channels=3,
                linear=t.interpolation != OCIO.INTERP_NEAREST,
                values=np.asarray(t.getValues(), dtype=np.float32).ravel(),
            )
        )

    uniforms = []
    for name, u in desc.getUniforms():
        if u.type == OCIO.UNIFORM_DOUBLE:
            uniforms.append((name, "float", u.getDouble()))
        elif u.type == OCIO.UNIFORM_BOOL:
            uniforms.append((name, "bool", u.getBool()))
        elif u.type == OCIO.UNIFORM_FLOAT3:
            uniforms.append((name, "vec3", tuple(u.getFloat3())))
    return ShaderBundle(desc.getShaderText(), textures, uniforms)
