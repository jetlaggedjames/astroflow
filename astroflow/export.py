"""Exporting finished images: formats, sizes, colour profiles, watermark, captions, labels.

Everything is saved locally - nothing is uploaded anywhere.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import struct
from pathlib import Path

import cv2
import numpy as np

from . import SOFTWARE_ID
from .config import atomic_write_text, presets_dir, settings
from .io.images import write_fits, write_standard, write_tiff

FORMATS = {
    "tiff": {"label": "TIFF", "bits": [8, 16, 32], "ext": "tif"},
    "png": {"label": "PNG", "bits": [8, 16], "ext": "png"},
    "jpg": {"label": "JPEG", "bits": [8], "ext": "jpg", "quality": True},
    "webp": {"label": "WebP", "bits": [8], "ext": "webp", "quality": True},
    "fits": {"label": "FITS (data)", "bits": [16, 32], "ext": "fits"},
}

SIZE_PRESETS = {
    "original": {"label": "Original size"},
    "instagram_portrait": {"label": "Instagram portrait 1080 x 1350", "w": 1080, "h": 1350, "fit": "cover"},
    "instagram_square": {"label": "Instagram square 1080 x 1080", "w": 1080, "h": 1080, "fit": "cover"},
    "facebook": {"label": "Facebook 2048 px long side", "long": 2048},
    "x_twitter": {"label": "X / Twitter 1600 x 900", "w": 1600, "h": 900, "fit": "contain"},
    "forum": {"label": "Forum / email 1600 px long side", "long": 1600},
    "hd_wallpaper": {"label": "HD wallpaper 1920 x 1080", "w": 1920, "h": 1080, "fit": "cover"},
    "4k_wallpaper": {"label": "4K wallpaper 3840 x 2160", "w": 3840, "h": 2160, "fit": "cover"},
    "print_4x6": {"label": 'Print 4 x 6 in @ 300 dpi', "in": (6, 4), "dpi": 300, "fit": "cover"},
    "print_8x10": {"label": 'Print 8 x 10 in @ 300 dpi', "in": (10, 8), "dpi": 300, "fit": "cover"},
    "print_a4": {"label": "Print A4 @ 300 dpi", "in": (11.69, 8.27), "dpi": 300, "fit": "cover"},
    "print_a3": {"label": "Print A3 @ 300 dpi", "in": (16.54, 11.69), "dpi": 300, "fit": "cover"},
}

PROFILES = {"srgb": "sRGB (screens, web)", "adobergb": "Adobe RGB (1998) (wide-gamut printing)", "none": "None (raw data)"}


# -------------------------------------------------------------- colour ---

def _s15(v: float) -> bytes:
    return struct.pack(">i", int(round(v * 65536)))


def _xyz_tag(x: float, y: float, z: float) -> bytes:
    return b"XYZ " + b"\0" * 4 + _s15(x) + _s15(y) + _s15(z)


def _desc_tag(text: str) -> bytes:
    a = text.encode("ascii") + b"\0"
    return b"desc" + b"\0" * 4 + struct.pack(">I", len(a)) + a + b"\0" * 4 + b"\0" * 4 + b"\0" * 2 + b"\0" + b"\0" * 67


def _text_tag(text: str) -> bytes:
    return b"text" + b"\0" * 4 + text.encode("ascii") + b"\0"


def _curve_tag(gamma: float) -> bytes:
    return b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", int(round(gamma * 256)))


def adobe_rgb_icc() -> bytes:
    """A standard Adobe RGB (1998)-compatible ICC v2 matrix/TRC profile."""
    tags = [
        (b"desc", _desc_tag("Adobe RGB (1998) compatible (AstroFlow)")),
        (b"cprt", _text_tag("No copyright, use freely")),
        (b"wtpt", _xyz_tag(0.96420, 1.0, 0.82491)),
        (b"rXYZ", _xyz_tag(0.60974, 0.31111, 0.01947)),
        (b"gXYZ", _xyz_tag(0.20528, 0.62567, 0.06087)),
        (b"bXYZ", _xyz_tag(0.14919, 0.06322, 0.74457)),
        (b"rTRC", _curve_tag(563 / 256)),
        (b"gTRC", _curve_tag(563 / 256)),
        (b"bTRC", _curve_tag(563 / 256)),
    ]
    offset = 128 + 4 + 12 * len(tags)
    table, data = b"", b""
    for sig, blob in tags:
        while (offset + len(data)) % 4:
            data += b"\0"
        table += sig + struct.pack(">II", offset + len(data), len(blob))
        data += blob
    body = struct.pack(">I", len(tags)) + table + data
    size = 128 + len(body)
    now = dt.datetime.now(dt.timezone.utc)
    header = (struct.pack(">I", size) + b"none" + struct.pack(">I", 0x02100000) + b"mntr" + b"RGB " + b"XYZ "
              + struct.pack(">6H", now.year, now.month, now.day, now.hour, now.minute, now.second)
              + b"acsp" + b"MSFT" + b"\0" * 4 + b"none" + b"\0" * 4 + b"\0" * 8 + b"\0" * 4
              + _s15(0.9642) + _s15(1.0) + _s15(0.8249) + b"none" + b"\0" * 44)
    assert len(header) == 128
    return header + body


def srgb_icc() -> bytes:
    from PIL import ImageCms
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def to_profile(img: np.ndarray, profile: str) -> np.ndarray:
    """Images are edited as sRGB (what the screen shows). Convert for other profiles."""
    if profile != "adobergb" or img.ndim != 3:
        return img
    x = np.clip(img, 0, 1)
    lin = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.7152, 0.2848, 0.0000],     # linear sRGB -> linear Adobe RGB (both D65)
                  [0.0000, 1.0000, 0.0000],
                  [0.0000, 0.0412, 0.9588]], np.float32)
    out = np.clip(lin @ M.T, 0, 1)
    return (out ** (256 / 563)).astype(np.float32)


# --------------------------------------------------------------- sizing ---

def target_size(w: int, h: int, opts: dict) -> tuple[int, int, str, int | None]:
    """(new_w, new_h, fit, dpi) for the chosen size option."""
    mode = opts.get("size", "original")
    if mode == "percent":
        f = float(opts.get("percent", 100)) / 100
        return max(1, round(w * f)), max(1, round(h * f)), "stretch", None
    if mode == "custom":
        cw, ch = int(opts.get("width") or 0), int(opts.get("height") or 0)
        if cw and ch:
            return cw, ch, opts.get("fit", "contain"), None
        if cw:
            return cw, max(1, round(h * cw / w)), "stretch", None
        if ch:
            return max(1, round(w * ch / h)), ch, "stretch", None
        return w, h, "stretch", None
    p = SIZE_PRESETS.get(mode)
    if not p or mode == "original":
        return w, h, "stretch", None
    if "long" in p:
        f = p["long"] / max(w, h)
        f = min(f, 1.0)
        return max(1, round(w * f)), max(1, round(h * f)), "stretch", None
    if "in" in p:
        a, b = p["in"]
        pw, ph = (round(a * p["dpi"]), round(b * p["dpi"])) if w >= h else (round(b * p["dpi"]), round(a * p["dpi"]))
        return pw, ph, p.get("fit", "cover"), p["dpi"]
    tw, th = p["w"], p["h"]
    if (w < h) != (tw < th) and p.get("fit") == "contain":
        tw, th = th, tw
    return tw, th, p.get("fit", "cover"), None


def resize_to(img: np.ndarray, w: int, h: int, fit: str, bg: float = 0.0) -> np.ndarray:
    H, W = img.shape[:2]
    if (W, H) == (w, h):
        return img
    if fit == "stretch":
        interp = cv2.INTER_AREA if w < W else cv2.INTER_LANCZOS4
        return cv2.resize(img, (w, h), interpolation=interp)
    f = max(w / W, h / H) if fit == "cover" else min(w / W, h / H)
    nw, nh = max(1, round(W * f)), max(1, round(H * f))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_LANCZOS4)
    if fit == "cover":
        x0, y0 = (nw - w) // 2, (nh - h) // 2
        return r[y0:y0 + h, x0:x0 + w]
    canvas = np.full((h, w) + img.shape[2:], bg, np.float32)
    x0, y0 = (w - nw) // 2, (h - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = r
    return canvas


# ----------------------------------------------------- watermark/caption ---

def _font(size: int, path: str = ""):
    from PIL import ImageFont
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    for cand in ("arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _anchor(pos: str, W: int, H: int, w: int, h: int, margin: int) -> tuple[int, int]:
    x = {"left": margin, "center": (W - w) // 2, "right": W - w - margin}
    y = {"top": margin, "middle": (H - h) // 2, "bottom": H - h - margin}
    v, hz = (pos.split("-") + ["center"])[:2] if "-" in pos else (("middle", "center") if pos == "center" else ("bottom", "right"))
    return x.get(hz, W - w - margin), y.get(v, H - h - margin)


def apply_watermark(rgb8: np.ndarray, wm: dict) -> np.ndarray:
    """wm = {"type": "text"|"image", "text", "image_path", "position": "bottom-right", "size": % of width,
             "opacity": 0..1, "margin": % of width, "color": "#ffffff", "font": path}"""
    from PIL import Image, ImageDraw
    if not wm or not wm.get("enabled", True):
        return rgb8
    base = Image.fromarray(rgb8).convert("RGBA")
    W, H = base.size
    margin = int(W * float(wm.get("margin", 2)) / 100)
    opacity = float(wm.get("opacity", 0.6))
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    if wm.get("type", "text") == "image" and wm.get("image_path"):
        logo = Image.open(wm["image_path"]).convert("RGBA")
        tw = max(1, int(W * float(wm.get("size", 15)) / 100))
        logo = logo.resize((tw, max(1, int(logo.height * tw / logo.width))), Image.LANCZOS)
        alpha = logo.split()[3].point(lambda a: int(a * opacity))
        logo.putalpha(alpha)
        x, y = _anchor(wm.get("position", "bottom-right"), W, H, logo.width, logo.height, margin)
        layer.paste(logo, (x, y), logo)
    else:
        text = wm.get("text") or ""
        if not text:
            return rgb8
        size = max(10, int(W * float(wm.get("size", 3)) / 100))
        font = _font(size, wm.get("font", ""))
        d = ImageDraw.Draw(layer)
        box = d.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        x, y = _anchor(wm.get("position", "bottom-right"), W, H, tw, th, margin)
        col = wm.get("color", "#ffffff").lstrip("#")
        rgb = tuple(int(col[i:i + 2], 16) for i in (0, 2, 4)) if len(col) == 6 else (255, 255, 255)
        d.text((x - box[0], y - box[1]), text, font=font, fill=rgb + (int(255 * opacity),))
    return np.asarray(Image.alpha_composite(base, layer).convert("RGB"))


def caption_lines(meta: dict, custom: str = "") -> list[str]:
    s = settings()
    eq = s.get("equipment")
    target = meta.get("OBJECT") or meta.get("target") or ""
    date = str(meta.get("DATE-OBS") or meta.get("date") or "")[:10]
    exp = meta.get("EXPTIME")
    n = meta.get("NCOMBINE") or meta.get("frames")
    line1 = " - ".join(x for x in [target, date, s.get("location.name") or ""] if x)
    parts = [eq.get("telescope"), meta.get("INSTRUME") or eq.get("camera")]
    barlows = eq.get("barlows") or []
    b = barlows[int(eq.get("active_barlow", 0))] if barlows else None
    if b and float(b.get("factor", 1)) != 1.0:
        parts.append(b.get("name"))
    if eq.get("filter"):
        parts.append(eq.get("filter"))
    line2 = " | ".join(str(p) for p in parts if p)
    det = []
    if n:
        det.append(f"{int(n):,} frames")
    if exp:
        e = float(exp)
        det.append(f"{e * 1000:.1f} ms" if e < 1 else f"{e:g} s")
        if n and e >= 1:
            det.append(f"total {int(n) * e / 60:.0f} min")
    if meta.get("GAIN") is not None:
        det.append(f"gain {meta['GAIN']}")
    det.append(f"processed with {SOFTWARE_ID}")
    lines = [line1, line2, ", ".join(det)]
    if custom:
        lines.append(custom)
    return [ln for ln in lines if ln]


def add_caption_bar(rgb8: np.ndarray, lines: list[str], position: str = "bottom") -> np.ndarray:
    from PIL import Image, ImageDraw
    H, W = rgb8.shape[:2]
    size = max(12, W // 70)
    font = _font(size)
    pad = size // 2
    bar_h = pad * 2 + len(lines) * int(size * 1.35)
    bar = Image.new("RGB", (W, bar_h), (12, 12, 14))
    d = ImageDraw.Draw(bar)
    for i, ln in enumerate(lines):
        d.text((pad * 2, pad + i * int(size * 1.35)), ln, font=font, fill=(225, 225, 225) if i == 0 else (170, 170, 175))
    bar_np = np.asarray(bar)
    return np.vstack([bar_np, rgb8] if position == "top" else [rgb8, bar_np])


# ----------------------------------------------------------------- export ---

def export_image(img: np.ndarray, out_path: str | Path, opts: dict, meta: dict | None = None,
                 solution: dict | None = None, history: list[str] | None = None) -> dict:
    """Export a processed float image (0..1) with all options. Returns info about the file written."""
    meta = dict(meta or {})
    fmt = opts.get("format", "jpg").lower()
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format {fmt}")
    bits = int(opts.get("bits", FORMATS[fmt]["bits"][0]))
    if bits not in FORMATS[fmt]["bits"]:
        bits = FORMATS[fmt]["bits"][0]
    out = Path(out_path)
    if out.suffix.lower().lstrip(".") not in (FORMATS[fmt]["ext"], fmt):
        out = out.with_suffix("." + FORMATS[fmt]["ext"])
    out.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(np.nan_to_num(img.astype(np.float32)), 0, 1)
    H, W = data.shape[:2]
    tw, th, fit, dpi = target_size(W, H, opts)
    sx, sy = tw / W, th / H
    data = resize_to(data, tw, th, fit)
    if fmt == "fits":
        write_fits(out, data, {**meta, "AFSTATUS": "exported"}, history, bits=bits)
        return {"path": str(out), "width": tw, "height": th, "bytes": out.stat().st_size}
    overlays = opts.get("annotate") or opts.get("caption") or opts.get("watermark", {}).get("enabled") or opts.get("scale_bar") or opts.get("compass")
    profile = opts.get("profile", "srgb")
    if overlays:
        # Overlays are drawn in 8-bit; high bit depth is kept only without overlays.
        rgb8 = (np.clip(data if data.ndim == 3 else np.repeat(data[..., None], 3, axis=2), 0, 1) * 255).astype(np.uint8)
        if solution and (opts.get("annotate") or opts.get("scale_bar") or opts.get("compass")) and fit == "stretch":
            from .solve import annotate
            from .solve.wcs import Solution
            sol = Solution.from_dict(solution)
            items = annotate.objects_in_image(sol) if opts.get("annotate") else []
            for it in items:
                it.update({"x": it["x"] * sx, "y": it["y"] * sy, "rx": it["rx"] * sx, "ry": it["ry"] * sy})
            sol_scaled = Solution(sol.ra, sol.dec, sol.scale_arcsec / sx, sol.rotation_deg, sol.flipped, tw, th,
                                  _scale_header(sol.header, sx, sy))
            rgb8 = annotate.draw_overlay(rgb8, items, sol_scaled, show_compass=bool(opts.get("compass")),
                                         show_scale=bool(opts.get("scale_bar")))
        if opts.get("watermark", {}).get("enabled"):
            rgb8 = apply_watermark(rgb8, opts["watermark"])
        if opts.get("caption"):
            rgb8 = add_caption_bar(rgb8, caption_lines(meta, opts.get("caption_text", "")), opts.get("caption_position", "bottom"))
        data = rgb8.astype(np.float32) / 255.0
        if bits > 8 and fmt in ("tiff", "png"):
            bits = 8 if fmt == "png" else bits
    data = to_profile(data, profile) if data.ndim == 3 else data
    icc = None if profile == "none" else (adobe_rgb_icc() if profile == "adobergb" else srgb_icc())
    meta_out = {k: v for k, v in meta.items() if isinstance(v, (str, int, float)) and not str(k).startswith("_")}
    meta_out["AFSTATUS"] = "exported"
    if fmt == "tiff":
        write_tiff(out, data, bits, {**meta_out, "history": history or []}, icc_profile=icc)
    else:
        write_standard(out, data, fmt, bits=bits, quality=int(opts.get("quality", 92)), icc_profile=icc, meta=meta_out)
    if dpi:
        _set_dpi(out, fmt, dpi)
    return {"path": str(out), "width": int(data.shape[1]), "height": int(data.shape[0]), "bytes": out.stat().st_size,
            "format": fmt, "bits": bits, "profile": profile, "dpi": dpi}


def _scale_header(h: dict, sx: float, sy: float) -> dict:
    out = dict(h)
    for k in ("CD1_1", "CD2_1"):
        if k in out:
            out[k] = float(out[k]) / sx
    for k in ("CD1_2", "CD2_2"):
        if k in out:
            out[k] = float(out[k]) / sy
    if "CRPIX1" in out:
        out["CRPIX1"] = (float(out["CRPIX1"]) - 0.5) * sx + 0.5
    if "CRPIX2" in out:
        out["CRPIX2"] = (float(out["CRPIX2"]) - 0.5) * sy + 0.5
    for k in list(out):
        if k.startswith(("A_", "B_", "AP_", "BP_")):
            out.pop(k)
    return out


def _set_dpi(path: Path, fmt: str, dpi: int) -> None:
    from PIL import Image
    if fmt in ("jpg", "png"):
        with Image.open(path) as im:
            im.load()
            info = {"dpi": (dpi, dpi)}
            if im.info.get("icc_profile"):
                info["icc_profile"] = im.info["icc_profile"]
            im.save(path, **({"quality": 95} if fmt == "jpg" else {}), **info)
    elif fmt == "tiff":
        import tifffile
        data = tifffile.imread(path)
        with tifffile.TiffFile(path) as tf:
            desc = tf.pages[0].description
            icc_tag = tf.pages[0].tags.get(34675)
            icc = icc_tag.value if icc_tag else None
        extratags = [(34675, 7, len(icc), icc, True)] if icc else []
        tifffile.imwrite(path, data, photometric="rgb" if data.ndim == 3 else "minisblack", description=desc,
                         resolution=(dpi, dpi), resolutionunit="INCH", extratags=extratags, software=SOFTWARE_ID,
                         compression="zlib" if data.dtype != np.float32 else None, metadata=None)


# ------------------------------------------------------- watermark presets ---

def _wm_file() -> Path:
    return presets_dir() / "watermarks.json"


def list_watermarks() -> dict:
    f = _wm_file()
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_watermark(name: str, wm: dict) -> dict:
    d = list_watermarks()
    d[name] = wm
    atomic_write_text(_wm_file(), json.dumps(d, indent=1))
    return d


def delete_watermark(name: str) -> dict:
    d = list_watermarks()
    d.pop(name, None)
    atomic_write_text(_wm_file(), json.dumps(d, indent=1))
    return d


def default_filename(meta: dict, fmt: str, suffix: str = "") -> str:
    target = str(meta.get("OBJECT") or "image").replace(" ", "")
    date = str(meta.get("DATE-OBS") or dt.date.today().isoformat())[:10]
    name = f"{target}_{date}{('_' + suffix) if suffix else ''}"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) + "." + FORMATS[fmt]["ext"]
