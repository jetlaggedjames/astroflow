"""Built-in plate solver (no extra software): for wider fields of view.

How it works: we take catalogue stars around a guessed position (from the
mount, the target name or a sweep of the whole sky), project them onto a
flat "tangent plane", and match their triangle pattern against the stars
detected in the image - the same matching used to align frames. A match
gives the exact position, scale and rotation.

It uses the bundled catalogue (stars to magnitude 9), so it needs a field
of view of roughly 1 degree or more. Narrower fields use ASTAP.
"""
from __future__ import annotations

import math
import time
from typing import Callable

import numpy as np

from ..astro.catalog import star_table
from ..astro.coords import angular_separation
from ..imaging.register import RegistrationError, match_stars
from ..imaging.stars import detect_stars
from .wcs import Solution


def _gnomonic(ra, dec, ra0, dec0):
    """Sky -> tangent plane (xi east, eta north), degrees."""
    ra, dec, ra0, dec0 = map(np.radians, (ra, dec, ra0, dec0))
    cosc = np.sin(dec0) * np.sin(dec) + np.cos(dec0) * np.cos(dec) * np.cos(ra - ra0)
    xi = np.cos(dec) * np.sin(ra - ra0) / cosc
    eta = (np.cos(dec0) * np.sin(dec) - np.sin(dec0) * np.cos(dec) * np.cos(ra - ra0)) / cosc
    return np.degrees(xi), np.degrees(eta)


def _try_center(xy_img: np.ndarray, ra0: float, dec0: float, radius: float, scale_deg: float | None,
                W: int, H: int, n_stars: int) -> Solution | None:
    st = star_table()
    sep = angular_separation(ra0, dec0, st["ra"], st["dec"])
    sel = np.nonzero(sep < radius)[0]
    if len(sel) < 8:
        return None
    sel = sel[np.argsort(st["mag"][sel])][: n_stars * 3]
    xi, eta = _gnomonic(st["ra"][sel], st["dec"][sel], ra0, dec0)
    # Catalogue "pixels": a mirrored plane (east left, north up) in units of the guessed pixel scale.
    unit = scale_deg or 1.0 / 3600
    cat = np.column_stack([-xi / unit, -eta / unit])
    try:
        tr = match_stars(xy_img[:n_stars], cat[: n_stars * 2], "similarity", n_stars=n_stars, tolerance=4.0)
    except RegistrationError:
        tr = None
    flipped = False
    if tr is None or tr.matches < 8:
        try:
            tr = match_stars(xy_img[:n_stars] * np.array([-1, 1]), cat[: n_stars * 2], "similarity", n_stars=n_stars, tolerance=4.0)
            flipped = True
        except RegistrationError:
            return None
    if tr.matches < 8 or (scale_deg and not (0.7 < tr.scale < 1.4)):
        return None
    # image pixel (x, y) -> tangent plane degrees: (xi, eta) = -unit * T(x, y)
    M = tr.matrix
    if flipped:
        M = M @ np.diag([-1.0, 1.0, 1.0])
    A = -unit * M[:2, :2]
    b = -unit * M[:2, 2]
    # Choose CRPIX at the image centre: tangent-plane coordinates there.
    cx, cy = W / 2 - 0.5, H / 2 - 0.5
    xi_c, eta_c = A @ np.array([cx, cy]) + b
    rac, decc = _inverse_gnomonic(xi_c, eta_c, ra0, dec0)
    hdr = {"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRVAL1": float(rac), "CRVAL2": float(decc),
           "CRPIX1": cx + 1, "CRPIX2": cy + 1, "CD1_1": A[0, 0], "CD1_2": A[0, 1], "CD2_1": A[1, 0], "CD2_2": A[1, 1],
           "RADESYS": "ICRS", "EQUINOX": 2000.0}
    sol = Solution.from_header(hdr, W, H, solver="built-in")
    sol.stars_matched = tr.matches
    # Re-centre the projection on the solved centre for accuracy (one refinement).
    return sol


def _inverse_gnomonic(xi, eta, ra0, dec0):
    xi, eta, ra0r, dec0r = math.radians(xi), math.radians(eta), math.radians(ra0), math.radians(dec0)
    rho = math.hypot(xi, eta)
    if rho < 1e-12:
        return ra0, dec0
    c = math.atan(rho)
    dec = math.asin(math.cos(c) * math.sin(dec0r) + eta * math.sin(c) * math.cos(dec0r) / rho)
    ra = ra0r + math.atan2(xi * math.sin(c), rho * math.cos(dec0r) * math.cos(c) - eta * math.sin(dec0r) * math.sin(c))
    return math.degrees(ra) % 360, math.degrees(dec)


def solve(img: np.ndarray, hint: dict | None = None, scale_arcsec: float | None = None, blind: bool = True,
          progress: Callable[[float, str], None] | None = None, check: Callable[[], None] | None = None) -> Solution | None:
    H, W = img.shape[:2]
    stars = detect_stars(img, sigma=6.0, max_stars=80)
    if len(stars) < 8:
        return None
    xy = stars.xy()
    scale_deg = scale_arcsec / 3600 if scale_arcsec else None
    fov = (max(W, H) * scale_deg) if scale_deg else 3.0
    radius = max(0.6 * fov, 0.5)
    t0 = time.time()
    centres: list[tuple[float, float]] = []
    if hint and hint.get("ra") is not None:
        centres.append((float(hint["ra"]), float(hint["dec"])))
        # Small spiral around the hint in case it is a bit off.
        for r in (0.5, 1.0):
            for a in range(0, 360, 60):
                d = r * fov
                centres.append((float(hint["ra"]) + d * math.cos(math.radians(a)) / max(math.cos(math.radians(float(hint["dec"]))), 0.1),
                                float(hint["dec"]) + d * math.sin(math.radians(a))))
    if blind:
        step = max(radius * 1.2, 2.0)
        decs = np.arange(-88, 89, step)
        for dec in decs:
            n = max(1, int(360 * math.cos(math.radians(dec)) / step))
            for k in range(n):
                centres.append((k * 360 / n, float(dec)))
    for i, (ra0, dec0) in enumerate(centres):
        if check:
            check()
        if progress and i % 25 == 0:
            progress(i / len(centres), f"Searching the sky: {i}/{len(centres)} areas")
        sol = _try_center(xy, ra0 % 360, max(-89.9, min(89.9, dec0)), radius, scale_deg, W, H, min(len(xy), 40))
        if sol is not None:
            # Verify with all detected stars near the solved centre.
            sol2 = _try_center(xy, sol.ra, sol.dec, radius, sol.scale_arcsec / 3600, W, H, min(len(xy), 60)) or sol
            sol2.seconds = round(time.time() - t0, 2)
            return sol2
    return None
