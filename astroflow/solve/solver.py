"""Choosing a plate solver and running it.

Order (engine "auto"): ASTAP if installed -> built-in solver (wide fields,
or near a known position) -> Astrometry.net online (only if you allowed it).
Results are stored next to the image as ``<file>.wcs.json`` (originals are
never modified) and in the library index.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import settings
from ..io.debayer import debayer
from ..io.images import read_image, to_float
from ..io.source import open_source
from . import astap, builtin, online
from .wcs import Solution

log = logging.getLogger("astroflow.solve")


def status() -> dict:
    s = settings()
    exe = astap.find_astap(s.get("solver.astap_path", ""))
    db = astap.find_database_dir(exe, s.get("solver.astap_db_dir", ""))
    fov = field_of_view()
    return {"astap": str(exe) if exe else None, "astap_db_dir": str(db) if db else None,
            "databases": astap.installed_databases(db), "online_allowed": bool(s.get("solver.allow_online")),
            "online_key": bool(s.get("solver.astrometry_api_key")), "fov_deg": fov,
            "recommended_db": astap.recommend_database(*fov), "builtin_ok": min(fov) >= 2.0,
            "builtin_note": "The built-in solver needs a field of view of about 2-3 degrees or more."}


def field_of_view(binning: int = 1) -> tuple[float, float]:
    s = settings()
    scale = s.pixel_scale_arcsec(binning)
    return (float(s.get("equipment.sensor_w_px", 3856)) * scale / 3600 / binning,
            float(s.get("equipment.sensor_h_px", 2180)) * scale / 3600 / binning)


def load_for_solving(path: str | Path) -> tuple[np.ndarray, dict]:
    """A float image for solving: debayered stills, or a middle frame of a video."""
    p = Path(path)
    if p.suffix.lower() in (".ser", ".avi", ".mp4", ".mov", ".mkv"):
        src = open_source(p)
        try:
            return src.get_float(src.count // 2), dict(src.meta)
        finally:
            src.close()
    data, meta = read_image(p)
    pat = str(meta.get("BAYERPAT") or "").upper()
    if data.ndim == 2 and pat in ("RGGB", "BGGR", "GRBG", "GBRG"):
        data = debayer(data, pat)
    return to_float(data), meta


def hint_from_meta(meta: dict) -> dict | None:
    """Approximate pointing from file headers (mount position or target name), if any."""
    from ..astro.coords import parse_dec, parse_ra
    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if num(meta.get("RA")) and num(meta.get("DEC")):
        return {"ra": float(meta["RA"]), "dec": float(meta["DEC"])}          # degrees (common FITS usage)
    if meta.get("OBJCTRA") and meta.get("OBJCTDEC"):
        try:
            return {"ra": parse_ra(str(meta["OBJCTRA"])), "dec": parse_dec(str(meta["OBJCTDEC"]))}  # "HH MM SS"
        except ValueError:
            pass
    if num(meta.get("CRVAL1")) and num(meta.get("CRVAL2")):
        return {"ra": float(meta["CRVAL1"]), "dec": float(meta["CRVAL2"])}
    obj = meta.get("OBJECT")
    if obj:
        from ..astro.catalog import Catalog
        o = Catalog.default().get(str(obj))
        if o:
            return {"ra": o.ra, "dec": o.dec}
    return None


def scale_from_meta(meta: dict, width: int) -> float | None:
    """Arc-seconds per pixel from the file (focal length + pixel size), else from settings."""
    try:
        fl = float(meta.get("FOCALLEN") or 0)
        px = float(meta.get("XPIXSZ") or meta.get("PIXSIZE1") or 0)
        b = float(meta.get("XBINNING") or 1)
        if fl > 0 and px > 0:
            return 206.265 * px * b / fl
    except (TypeError, ValueError):
        pass
    s = settings()
    sensor_w = float(s.get("equipment.sensor_w_px", 3856))
    # A narrower image than the sensor is usually a cropped region (ROI), not binning.
    return s.pixel_scale_arcsec() if width <= sensor_w else s.pixel_scale_arcsec() * sensor_w / width


def solve_image(img: np.ndarray, hint: dict | None = None, scale_arcsec: float | None = None, engine: str | None = None,
                allow_blind: bool = True, progress: Callable[[float, str], None] | None = None,
                check: Callable[[], None] | None = None) -> tuple[Solution | None, list[str]]:
    s = settings()
    engine = engine or s.get("solver.engine", "auto")
    prog = progress or (lambda f, m=None: None)
    notes: list[str] = []
    H, W = img.shape[:2]
    if scale_arcsec is None:
        scale_arcsec = scale_from_meta({}, W)
    fov_h = H * scale_arcsec / 3600 if scale_arcsec else None
    if engine in ("auto", "astap"):
        exe = astap.find_astap(s.get("solver.astap_path", ""))
        db = astap.find_database_dir(exe, s.get("solver.astap_db_dir", ""))
        if exe and db:
            prog(0.1, "Solving with ASTAP")
            sol = astap.solve(img, exe, db, hint, fov_h)
            if sol is None and hint:
                notes.append("ASTAP: no solution near the expected position - trying a wider search")
                sol = astap.solve(img, exe, db, hint, fov_h, search_radius=60)
            if sol is None and fov_h:
                sol = astap.solve(img, exe, db, hint, None)  # let ASTAP try other field sizes
            if sol:
                return sol, notes
            notes.append("ASTAP could not solve this image.")
        else:
            notes.append("ASTAP is not installed (run the setup wizard to add it)" if not exe else "No ASTAP star database found.")
        if engine == "astap":
            return None, notes
    if engine in ("auto", "builtin"):
        prog(0.3, "Solving with the built-in solver")
        # A whole-sky search is slow; only do it when there is no position hint (or on request).
        blind = allow_blind and (engine == "builtin" or hint is None)
        sol = builtin.solve(img, hint, scale_arcsec, blind=blind,
                            progress=lambda f, m=None: prog(0.3 + 0.5 * f, m), check=check)
        if sol:
            return sol, notes
        notes.append("Built-in solver found no match (it needs a field of view of ~2-3 degrees or more).")
        if engine == "builtin":
            return None, notes
    if engine in ("auto", "online") and s.get("solver.allow_online") and s.get("solver.astrometry_api_key"):
        try:
            prog(0.8, "Solving online with Astrometry.net")
            sol = online.solve(img, s.get("solver.astrometry_api_key"), hint, scale_arcsec, progress=prog, check=check)
            if sol:
                return sol, notes
            notes.append("Astrometry.net could not solve this image.")
        except Exception as e:
            notes.append(f"Online solving failed: {e}")
    return None, notes


def solve_file(path: str | Path, hint: dict | None = None, engine: str | None = None,
               progress: Callable[[float, str], None] | None = None, check: Callable[[], None] | None = None) -> dict:
    img, meta = load_for_solving(path)
    hint = hint or hint_from_meta(meta)
    sol, notes = solve_image(img, hint, scale_from_meta(meta, img.shape[1]), engine, progress=progress, check=check)
    out = {"path": str(path), "solved": sol is not None, "notes": notes}
    if sol:
        sol.save_sidecar(path)
        out["solution"] = sol.to_dict()
    return out
