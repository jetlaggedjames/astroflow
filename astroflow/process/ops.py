"""Every post-processing tool, with its settings described for the user interface.

Each tool is a function ``fn(img, p, ctx) -> img`` working on float32 images
(0..1, mono (H,W) or RGB (H,W,3)). Its settings (``Param``) carry a label,
range, default and a plain-language help text; the UI builds the sliders
from these automatically. ``advanced=True`` settings are hidden in Beginner
mode. ``spatial=True`` settings are sizes in pixels, scaled for previews.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import cv2
import numpy as np
from scipy.interpolate import PchipInterpolator

from .. import gpu
from ..imaging.background import fit_background
from ..imaging.quality import object_mask
from ..imaging.register import xcorr_shift
from ..imaging.stars import detect_stars, star_mask
from .core import (blur, estimate_psf_sigma, fft_convolve, lab_to_rgb, luminance, mad_sigma, moffat_psf,
                   psf_otf, rgb_to_lab, rgb_to_ycc, wavelet_layers, with_luminance, ycc_to_rgb)


@dataclass
class Param:
    name: str
    label: str
    kind: str = "float"            # float | int | bool | choice | points | text | file
    default: Any = 0.0
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list | None = None
    help: str = ""
    advanced: bool = False
    spatial: bool = False

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Op:
    id: str
    name: str
    category: str
    description: str
    params: list[Param]
    fn: Callable
    targets: tuple = ("planet", "moon", "dso")
    geometric: bool = False
    gpu: bool = False
    linear_input: bool = False      # works best before stretching
    mask_ok: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "category": self.category, "description": self.description,
                "params": [p.to_dict() for p in self.params], "targets": list(self.targets),
                "geometric": self.geometric, "gpu": self.gpu, "linear_input": self.linear_input,
                "mask_ok": self.mask_ok and not self.geometric}

    def defaults(self) -> dict:
        return {p.name: p.default for p in self.params}


@dataclass
class Ctx:
    scale: float = 1.0                # preview scale (1 = full resolution)
    meta: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    load_image: Callable | None = None  # loads another image (channel combine)


REGISTRY: dict[str, Op] = {}
CATEGORIES = ["Geometry", "Stretch & tone", "Colour", "Detail & sharpening", "Noise", "Deep-sky", "Planetary & lunar"]


def op(id: str, name: str, category: str, description: str, params: list[Param], **kw):
    def deco(fn):
        REGISTRY[id] = Op(id, name, category, description, params, fn, **kw)
        return fn
    return deco


def resolve(op_: Op, params: dict | None, scale: float = 1.0) -> dict:
    """Fill defaults, clamp to ranges, scale pixel sizes for previews."""
    out = {}
    params = params or {}
    for p in op_.params:
        v = params.get(p.name, p.default)
        if p.kind in ("float", "int") and v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = float(p.default)
            if p.min is not None:
                v = max(p.min, v)
            if p.max is not None:
                v = min(p.max, v)
            if p.spatial:
                v = v * scale
            if p.kind == "int" and not p.spatial:
                v = int(round(v))
        elif p.kind == "bool":
            v = bool(v)
        elif p.kind == "choice" and p.choices and v not in [c if not isinstance(c, (list, tuple)) else c[0] for c in p.choices]:
            v = p.default
        out[p.name] = v
    return out


def run_op(op_id: str, img: np.ndarray, params: dict | None, ctx: Ctx) -> np.ndarray:
    o = REGISTRY[op_id]
    p = resolve(o, params, ctx.scale)
    out = o.fn(img.astype(np.float32, copy=False), p, ctx)
    return np.nan_to_num(out.astype(np.float32, copy=False), nan=0.0, posinf=1.0, neginf=0.0)


def _per_channel(img: np.ndarray, fn: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    if img.ndim == 2:
        return fn(img)
    return np.dstack([fn(img[..., c]) for c in range(img.shape[2])])


def _mtf_scalar(m: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    return ((m - 1) * x) / ((2 * m - 1) * x - m)


def _mtf_arr(m: float, x: np.ndarray) -> np.ndarray:
    """Midtones transfer function: maps m -> 0.5, keeps 0 and 1."""
    x = np.clip(x, 0, 1)
    return ((m - 1) * x) / ((2 * m - 1) * x - m)


def _stats(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    data = img if img.ndim == 3 else img[..., None]
    step = max(1, int(math.sqrt(data.shape[0] * data.shape[1] / 400_000)))
    sub = data[::step, ::step].reshape(-1, data.shape[2])
    med = np.median(sub, axis=0)
    mad = np.median(np.abs(sub - med), axis=0) * 1.4826
    return med, mad


# =============================================================== Geometry ===

@op("crop", "Crop", "Geometry", "Cut away the edges. Values are fractions of the width/height (0 = left/top, 1 = right/bottom).",
    [Param("left", "Left", default=0.0, min=0, max=0.95, step=0.001),
     Param("top", "Top", default=0.0, min=0, max=0.95, step=0.001),
     Param("right", "Right", default=1.0, min=0.05, max=1, step=0.001),
     Param("bottom", "Bottom", default=1.0, min=0.05, max=1, step=0.001)], geometric=True)
def _crop(img, p, ctx):
    H, W = img.shape[:2]
    x0, x1 = int(round(p["left"] * W)), int(round(p["right"] * W))
    y0, y1 = int(round(p["top"] * H)), int(round(p["bottom"] * H))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return img
    return img[y0:y1, x0:x1].copy()


@op("rotate", "Rotate", "Geometry", "Turn the picture. Use 90-degree steps for orientation, or any angle to level a horizon or put a planet's pole up.",
    [Param("angle", "Angle (degrees)", default=0.0, min=-180, max=180, step=0.1, help="Positive turns counter-clockwise."),
     Param("expand", "Grow canvas to fit", "bool", default=False, help="Keep the corners instead of cutting them off.")],
    geometric=True)
def _rotate(img, p, ctx):
    a = p["angle"] % 360
    if abs(a) < 1e-6:
        return img
    if abs(a - 90) < 1e-6:
        return np.ascontiguousarray(np.rot90(img, 1))
    if abs(a - 180) < 1e-6:
        return np.ascontiguousarray(np.rot90(img, 2))
    if abs(a - 270) < 1e-6:
        return np.ascontiguousarray(np.rot90(img, 3))
    H, W = img.shape[:2]
    M = cv2.getRotationMatrix2D((W / 2, H / 2), p["angle"], 1.0)
    if p["expand"]:
        c, s = abs(M[0, 0]), abs(M[0, 1])
        nW, nH = int(H * s + W * c), int(H * c + W * s)
        M[0, 2] += nW / 2 - W / 2
        M[1, 2] += nH / 2 - H / 2
        W, H = nW, nH
    return cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=0)


@op("flip", "Flip / mirror", "Geometry", "Mirror the picture. A star diagonal or some telescopes show a mirrored view; flip it back here.",
    [Param("horizontal", "Left-right", "bool", default=False), Param("vertical", "Top-bottom", "bool", default=False)],
    geometric=True)
def _flip(img, p, ctx):
    out = img
    if p["horizontal"]:
        out = out[:, ::-1]
    if p["vertical"]:
        out = out[::-1]
    return np.ascontiguousarray(out)


@op("resize", "Resize", "Geometry", "Make the image bigger or smaller (percent). Planetary images are often enlarged 150-200% before sharpening.",
    [Param("percent", "Size (%)", default=100.0, min=10, max=400, step=1),
     Param("method", "Method", "choice", default="lanczos", choices=["lanczos", "bicubic", "area"], advanced=True)],
    geometric=True)
def _resize(img, p, ctx):
    f = p["percent"] / 100.0
    if abs(f - 1) < 1e-3:
        return img
    H, W = img.shape[:2]
    interp = {"lanczos": cv2.INTER_LANCZOS4, "bicubic": cv2.INTER_CUBIC, "area": cv2.INTER_AREA}[p["method"]]
    if f < 1:
        interp = cv2.INTER_AREA
    return cv2.resize(img, (max(1, int(round(W * f))), max(1, int(round(H * f)))), interpolation=interp)


# ========================================================= Stretch & tone ===

@op("auto_stretch", "Auto stretch", "Stretch & tone",
    "Brightens a dark, 'linear' stacked image so faint detail becomes visible, by setting the black point "
    "just below the sky background and lifting the midtones. A good first step for deep-sky images.",
    [Param("target", "Background brightness", default=0.2, min=0.05, max=0.5, step=0.01,
           help="How bright the sky background becomes (0.2 = dark grey)."),
     Param("shadows", "Black clipping (noise units)", default=-2.8, min=-6, max=0, step=0.1, advanced=True,
           help="Where the black point is set, in noise units below the background. Closer to 0 = darker, more clipping."),
     Param("linked", "Same for all colours", "bool", default=True, advanced=True,
           help="Off = stretch each colour separately (also neutralises a colour cast).")], linear_input=True)
def _auto_stretch(img, p, ctx):
    med, mad = _stats(img)
    def stretch(x, m_, s_):
        c0 = float(np.clip(m_ + p["shadows"] * s_, 0, 1))
        mid = _mtf_scalar(p["target"], float(m_ - c0)) if m_ > c0 else 0.5
        return _mtf_arr(mid, (x - c0) / max(1 - c0, 1e-6))
    if img.ndim == 2 or p["linked"]:
        m_, s_ = float(np.mean(med)), float(np.mean(mad))
        if img.ndim == 3:
            # Linked: same black point and midtones for all channels (keeps colour).
            return np.dstack([stretch(img[..., c], m_, s_) for c in range(3)])
        return stretch(img, m_, s_)
    return np.dstack([stretch(img[..., c], float(med[c]), float(mad[c])) for c in range(3)])


@op("levels", "Levels (black / mid / white)", "Stretch & tone",
    "Set the black point (everything darker becomes black), the white point (everything brighter becomes white) and the midtones.",
    [Param("black", "Black point", default=0.0, min=0, max=0.99, step=0.001),
     Param("white", "White point", default=1.0, min=0.01, max=1, step=0.001),
     Param("gamma", "Midtones", default=1.0, min=0.1, max=5, step=0.01, help="Above 1 brightens the middle tones, below 1 darkens."),
     Param("channel", "Channel", "choice", default="rgb", choices=["rgb", "r", "g", "b"], advanced=True)])
def _levels(img, p, ctx):
    b, w, g = p["black"], max(p["white"], p["black"] + 1e-3), p["gamma"]
    def f(x):
        return np.clip((x - b) / (w - b), 0, 1) ** (1.0 / g)
    if img.ndim == 2 or p["channel"] == "rgb":
        return f(img)
    out = img.copy()
    c = "rgb".index(p["channel"])
    out[..., c] = f(img[..., c])
    return out


@op("auto_levels", "Auto black & white point", "Stretch & tone",
    "Sets black and white points automatically from the image, clipping only a tiny fraction of pixels.",
    [Param("clip_low", "Black clip (%)", default=0.05, min=0, max=5, step=0.01),
     Param("clip_high", "White clip (%)", default=0.02, min=0, max=5, step=0.01),
     Param("per_channel", "Per colour", "bool", default=False, advanced=True)])
def _auto_levels(img, p, ctx):
    def f(x):
        lo, hi = np.percentile(x[:: max(1, x.shape[0] // 700), :: max(1, x.shape[1] // 700)], [p["clip_low"], 100 - p["clip_high"]])
        return lo, max(hi, lo + 1e-4)
    if img.ndim == 3 and p["per_channel"]:
        chans = []
        for c in range(3):
            lo, hi = f(img[..., c])
            chans.append(np.clip((img[..., c] - lo) / (hi - lo), 0, 1))
        return np.dstack(chans)
    lo, hi = f(luminance(img) if img.ndim == 3 else img)
    if img.ndim == 3:
        lo = min(lo, float(np.percentile(img.min(axis=2)[::4, ::4], p["clip_low"])))
        hi = max(hi, float(np.percentile(img.max(axis=2)[::4, ::4], 100 - p["clip_high"])))
    return np.clip((img - lo) / (hi - lo), 0, 1)


@op("curves", "Curves", "Stretch & tone",
    "Reshape brightness with a curve. Points are [input, output] pairs from 0 to 1. Drag points in the curve editor.",
    [Param("points", "Curve points", "points", default=[[0, 0], [0.25, 0.25], [0.75, 0.75], [1, 1]]),
     Param("channel", "Channel", "choice", default="rgb", choices=["rgb", "luminance", "r", "g", "b"])])
def _curves(img, p, ctx):
    pts = sorted([(float(a), float(b)) for a, b in (p["points"] or [[0, 0], [1, 1]])])
    xs = np.array([a for a, _ in pts])
    ys = np.array([b for _, b in pts])
    keep = np.concatenate([[True], np.diff(xs) > 1e-4])
    xs, ys = xs[keep], ys[keep]
    if len(xs) < 2:
        return img
    lut_x = np.linspace(0, 1, 4096)
    lut = np.clip(PchipInterpolator(xs, ys, extrapolate=True)(lut_x), 0, 1).astype(np.float32)
    def f(x):
        return np.interp(np.clip(x, 0, 1), lut_x, lut).astype(np.float32)
    ch = p["channel"]
    if img.ndim == 2:
        return f(img)
    if ch == "rgb":
        return f(img)
    if ch == "luminance":
        L = luminance(img)
        return with_luminance(img, f(L), L)
    out = img.copy()
    c = "rgb".index(ch)
    out[..., c] = f(img[..., c])
    return out


@op("arcsinh_stretch", "Arcsinh stretch (colour-preserving)", "Stretch & tone",
    "A strong stretch that keeps star colours (it brightens all colours of a pixel by the same factor). Great for deep-sky.",
    [Param("strength", "Stretch strength", default=50.0, min=1, max=2000, step=1, help="Higher = brighter faint parts."),
     Param("black", "Black point", default=0.0, min=0, max=0.5, step=0.0005)], linear_input=True)
def _asinh(img, p, ctx):
    beta = p["strength"]
    x = np.clip(img - p["black"], 0, None) / max(1 - p["black"], 1e-6)
    if img.ndim == 2:
        return np.arcsinh(beta * x) / np.arcsinh(beta)
    L = luminance(x)
    Ls = np.arcsinh(beta * L) / np.arcsinh(beta)
    out = with_luminance(x, Ls, L)
    mx = out.max(axis=2, keepdims=True)
    return np.where(mx > 1, out / np.maximum(mx, 1e-6), out)


@op("ghs", "Generalised hyperbolic stretch", "Stretch & tone",
    "A flexible stretch: 'Strength' sets how much, 'Focus point' sets which brightness gets the most contrast "
    "(put it at the sky background for faint nebulae), 'Shape' changes how the stretch is spread.",
    [Param("D", "Strength", default=5.0, min=0, max=20, step=0.1),
     Param("b", "Shape", default=2.0, min=0, max=15, step=0.1, advanced=True, help="0 = exponential; higher = more concentrated at the focus point."),
     Param("SP", "Focus point", default=0.0, min=0, max=0.99, step=0.001),
     Param("colour", "Preserve colour", "bool", default=True, advanced=True)], linear_input=True)
def _ghs(img, p, ctx):
    D, b, SP = p["D"], p["b"], p["SP"]
    if D <= 1e-6:
        return img
    def T(x):
        d = x - SP
        if b <= 1e-6:
            pos = 1 - np.exp(-D * np.abs(d))
        else:
            pos = 1 - (1 + b * D * np.abs(d)) ** (-1.0 / b)
        return np.sign(d) * pos
    t0, t1 = float(T(np.array(0.0))), float(T(np.array(1.0)))
    def f(x):
        return (T(np.clip(x, 0, 1)) - t0) / (t1 - t0)
    if img.ndim == 3 and p["colour"]:
        L = luminance(img)
        out = with_luminance(img, f(L), L)
        mx = out.max(axis=2, keepdims=True)
        return np.where(mx > 1, out / np.maximum(mx, 1e-6), out)
    return f(img)


@op("gamma", "Gamma", "Stretch & tone", "Simple brightness curve. Above 1 brightens shadows and midtones.",
    [Param("gamma", "Gamma", default=1.0, min=0.2, max=4, step=0.01)])
def _gamma(img, p, ctx):
    return np.clip(img, 0, 1) ** (1.0 / p["gamma"])


@op("hdr_compress", "HDR compression (bright cores)", "Stretch & tone",
    "Tames very bright areas (the core of M42 or a galaxy, the bright lunar limb) while keeping their detail visible.",
    [Param("amount", "Amount", default=0.5, min=0, max=1, step=0.01),
     Param("radius", "Structure size (px)", default=32.0, min=4, max=256, step=1, spatial=True, advanced=True)])
def _hdr(img, p, ctx):
    L = luminance(img).astype(np.float32)
    base = cv2.GaussianBlur(L, (0, 0), max(p["radius"], 0.5))
    detail = L - base
    a = p["amount"] * 4
    base_c = base * (1 + a) / (1 + a * base)
    base_c = base_c * (base.max() / max(base_c.max(), 1e-6))
    newL = np.clip(base_c + detail, 0, None)
    return with_luminance(img, newL, L) if img.ndim == 3 else newL


@op("local_contrast", "Local contrast", "Stretch & tone",
    "Boosts contrast of medium-sized structures (lunar craters, nebula clouds) without blowing out bright parts.",
    [Param("amount", "Amount", default=0.4, min=0, max=1, step=0.01),
     Param("tile", "Region size (px)", default=64.0, min=8, max=512, step=1, spatial=True, advanced=True),
     Param("limit", "Strength limit", default=2.0, min=1, max=8, step=0.1, advanced=True)])
def _local_contrast(img, p, ctx):
    L = np.clip(luminance(img), 0, 1).astype(np.float32)
    H, W = L.shape
    tiles = max(1, int(round(max(H, W) / max(p["tile"], 4))))
    clahe = cv2.createCLAHE(clipLimit=p["limit"], tileGridSize=(tiles, max(1, int(round(tiles * W / max(H, 1))))))
    L16 = (L * 65535).astype(np.uint16)
    Lc = clahe.apply(L16).astype(np.float32) / 65535
    newL = L * (1 - p["amount"]) + Lc * p["amount"]
    return with_luminance(img, newL, L) if img.ndim == 3 else newL


# ================================================================ Colour ===

@op("color_calibration", "Colour calibration", "Colour",
    "Removes colour casts. 'Neutral background' makes the empty sky grey. 'Stars' makes the average star white "
    "(like the Sun). 'Photometric' uses the star catalogue colours (needs a plate-solved image).",
    [Param("method", "Method", "choice", default="background+stars",
           choices=["background", "stars", "background+stars", "photometric", "gray_world"]),
     Param("strength", "Strength", default=1.0, min=0, max=1, step=0.01, advanced=True)],
    targets=("dso",), linear_input=True)
def _color_cal(img, p, ctx):
    if img.ndim == 2:
        return img
    out = img.copy()
    method = p["method"]
    if method == "photometric":
        sol = ctx.meta.get("solution")
        if not sol:
            ctx.notes.append("Photometric calibration needs a plate-solved image - used star colours instead.")
        method = "background+stars"
    if "background" in method:
        from ..imaging.background import sample_background
        pts, vals = sample_background(out, grid=12)
        bg = np.median(vals, axis=0) if len(vals) else np.median(out.reshape(-1, 3), axis=0)
        target = float(np.mean(bg))
        out = out - (bg - target) * p["strength"]
    if "stars" in method:
        st = detect_stars(luminance(out), sigma=8.0, max_stars=200)
        good = getattr(st, "unsaturated", np.ones(len(st), bool))
        if np.count_nonzero(good) >= 5:
            fl = []
            bgv = np.median(out.reshape(-1, 3), axis=0)
            for x, y, f in zip(st.x[good], st.y[good], st.fwhm[good]):
                r = int(max(2, 2 * f))
                yi, xi = int(y), int(x)
                patch = out[max(0, yi - r): yi + r + 1, max(0, xi - r): xi + r + 1].reshape(-1, 3)
                fl.append(np.sum(patch - bgv, axis=0))
            fl = np.array(fl)
            fl = fl[np.all(fl > 0, axis=1)]
            if len(fl) >= 5:
                ratio = np.median(fl / fl[:, 1:2], axis=0)  # relative to green
                mult = 1.0 / np.maximum(ratio, 1e-3)
                mult = mult / mult[1]
                mult = 1 + (mult - 1) * p["strength"]
                bgv2 = np.median(out.reshape(-1, 3), axis=0)
                out = (out - bgv2) * mult + bgv2
                ctx.notes.append(f"Star colour balance from {len(fl)} stars: R x{mult[0]:.2f}, B x{mult[2]:.2f}")
        else:
            ctx.notes.append("Not enough unsaturated stars for star colour calibration.")
    if method == "gray_world":
        m = out.reshape(-1, 3).mean(axis=0)
        out = out * (m.mean() / np.maximum(m, 1e-6))
    return np.clip(out, 0, None)


@op("white_balance", "White balance (manual)", "Colour",
    "Multiply red, green and blue by your own factors. Use to fine-tune the colour of a planet or the Moon.",
    [Param("r", "Red", default=1.0, min=0.2, max=3, step=0.005),
     Param("g", "Green", default=1.0, min=0.2, max=3, step=0.005),
     Param("b", "Blue", default=1.0, min=0.2, max=3, step=0.005)])
def _wb(img, p, ctx):
    if img.ndim == 2:
        return img
    return img * np.array([p["r"], p["g"], p["b"]], np.float32)


@op("saturation", "Saturation & vibrance", "Colour",
    "Makes colours stronger. 'Vibrance' boosts dull colours more than already-strong ones (looks more natural).",
    [Param("saturation", "Saturation", default=0.3, min=-1, max=3, step=0.01),
     Param("vibrance", "Vibrance", default=0.0, min=-1, max=3, step=0.01),
     Param("protect_dark", "Protect dark areas", default=0.1, min=0, max=0.5, step=0.01, advanced=True,
           help="Leaves the darkest parts (where colour is mostly noise) alone.")])
def _saturation(img, p, ctx):
    if img.ndim == 2:
        return img
    scale = float(max(1.0, img.max()))
    lab = rgb_to_lab(img / scale)
    a, b = lab[..., 1], lab[..., 2]
    C = np.hypot(a, b)
    f = (1 + p["saturation"]) * (1 + p["vibrance"] * np.clip(1 - C / 60.0, 0, 1))
    L = lab[..., 0] / 100.0
    protect = np.clip(L / max(p["protect_dark"], 1e-3), 0, 1) if p["protect_dark"] > 0 else 1.0
    f = 1 + (f - 1) * protect
    lab[..., 1] = a * f
    lab[..., 2] = b * f
    return np.clip(lab_to_rgb(lab), 0, 1) * scale


@op("scnr", "Remove green cast (SCNR)", "Colour",
    "Deep-sky objects are almost never green; this removes green noise and casts.",
    [Param("amount", "Amount", default=1.0, min=0, max=1, step=0.01)], targets=("dso",))
def _scnr(img, p, ctx):
    if img.ndim == 2:
        return img
    out = img.copy()
    neutral = (img[..., 0] + img[..., 2]) / 2
    g = np.minimum(img[..., 1], neutral)
    out[..., 1] = img[..., 1] * (1 - p["amount"]) + g * p["amount"]
    return out


@op("rgb_align", "RGB channel align (atmospheric dispersion)", "Colour",
    "Low planets show a blue fringe on one side and red on the other, because the air acts like a weak prism. "
    "This lines the colour channels back up. 'Automatic' measures the shift for you.",
    [Param("auto", "Automatic", "bool", default=True),
     Param("r_dx", "Red shift X (px)", default=0.0, min=-15, max=15, step=0.05, spatial=True, advanced=True),
     Param("r_dy", "Red shift Y (px)", default=0.0, min=-15, max=15, step=0.05, spatial=True, advanced=True),
     Param("b_dx", "Blue shift X (px)", default=0.0, min=-15, max=15, step=0.05, spatial=True, advanced=True),
     Param("b_dy", "Blue shift Y (px)", default=0.0, min=-15, max=15, step=0.05, spatial=True, advanced=True)],
    targets=("planet", "moon"))
def _rgb_align(img, p, ctx):
    if img.ndim == 2:
        return img
    H, W = img.shape[:2]
    out = img.copy()
    g = img[..., 1]
    for c, kx, ky in ((0, "r_dx", "r_dy"), (2, "b_dx", "b_dy")):
        ch = img[..., c]
        if p["auto"]:
            norm = lambda a: (a - a.mean()) / (a.std() + 1e-9)  # noqa: E731
            dx, dy, _ = xcorr_shift(norm(g), norm(ch))
            ctx.notes.append(f"{'Red' if c == 0 else 'Blue'} channel shifted by ({-dx:.2f}, {-dy:.2f}) px")
        else:
            dx, dy = -p[kx], -p[ky]
        M = np.float32([[1, 0, -dx], [0, 1, -dy]])
        out[..., c] = cv2.warpAffine(ch, M, (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    return out


@op("lunar_color", "Mineral Moon colours", "Planetary & lunar",
    "The Moon has very subtle colours from different minerals (blue titanium-rich seas, orange-brown highlands). "
    "This boosts them strongly while smoothing colour noise.",
    [Param("amount", "Colour boost", default=3.0, min=0, max=15, step=0.1),
     Param("smooth", "Colour smoothing (px)", default=3.0, min=0, max=20, step=0.1, spatial=True)],
    targets=("moon",))
def _lunar_color(img, p, ctx):
    if img.ndim == 2:
        return img
    scale = float(max(1.0, img.max()))
    lab = rgb_to_lab(img / scale)
    for c in (1, 2):
        ch = lab[..., c] - np.median(lab[..., c])
        if p["smooth"] > 0.3:
            ch = cv2.GaussianBlur(ch, (0, 0), p["smooth"])
        lab[..., c] = ch * (1 + p["amount"])
    return np.clip(lab_to_rgb(lab), 0, 1) * scale


@op("channel_combine", "Combine with another image (IR / luminance)", "Colour",
    "Use a second stacked image as the brightness (luminance) layer - e.g. a sharper infrared (IR-pass) capture "
    "for detail, with this image's colour. The second image is aligned automatically.",
    [Param("file", "Second image", "file", default=""),
     Param("weight", "Luminance weight", default=0.8, min=0, max=1, step=0.01),
     Param("mode", "Mode", "choice", default="luminance", choices=["luminance", "replace_red", "replace_green", "replace_blue"])],
    targets=("planet", "moon", "dso"))
def _channel_combine(img, p, ctx):
    if not p["file"] or ctx.load_image is None:
        ctx.notes.append("Choose a second image to combine.")
        return img
    other = ctx.load_image(p["file"])
    if other is None:
        ctx.notes.append("Could not open the second image.")
        return img
    other_l = luminance(other).astype(np.float32)
    H, W = img.shape[:2]
    if other_l.shape != (H, W):
        other_l = cv2.resize(other_l, (W, H), interpolation=cv2.INTER_AREA)
    base_l = luminance(img).astype(np.float32)
    dx, dy, _ = xcorr_shift(base_l, other_l)
    other_l = cv2.warpAffine(other_l, np.float32([[1, 0, dx], [0, 1, dy]]), (W, H), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    # match brightness
    other_l = (other_l - np.median(other_l)) * (base_l.std() / max(other_l.std(), 1e-9)) + np.median(base_l)
    if img.ndim == 2:
        return base_l * (1 - p["weight"]) + other_l * p["weight"]
    if p["mode"] == "luminance":
        newL = base_l * (1 - p["weight"]) + other_l * p["weight"]
        return with_luminance(img, np.clip(newL, 0, None), base_l)
    out = img.copy()
    c = {"replace_red": 0, "replace_green": 1, "replace_blue": 2}[p["mode"]]
    out[..., c] = img[..., c] * (1 - p["weight"]) + other_l * p["weight"]
    return out


# ==================================================== Detail & sharpening ===

def _limb_weight(img: np.ndarray, width: float) -> np.ndarray | None:
    """1 inside the planet, falling to 0 within ``width`` px of its edge (to stop edge ringing)."""
    if width <= 0.5:
        return None
    m, _ = object_mask(luminance(img).astype(np.float32))
    if m.mean() > 0.6:
        return None  # surface fills the frame: no limb
    d = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 5)
    return np.clip(d / width, 0, 1).astype(np.float32)


@op("wavelets", "Wavelet sharpening", "Detail & sharpening",
    "The classic planetary sharpening (like RegiStax). The image is split into layers of detail sizes: layer 1 is "
    "the finest detail (and noise), layer 6 the broadest. Raise a layer's slider to sharpen that size of detail; "
    "use its 'noise' slider to smooth noise in that layer.",
    [Param("l1", "Layer 1 (finest)", default=0.0, min=-1, max=40, step=0.1),
     Param("l2", "Layer 2", default=8.0, min=-1, max=40, step=0.1),
     Param("l3", "Layer 3", default=4.0, min=-1, max=40, step=0.1),
     Param("l4", "Layer 4", default=1.0, min=-1, max=40, step=0.1),
     Param("l5", "Layer 5", default=0.0, min=-1, max=40, step=0.1),
     Param("l6", "Layer 6 (broadest)", default=0.0, min=-1, max=40, step=0.1),
     Param("n1", "Layer 1 noise", default=0.6, min=0, max=3, step=0.01, advanced=True),
     Param("n2", "Layer 2 noise", default=0.3, min=0, max=3, step=0.01, advanced=True),
     Param("n3", "Layer 3 noise", default=0.1, min=0, max=3, step=0.01, advanced=True),
     Param("n4", "Layer 4 noise", default=0.0, min=0, max=3, step=0.01, advanced=True),
     Param("n5", "Layer 5 noise", default=0.0, min=0, max=3, step=0.01, advanced=True),
     Param("n6", "Layer 6 noise", default=0.0, min=0, max=3, step=0.01, advanced=True),
     Param("base", "Layer size scale", default=0.5, min=0.25, max=2, step=0.05, advanced=True,
           help="Bigger = every layer covers larger details (use for over-sampled images)."),
     Param("luminance_only", "Sharpen brightness only", "bool", default=True,
           help="Avoids coloured noise. Turn off to sharpen each colour separately."),
     Param("protect_limb", "Protect planet edge (px)", default=6.0, min=0, max=60, step=0.5, spatial=True,
           help="Reduces the bright/dark ring that sharpening creates along a planet's edge.")],
    targets=("planet", "moon"), gpu=True)
def _wavelets(img, p, ctx):
    gains = [p[f"l{i}"] for i in range(1, 7)]
    noise = [p[f"n{i}"] for i in range(1, 7)]
    if not any(abs(g) > 1e-6 for g in gains) and not any(n > 0 for n in noise):
        return img

    def sharpen(ch: np.ndarray) -> np.ndarray:
        def work(xp):
            a = xp.asarray(ch)
            layers, resid = wavelet_layers(xp, a, 6, p["base"], ctx.scale)
            out = resid
            sig1 = None
            for j, (lay, g, nz) in enumerate(zip(layers, gains, noise)):
                if nz > 0:
                    s = mad_sigma(xp, lay) if j == 0 or sig1 is None else sig1 * (0.5 ** j)
                    if j == 0:
                        sig1 = s
                    t = nz * s * 3
                    lay = xp.sign(lay) * xp.maximum(xp.abs(lay) - t, 0)
                out = out + lay * (1 + g)
            return out
        return gpu.run(work, what="wavelets")

    lw = _limb_weight(img, p["protect_limb"])
    if img.ndim == 3 and p["luminance_only"]:
        L = luminance(img).astype(np.float32)
        newL = sharpen(L)
        if lw is not None:
            newL = L + (newL - L) * lw
        return with_luminance(img, newL, L)
    out = _per_channel(img, sharpen)
    if lw is not None:
        w = lw[..., None] if img.ndim == 3 else lw
        out = img + (out - img) * w
    return out


@op("deconvolution", "Deconvolution (Richardson-Lucy)", "Detail & sharpening",
    "Undoes blur mathematically, given the shape of the blur (the 'PSF'). 'Automatic' measures the blur from "
    "stars or from the planet's edge. More iterations = sharper but noisier; regularisation keeps noise down.",
    [Param("psf", "Blur shape", "choice", default="auto", choices=["auto", "gaussian", "moffat"]),
     Param("fwhm", "Blur size FWHM (px)", default=2.5, min=0.5, max=15, step=0.05, spatial=True,
           help="Used when the blur shape is not automatic. For stars this is the star size."),
     Param("iterations", "Iterations", "int", default=15, min=1, max=200, step=1),
     Param("regularization", "Noise control", default=0.002, min=0, max=0.05, step=0.0005, advanced=True,
           help="Total-variation regularisation; higher keeps noise and artefacts down."),
     Param("luminance_only", "Brightness only", "bool", default=True),
     Param("dering", "Prevent dark rings", "bool", default=True, advanced=True,
           help="Stops the dark halos deconvolution can create around stars and bright edges."),
     Param("protect_limb", "Protect planet edge (px)", default=6.0, min=0, max=60, step=0.5, spatial=True)],
    gpu=True)
def _deconv(img, p, ctx):
    if p["psf"] == "auto":
        sigma, how = estimate_psf_sigma(img)
        sigma *= 1.0 if ctx.scale >= 1 else 1.0  # measured on the image being processed: already in its pixels
        ctx.notes.append(f"Blur measured {how}: sigma {sigma:.2f}px")
        kind = "gaussian"
    else:
        sigma = p["fwhm"] / 2.3548
        kind = p["psf"]
    sigma = max(sigma, 0.3)
    iters = int(p["iterations"])
    lam = p["regularization"]

    def rl(ch: np.ndarray) -> np.ndarray:
        def work(xp):
            d = xp.clip(xp.asarray(ch), 1e-6, None)
            pad = int(math.ceil(4 * sigma)) + 2
            if xp is np:
                d = np.pad(d, pad, mode="reflect")
            else:
                d = xp.pad(d, pad, mode="reflect")
            if kind == "moffat":
                psf = moffat_psf(sigma * 2.3548, 3.0)
                otf = psf_otf(xp, psf, d.shape)
                conv = lambda a: xp.clip(fft_convolve(xp, a, otf), 1e-9, None)  # noqa: E731
            else:
                conv = lambda a: blur(xp, a, sigma)  # noqa: E731
            u = d.copy()
            for _ in range(iters):
                est = conv(u)
                rel = d / xp.maximum(est, 1e-9)
                corr = conv(rel)
                if lam > 0:
                    gy, gx = xp.gradient(u)
                    mag = xp.sqrt(gx * gx + gy * gy) + 1e-6
                    div = xp.gradient(gx / mag, axis=1) + xp.gradient(gy / mag, axis=0)
                    u = u * corr / xp.maximum(1 - lam * div, 0.3)
                else:
                    u = u * corr
                u = xp.maximum(u, 0)
            return u[pad:-pad, pad:-pad]
        out = gpu.run(work, what="deconvolution")
        if p["dering"]:
            # Never darker than the darkest original value nearby: removes "panda eye" rings.
            r = max(1, int(round(2.5 * sigma)))
            floor = cv2.erode(ch.astype(np.float32), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
            out = np.maximum(out, floor)
        return out

    lw = _limb_weight(img, p["protect_limb"])
    if img.ndim == 3 and p["luminance_only"]:
        L = luminance(img).astype(np.float32)
        newL = rl(L)
        if lw is not None:
            newL = L + (newL - L) * lw
        return with_luminance(img, newL, L)
    out = _per_channel(img, rl)
    if lw is not None:
        w = lw[..., None] if img.ndim == 3 else lw
        out = img + (out - img) * w
    return out


@op("unsharp_mask", "Unsharp mask", "Detail & sharpening",
    "Simple, familiar sharpening: increases contrast at edges of a chosen size.",
    [Param("radius", "Radius (px)", default=2.0, min=0.3, max=50, step=0.1, spatial=True),
     Param("amount", "Amount", default=0.8, min=0, max=5, step=0.01),
     Param("threshold", "Threshold", default=0.0, min=0, max=0.2, step=0.001, advanced=True,
           help="Only sharpen differences bigger than this (protects smooth areas from noise).")])
def _usm(img, p, ctx):
    b = cv2.GaussianBlur(img, (0, 0), max(p["radius"], 0.3))
    d = img - b
    if p["threshold"] > 0:
        d = np.where(np.abs(d) > p["threshold"], d, 0)
    return img + p["amount"] * d


@op("limb_cleanup", "Planet edge clean-up", "Planetary & lunar",
    "Softens the bright or dark ring that strong sharpening leaves along the edge (limb) of a planet.",
    [Param("width", "Edge width (px)", default=4.0, min=1, max=40, step=0.5, spatial=True),
     Param("strength", "Strength", default=0.7, min=0, max=1, step=0.01)], targets=("planet",))
def _limb_cleanup(img, p, ctx):
    m, _ = object_mask(luminance(img).astype(np.float32))
    if m.mean() > 0.6:
        return img
    w = max(1, int(round(p["width"])))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * w + 1, 2 * w + 1))
    ring = (cv2.dilate(m.astype(np.uint8), k) - cv2.erode(m.astype(np.uint8), k)).astype(np.float32)
    ring = cv2.GaussianBlur(ring, (0, 0), w / 2) * p["strength"]
    sm = cv2.GaussianBlur(img, (0, 0), w / 2)
    r = ring[..., None] if img.ndim == 3 else ring
    return img * (1 - r) + sm * r


# ================================================================= Noise ===

@op("denoise", "Noise reduction", "Noise",
    "Smooths grain while keeping real detail. Brightness noise and colour noise are treated separately: colour "
    "noise can usually be removed strongly without losing anything.",
    [Param("method", "Method", "choice", default="wavelet", choices=["wavelet", "nlmeans"],
           help="'wavelet' is gentle and fast; 'nlmeans' (non-local means) is stronger for deep-sky backgrounds."),
     Param("luminance", "Brightness noise", default=0.5, min=0, max=3, step=0.01),
     Param("chroma", "Colour noise", default=1.0, min=0, max=3, step=0.01),
     Param("detail", "Keep detail", default=0.5, min=0, max=1, step=0.01, advanced=True,
           help="Higher protects fine detail more (removes less noise).")], gpu=True)
def _denoise(img, p, ctx):
    if p["method"] == "nlmeans":
        return _nlmeans(img, p)
    def wden(ch: np.ndarray, strength: float, layers: int = 4) -> np.ndarray:
        if strength <= 0:
            return ch
        def work(xp):
            a = xp.asarray(ch)
            lay, resid = wavelet_layers(xp, a, layers, 0.5, 1.0)
            s1 = mad_sigma(xp, lay[0]) + 1e-9
            out = resid
            for j, l in enumerate(lay):
                t = strength * s1 * (0.55 ** j) * 3 * (1.2 - p["detail"])
                out = out + xp.sign(l) * xp.maximum(xp.abs(l) - t, 0)
            return out
        return gpu.run(work, what="noise reduction")
    if img.ndim == 2:
        return wden(img, p["luminance"])
    ycc = rgb_to_ycc(img)
    ycc[..., 0] = wden(ycc[..., 0], p["luminance"])
    for c in (1, 2):
        ycc[..., c] = wden(ycc[..., c], p["chroma"] * 1.5, 5)
    return ycc_to_rgb(ycc)


def _nlmeans(img: np.ndarray, p: dict) -> np.ndarray:
    scale = float(max(img.max(), 1e-6))
    def run(ch, h):
        if h <= 0:
            return ch
        u = np.clip(ch / scale * 65535, 0, 65535).astype(np.uint16)
        noise = mad_sigma(np, (ch / scale - cv2.GaussianBlur(ch / scale, (0, 0), 1.0)).astype(np.float32)) * 65535
        hh = float(max(1.0, noise * h * 2.5 * (1.2 - p["detail"])))
        out = cv2.fastNlMeansDenoising(u, None, [hh], 7, 21, cv2.NORM_L1)
        return out.astype(np.float32) / 65535 * scale
    if img.ndim == 2:
        return run(img, p["luminance"])
    ycc = rgb_to_ycc(img)
    mn = ycc.min(axis=(0, 1))
    shifted = ycc - mn
    out = np.dstack([run(shifted[..., 0], p["luminance"]), run(shifted[..., 1], p["chroma"]), run(shifted[..., 2], p["chroma"])]) + mn
    return ycc_to_rgb(out)


# ============================================================== Deep-sky ===

@op("background_extraction", "Gradient / background removal", "Deep-sky",
    "Removes light-pollution gradients, Moon glow and uneven backgrounds by modelling the empty sky and "
    "taking it away. Do this early, before stretching.",
    [Param("method", "Model", "choice", default="poly", choices=["poly", "rbf"],
           help="'poly' for simple gradients; 'rbf' follows more complex glows."),
     Param("degree", "Smoothness (poly degree)", "int", default=2, min=1, max=4, step=1, advanced=True),
     Param("grid", "Samples across", "int", default=16, min=6, max=40, step=1, advanced=True),
     Param("mode", "Correction", "choice", default="subtract", choices=["subtract", "divide"], advanced=True,
           help="Subtract for light pollution; divide for vignetting left-overs.")],
    targets=("dso",), linear_input=True)
def _bge(img, p, ctx):
    bg = fit_background(img, p["method"], int(p["degree"]), int(p["grid"]))
    if p["mode"] == "divide":
        lvl = np.median(bg, axis=(0, 1)) if img.ndim == 3 else np.median(bg)
        return img / np.maximum(bg, 1e-6) * lvl
    lvl = np.median(bg, axis=(0, 1)) if img.ndim == 3 else np.median(bg)
    # Keep a small pedestal so nothing is clipped to black.
    return np.clip(img - bg + lvl * 0.1, 0, None)


@op("star_reduction", "Star reduction / removal", "Deep-sky",
    "Makes stars smaller (or removes them) so the nebula or galaxy stands out.",
    [Param("amount", "Amount", default=0.5, min=0, max=1, step=0.01),
     Param("mode", "Mode", "choice", default="reduce", choices=["reduce", "remove"]),
     Param("size", "Star size (px)", default=3.0, min=1, max=20, step=0.5, spatial=True, advanced=True),
     Param("sensitivity", "Detection threshold", default=4.0, min=2, max=15, step=0.5, advanced=True,
           help="Lower finds fainter stars.")], targets=("dso",))
def _star_reduction(img, p, ctx):
    m = star_mask(img, sigma=p["sensitivity"], grow=1.6)
    if p["mode"] == "remove":
        base = img if img.ndim == 3 else img[..., None]
        # A large median (via downscale) works for big stars without being slow.
        small = cv2.resize(base, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
        if small.ndim == 2:
            small = small[..., None]
        med = np.dstack([cv2.medianBlur(small[..., c].astype(np.float32), 5) for c in range(small.shape[2])])
        bg = cv2.resize(med, (base.shape[1], base.shape[0]), interpolation=cv2.INTER_CUBIC)
        if bg.ndim == 2:
            bg = bg[..., None]
        mm = (m * p["amount"])[..., None]
        out = base * (1 - mm) + np.minimum(bg, base) * mm
        return out if img.ndim == 3 else out[..., 0]
    r = max(1, int(round(p["size"] / 2)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    eroded = cv2.erode(img, k)
    mm = m * p["amount"]
    if img.ndim == 3:
        mm = mm[..., None]
    return img * (1 - mm) + eroded * mm
