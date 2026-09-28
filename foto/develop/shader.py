"""The render pipeline (develop/pipeline.py) as GLSL, for live display on the GPU.

`DEVELOP_GLSL` defines `vec3 developMain(vec3 cameraRGB)` returning
display-referred linear sRGB. Tables are sampled with texelFetch and
interpolated by hand, so hue wraps exactly like the CPU reference and there
is no 8-bit filtering error. `develop_textures` / `develop_uniforms` turn a
RenderParams into what the shader needs.
"""

from __future__ import annotations

import numpy as np

from foto.color.ocio import LutTexture
from foto.develop.dcp import HueSatTable
from foto.develop.pipeline import RenderParams

DEVELOP_GLSL = """
uniform vec3 dev_cameraWhite;
uniform mat3 dev_cameraToProPhoto;
uniform float dev_exposureScale;      // 1 / white of the exposure ramp
uniform mat3 dev_output;              // ProPhoto -> linear sRGB
uniform bool dev_encodeSRGB;          // no linear space in the OCIO config: hand it sRGB-encoded values
uniform sampler3D dev_hsm;            // (sat, hue, val) texels of (hueShift, satScale, valScale)
uniform ivec3 dev_hsmDivs;            // hue, sat, val divisions; 0 = off
uniform bool dev_hsmSRGB;
uniform sampler3D dev_look;
uniform ivec3 dev_lookDivs;
uniform bool dev_lookSRGB;
uniform sampler1D dev_curve;
uniform int dev_curveSize;

float dev_encode(float x) { return x <= 0.0031308 ? 12.92 * x : 1.055 * pow(x, 1.0 / 2.4) - 0.055; }
float dev_decode(float x) { return x <= 0.04045 ? x / 12.92 : pow((x + 0.055) / 1.055, 2.4); }
vec3 dev_encode3(vec3 c) { return vec3(dev_encode(c.r), dev_encode(c.g), dev_encode(c.b)); }
vec3 dev_decode3(vec3 c) { return vec3(dev_decode(c.r), dev_decode(c.g), dev_decode(c.b)); }

vec3 dev_rgbToHsv(vec3 c) {
    float v = max(c.r, max(c.g, c.b));
    float gap = v - min(c.r, min(c.g, c.b));
    float s = v > 0.0 ? gap / v : 0.0;
    float h = 0.0;
    if (gap > 0.0) {
        if (c.r == v) h = (c.g - c.b) / gap;
        else if (c.g == v) h = 2.0 + (c.b - c.r) / gap;
        else h = 4.0 + (c.r - c.g) / gap;
        h = mod(h, 6.0);
    }
    return vec3(h, s, v);
}

vec3 dev_hsvToRgb(vec3 hsv) {
    float h = mod(hsv.x, 6.0), s = hsv.y, v = hsv.z;
    float i = floor(h), f = h - i;
    float p = v * (1.0 - s), q = v * (1.0 - s * f), t = v * (1.0 - s * (1.0 - f));
    int k = int(i) % 6;
    if (k == 0) return vec3(v, t, p);
    if (k == 1) return vec3(q, v, p);
    if (k == 2) return vec3(p, v, t);
    if (k == 3) return vec3(p, q, v);
    if (k == 4) return vec3(t, p, v);
    return vec3(v, p, q);
}

vec3 dev_entry(sampler3D table, int h, int s, int v) { return texelFetch(table, ivec3(s, h, v), 0).rgb; }

vec3 dev_hueSat(vec3 c, sampler3D table, ivec3 divs, bool srgb) {
    // Hue/sat from the linear colour; an sRGB-encoded table encodes only the value axis.
    vec3 hsv = dev_rgbToHsv(max(c, 0.0));
    if (srgb) hsv.z = dev_encode(clamp(hsv.z, 0.0, 1.0));
    float hs = hsv.x * float(divs.x) / 6.0;
    float ss = hsv.y * float(divs.y - 1);
    int h0 = int(floor(hs)) % divs.x, h1 = (h0 + 1) % divs.x;
    float hf = hs - floor(hs);
    int s0 = min(int(floor(ss)), max(divs.y - 2, 0)), s1 = min(s0 + 1, divs.y - 1);
    float sf = clamp(ss - float(s0), 0.0, 1.0);
    int v0 = 0, v1 = 0;
    float vf = 0.0;
    if (divs.z > 1) {
        float vs = clamp(hsv.z, 0.0, 1.0) * float(divs.z - 1);
        v0 = min(int(floor(vs)), divs.z - 2);
        v1 = v0 + 1;
        vf = vs - float(v0);
    }
    vec3 a0 = mix(mix(dev_entry(table, h0, s0, v0), dev_entry(table, h0, s1, v0), sf),
                  mix(dev_entry(table, h1, s0, v0), dev_entry(table, h1, s1, v0), sf), hf);
    vec3 a1 = mix(mix(dev_entry(table, h0, s0, v1), dev_entry(table, h0, s1, v1), sf),
                  mix(dev_entry(table, h1, s0, v1), dev_entry(table, h1, s1, v1), sf), hf);
    vec3 e = mix(a0, a1, vf);
    hsv.x += e.x * (6.0 / 360.0);
    hsv.y = min(hsv.y * e.y, 1.0);
    hsv.z = clamp(hsv.z * e.z, 0.0, 1.0);
    if (srgb) hsv.z = dev_decode(hsv.z);
    return dev_hsvToRgb(hsv);
}

float dev_tone(float x) {
    float pos = clamp(x, 0.0, 1.0) * float(dev_curveSize - 1);
    int i = min(int(floor(pos)), dev_curveSize - 2);
    return mix(texelFetch(dev_curve, i, 0).r, texelFetch(dev_curve, i + 1, 0).r, pos - float(i));
}

vec3 dev_rgbTone(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    float mx = max(c.r, max(c.g, c.b)), mn = min(c.r, min(c.g, c.b));
    float cmx = dev_tone(mx), cmn = dev_tone(mn);
    if (mx <= mn) return vec3(cmx);
    return cmn + (cmx - cmn) * (c - mn) / (mx - mn);
}

vec3 developMain(vec3 cam) {
    vec3 x = min(cam, dev_cameraWhite);
    x = clamp(dev_cameraToProPhoto * x, 0.0, 1.0);
    if (dev_hsmDivs.x > 0) x = dev_hueSat(x, dev_hsm, dev_hsmDivs, dev_hsmSRGB);
    x = min(x * dev_exposureScale, 1.0);
    if (dev_lookDivs.x > 0) x = dev_hueSat(x, dev_look, dev_lookDivs, dev_lookSRGB);
    x = dev_rgbTone(x);
    x = clamp(dev_output * x, 0.0, 1.0);
    return dev_encodeSRGB ? dev_encode3(x) : x;
}
"""

_EMPTY_TABLE = HueSatTable(1, 1, 1, np.zeros((1, 1, 1, 3), np.float32))


def _table_texture(sampler: str, table: HueSatTable | None) -> LutTexture:
    t = table or _EMPTY_TABLE
    return LutTexture(sampler, 3, t.sat_divs, t.hue_divs, t.val_divs, 3, False,
                      np.ascontiguousarray(t.data, np.float32).ravel())


def develop_textures(p: RenderParams) -> list[LutTexture]:
    return [
        _table_texture("dev_hsm", p.hue_sat_map),
        _table_texture("dev_look", p.look_table),
        LutTexture("dev_curve", 1, len(p.tone_curve), 1, 1, 1, False, np.asarray(p.tone_curve, np.float32)),
    ]


def develop_uniforms(p: RenderParams, encode_srgb: bool) -> list[tuple[str, str, object]]:
    def divs(t):
        return (t.hue_divs, t.sat_divs, t.val_divs) if t is not None else (0, 0, 0)

    return [
        ("dev_cameraWhite", "vec3", tuple(float(v) for v in p.camera_white)),
        ("dev_cameraToProPhoto", "mat3", p.camera_to_prophoto),
        ("dev_exposureScale", "float", 1.0 / p.white),
        ("dev_output", "mat3", p.output),
        ("dev_encodeSRGB", "bool", encode_srgb),
        ("dev_hsmDivs", "ivec3", divs(p.hue_sat_map)),
        ("dev_hsmSRGB", "bool", bool(p.hue_sat_map and p.hue_sat_map.srgb_encoded)),
        ("dev_lookDivs", "ivec3", divs(p.look_table)),
        ("dev_lookSRGB", "bool", bool(p.look_table and p.look_table.srgb_encoded)),
        ("dev_curveSize", "int", len(p.tone_curve)),
    ]
