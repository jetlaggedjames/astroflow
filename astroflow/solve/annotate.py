"""Labelling objects on a plate-solved image, plus compass and scale bar."""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

from ..astro.catalog import Catalog
from ..astro.ephemeris import BODIES, body_position
from .wcs import Solution


def _offset(ra: float, dec: float, pa_deg: float, dist_deg: float) -> tuple[float, float]:
    """Point at ``dist_deg`` from (ra, dec) in direction ``pa_deg`` (north through east)."""
    ra1, dec1, pa, d = map(math.radians, (ra, dec, pa_deg, dist_deg))
    dec2 = math.asin(math.sin(dec1) * math.cos(d) + math.cos(dec1) * math.sin(d) * math.cos(pa))
    ra2 = ra1 + math.atan2(math.sin(pa) * math.sin(d) * math.cos(dec1), math.cos(d) - math.sin(dec1) * math.sin(dec2))
    return math.degrees(ra2) % 360, math.degrees(dec2)


def objects_in_image(sol: Solution, include_stars: bool = True, max_star_mag: float = 7.5, max_dso_mag: float = 15.0,
                     when: dt.datetime | None = None, limit: int = 120) -> list[dict]:
    """Objects visible in the image with pixel position, size and orientation, ready to draw."""
    cat = Catalog.default()
    fw, fh = sol.fov_deg
    radius = 0.5 * math.hypot(fw, fh) * 1.1
    out = []
    pix_deg = sol.scale_arcsec / 3600
    for o in cat.in_field(sol.ra, sol.dec, radius + 1.0, limit=2000):
        if o.catalog == "stars":
            if not include_stars or (o.mag is not None and o.mag > max_star_mag):
                continue
        elif o.mag is not None and o.mag > max_dso_mag and not (o.size_arcmin and o.size_arcmin * 60 / sol.scale_arcsec > 40):
            continue
        x, y = sol.world2pix(o.ra, o.dec)
        x, y = float(x), float(y)
        semi_major = (o.size_arcmin or 0) / 2 / 60 / pix_deg
        semi_minor = (o.size2_arcmin or o.size_arcmin or 0) / 2 / 60 / pix_deg
        margin = max(semi_major, 0)
        if not (-margin <= x <= sol.width + margin and -margin <= y <= sol.height + margin):
            continue
        angle = 0.0
        if semi_major > 2:
            pa = o.pa if o.pa is not None else 0.0
            r2, d2 = _offset(o.ra, o.dec, pa, (o.size_arcmin or 1) / 120)
            x2, y2 = sol.world2pix(r2, d2)
            angle = math.degrees(math.atan2(float(y2) - y, float(x2) - x))
        out.append({"id": o.id, "label": o.label, "kind": o.kind, "x": x, "y": y, "rx": semi_major, "ry": semi_minor,
                    "angle": angle, "mag": o.mag, "catalog": o.catalog})
        if len(out) >= limit:
            break
    if when is not None:
        for b in BODIES:
            p = body_position(b, when)
            x, y = sol.world2pix(p["ra"], p["dec"])
            x, y = float(x), float(y)
            if 0 <= x < sol.width and 0 <= y < sol.height:
                r = p["diameter_arcsec"] / 2 / sol.scale_arcsec
                out.append({"id": p["name"], "label": p["name"], "kind": "Planet" if b not in ("sun", "moon") else p["name"],
                            "x": x, "y": y, "rx": r, "ry": r, "angle": 0.0, "mag": None, "catalog": "planets"})
    return out


def compass(sol: Solution) -> dict:
    """Directions of north and east in the image (unit vectors, image y pointing down)."""
    cx, cy = sol.width / 2, sol.height / 2
    ra0, dec0 = sol.pix2world(cx, cy)
    d = 0.1 * sol.fov_deg[1]
    nx, ny = sol.world2pix(*_offset(float(ra0), float(dec0), 0.0, d))
    ex, ey = sol.world2pix(*_offset(float(ra0), float(dec0), 90.0, d))
    vn = np.array([float(nx) - cx, float(ny) - cy])
    ve = np.array([float(ex) - cx, float(ey) - cy])
    return {"north": (vn / max(np.linalg.norm(vn), 1e-9)).tolist(), "east": (ve / max(np.linalg.norm(ve), 1e-9)).tolist()}


def scale_bar(scale_arcsec: float, width_px: int, fraction: float = 0.2) -> dict:
    """A round-number length (e.g. 5') close to ``fraction`` of the image width."""
    target = width_px * fraction * scale_arcsec  # arcsec
    nice = [1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 18000, 36000]
    v = min(nice, key=lambda n: abs(math.log(n / max(target, 1e-9))))
    label = f'{v}"' if v < 60 else (f"{v // 60}'" if v < 3600 else f"{v / 3600:g}°")
    return {"arcsec": v, "pixels": v / scale_arcsec, "label": label}


def draw_overlay(img: np.ndarray, items: list[dict], sol: Solution | None = None, show_compass: bool = True,
                 show_scale: bool = True, color=(255, 210, 90), font_scale: float = 1.0) -> np.ndarray:
    """Burn labels (and compass/scale bar) into an 8-bit RGB copy of the image."""
    from PIL import Image, ImageDraw, ImageFont
    base = img if img.dtype == np.uint8 else (np.clip(img, 0, 1) * 255).astype(np.uint8)
    if base.ndim == 2:
        base = np.repeat(base[..., None], 3, axis=2)
    H, W = base.shape[:2]
    im = Image.fromarray(base)
    dr = ImageDraw.Draw(im)
    size = max(12, int(round(min(W, H) / 55 * font_scale)))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:
        font = ImageFont.load_default()
    lw = max(1, size // 10)
    placed: list[tuple[float, float, float, float]] = []

    def free_spot(cands: list[tuple[float, float]], text: str) -> tuple[float, float]:
        """First candidate position whose text box doesn't overlap an earlier label."""
        tw, th = dr.textlength(text, font=font), size * 1.2
        for cx, cy in cands:
            cx = min(max(2, cx), W - tw - 2)
            cy = min(max(2, cy), H - th - 2)
            box = (cx, cy, cx + tw, cy + th)
            if not any(box[0] < b[2] and box[2] > b[0] and box[1] < b[3] and box[3] > b[1] for b in placed):
                placed.append(box)
                return cx, cy
        cx, cy = cands[0]
        placed.append((cx, cy, cx + tw, cy + th))
        return min(max(2, cx), W - tw - 2), min(max(2, cy), H - th - 2)

    # Big objects first so their labels get the best spots.
    for it in sorted(items, key=lambda i: -i["rx"]):
        x, y, rx, ry = it["x"], it["y"], it["rx"], it["ry"]
        if rx >= 6:
            pts = []
            a = math.radians(it["angle"])
            for k in range(73):
                t = 2 * math.pi * k / 72
                px = rx * math.cos(t)
                py = max(ry, 3) * math.sin(t)
                pts.append((x + px * math.cos(a) - py * math.sin(a), y + px * math.sin(a) + py * math.cos(a)))
            dr.line(pts, fill=color, width=lw)
            r = max(rx, ry)
            cands = [(x - dr.textlength(it["label"], font=font) / 2, y - r - size * 1.4), (x + rx * 0.75, y - r * 0.75 - size),
                     (x - dr.textlength(it["label"], font=font) / 2, y + r + 4), (x + r + 4, y - size / 2),
                     (x - r - 4 - dr.textlength(it["label"], font=font), y - size / 2)]
        else:
            g = max(6, size // 2)
            dr.line([(x - 2 * g, y), (x - g, y)], fill=color, width=lw)
            dr.line([(x + g, y), (x + 2 * g, y)], fill=color, width=lw)
            dr.line([(x, y - 2 * g), (x, y - g)], fill=color, width=lw)
            dr.line([(x, y + g), (x, y + 2 * g)], fill=color, width=lw)
            tl = dr.textlength(it["label"], font=font)
            cands = [(x + 2 * g + 2, y - size - 2), (x + 2 * g + 2, y + 2), (x - 2 * g - 2 - tl, y - size - 2),
                     (x - tl / 2, y + 2 * g + 2), (x - tl / 2, y - 2 * g - size * 1.3)]
        tx, ty = free_spot(cands, it["label"])
        dr.text((tx, ty), it["label"], fill=color, font=font, stroke_width=max(1, size // 8), stroke_fill=(0, 0, 0))
    if sol is not None and show_compass:
        c = compass(sol)
        L = min(W, H) * 0.08
        ox, oy = W - L * 1.6, H - L * 1.6
        for key, lab in (("north", "N"), ("east", "E")):
            vx, vy = c[key]
            dr.line([(ox, oy), (ox + vx * L, oy + vy * L)], fill=color, width=lw * 2)
            dr.text((ox + vx * L * 1.2 - size / 3, oy + vy * L * 1.2 - size / 2), lab, fill=color, font=font,
                    stroke_width=max(1, size // 8), stroke_fill=(0, 0, 0))
    if sol is not None and show_scale:
        sb = scale_bar(sol.scale_arcsec, W)
        x0, y0 = W * 0.04, H - size * 2.2
        dr.line([(x0, y0), (x0 + sb["pixels"], y0)], fill=color, width=lw * 2)
        dr.text((x0, y0 - size - 4), sb["label"], fill=color, font=font, stroke_width=max(1, size // 8), stroke_fill=(0, 0, 0))
    return np.asarray(im)
