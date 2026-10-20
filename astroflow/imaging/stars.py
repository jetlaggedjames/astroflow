"""Finding stars and measuring them.

For each star we measure its position (to a fraction of a pixel), brightness,
size (FWHM = how wide the star's blur is, in pixels: smaller = sharper focus
and steadier air) and eccentricity (0 = round, near 1 = stretched, e.g. by
tracking errors or field rotation).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class StarList:
    x: np.ndarray
    y: np.ndarray
    flux: np.ndarray
    peak: np.ndarray
    fwhm: np.ndarray
    ecc: np.ndarray
    background: float
    noise: float
    shape: tuple[int, int]

    def __len__(self) -> int:
        return len(self.x)

    def brightest(self, n: int) -> "StarList":
        idx = np.argsort(-self.flux)[:n]
        return self.subset(idx)

    def subset(self, idx: np.ndarray) -> "StarList":
        return StarList(self.x[idx], self.y[idx], self.flux[idx], self.peak[idx], self.fwhm[idx], self.ecc[idx],
                        self.background, self.noise, self.shape)

    def xy(self) -> np.ndarray:
        return np.column_stack([self.x, self.y])

    def summary(self) -> dict:
        n = len(self)
        return {
            "stars": int(n),
            "fwhm": float(np.median(self.fwhm)) if n else None,
            "eccentricity": float(np.median(self.ecc)) if n else None,
            "background": float(self.background),
            "noise": float(self.noise),
            "snr": float(np.median(self.peak) / max(self.noise, 1e-9)) if n else 0.0,
        }


def to_mono(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        return img.astype(np.float32).mean(axis=2)
    return img.astype(np.float32, copy=False)


def background_map(img: np.ndarray, box: int = 64) -> tuple[np.ndarray, float]:
    """Smooth sky background (stars removed) and the pixel noise level."""
    h, w = img.shape
    box = int(max(16, min(box, h // 2 or 1, w // 2 or 1)))
    ny, nx = max(1, h // box), max(1, w // box)
    crop = img[: ny * box, : nx * box].reshape(ny, box, nx, box).transpose(0, 2, 1, 3).reshape(ny, nx, -1)
    # Clip the brightest pixels (stars) out of each box before taking the median.
    med = np.median(crop, axis=2)
    lo = np.percentile(crop, 50, axis=2)
    hi = np.percentile(crop, 80, axis=2)
    bg = np.where(hi > lo, 0.5 * (med + lo), med).astype(np.float32)
    if ny >= 3 and nx >= 3:
        bg = cv2.medianBlur(bg, 3)
    full = cv2.resize(bg, (w, h), interpolation=cv2.INTER_CUBIC) if (ny > 1 or nx > 1) else np.full((h, w), bg[0, 0], np.float32)
    resid = img[:: max(1, h // 512), :: max(1, w // 512)] - full[:: max(1, h // 512), :: max(1, w // 512)]
    mad = float(np.median(np.abs(resid - np.median(resid))))
    return full, max(1.4826 * mad, 1e-6)


def detect_stars(img: np.ndarray, sigma: float = 5.0, max_stars: int = 2000, min_area: int = 3,
                 max_area: int | None = None, saturation: float = 0.98, border: int = 8) -> StarList:
    """Detect stars in a mono or RGB float image (0..1)."""
    mono = to_mono(img)
    h, w = mono.shape
    bg, noise = background_map(mono)
    sub = mono - bg
    smooth = cv2.GaussianBlur(sub, (0, 0), 1.0)
    thresh = sigma * noise / 1.5  # smoothing lowers the noise by ~1.5x for a sigma=1 Gaussian
    mask = (smooth > thresh).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    empty = StarList(*(np.zeros(0, np.float64) for _ in range(6)), float(np.median(bg)), noise, (h, w))
    if n <= 1:
        return empty
    area = stats[1:, cv2.CC_STAT_AREA]
    max_area = max_area or max(400, (h * w) // 2000)
    keep = (area >= min_area) & (area <= max_area)
    lab_ids = np.nonzero(keep)[0] + 1
    if len(lab_ids) == 0:
        return empty
    # Moments of every component in one pass with bincount.
    flat = labels.ravel()
    wgt = np.clip(sub, 0, None).ravel().astype(np.float64)
    yy, xx = np.indices((h, w))
    xs, ys = xx.ravel().astype(np.float64), yy.ravel().astype(np.float64)
    sel = (flat > 0) & keep[np.maximum(flat - 1, 0)]
    f, wv, xv, yv = flat[sel], wgt[sel], xs[sel], ys[sel]
    m0 = np.bincount(f, wv, minlength=n)
    m0s = np.where(m0 > 0, m0, 1)
    cx = np.bincount(f, wv * xv, minlength=n) / m0s
    cy = np.bincount(f, wv * yv, minlength=n) / m0s
    dx, dy = xv - cx[f], yv - cy[f]
    mxx = np.bincount(f, wv * dx * dx, minlength=n) / m0s
    myy = np.bincount(f, wv * dy * dy, minlength=n) / m0s
    mxy = np.bincount(f, wv * dx * dy, minlength=n) / m0s
    peak = np.zeros(n)
    np.maximum.at(peak, f, sub.ravel()[sel])
    raw_peak = np.zeros(n)
    np.maximum.at(raw_peak, f, mono.ravel()[sel])
    ids = lab_ids
    tr = mxx[ids] + myy[ids]
    det = np.sqrt(np.maximum(((mxx[ids] - myy[ids]) / 2) ** 2 + mxy[ids] ** 2, 0))
    l1 = np.maximum(tr / 2 + det, 1e-9)
    l2 = np.maximum(tr / 2 - det, 1e-9)
    # Moments measured only above the detection threshold underestimate the width.
    # For a Gaussian cut at level t (fraction of its peak) the measured variance is
    # sigma^2 * g with g = (1 - t(1 - ln t)) / (1 - t); undo that.
    sig2 = (l1 + l2) / 2
    t = np.clip(thresh / np.maximum(peak[ids], 1e-9), 1e-6, 0.9)
    g = (1 - t * (1 - np.log(t))) / (1 - t)
    sig = np.sqrt(sig2 / np.clip(g, 0.05, 1.0))
    fwhm = 2.3548 * sig
    ecc = np.sqrt(np.clip(1 - l2 / l1, 0, 1))
    x, y, flux = cx[ids], cy[ids], m0[ids]
    good = ((x > border) & (x < w - border) & (y > border) & (y < h - border)
            & (raw_peak[ids] < saturation) & (fwhm > 0.7) & (ecc < 0.95))
    # Keep saturated stars for alignment but flag them by not using them for FWHM later.
    sat = raw_peak[ids] >= saturation
    good_all = ((x > border) & (x < w - border) & (y > border) & (y < h - border) & (fwhm > 0.7) & (ecc < 0.95)) | (sat & (ecc < 0.95))
    idx = np.nonzero(good_all)[0]
    order = idx[np.argsort(-flux[idx])][:max_stars]
    stars = StarList(x[order], y[order], flux[order], peak[ids][order], fwhm[order], ecc[order],
                     float(np.median(bg)), noise, (h, w))
    stars.unsaturated = good[order]  # type: ignore[attr-defined]
    return stars


def measure_summary(stars: StarList) -> dict:
    """Quality summary that ignores saturated stars for size/shape statistics."""
    s = stars.summary()
    good = getattr(stars, "unsaturated", None)
    if good is not None and np.count_nonzero(good) >= 3:
        s["fwhm"] = float(np.median(stars.fwhm[good]))
        s["eccentricity"] = float(np.median(stars.ecc[good]))
    return s


def star_mask(img: np.ndarray, sigma: float = 4.0, grow: float = 1.5, max_stars: int = 5000) -> np.ndarray:
    """Soft mask (0..1) covering the stars, sized by each star's brightness."""
    mono = to_mono(img)
    stars = detect_stars(mono, sigma=sigma, max_stars=max_stars, min_area=2, border=0)
    mask = np.zeros(mono.shape, np.float32)
    if len(stars) == 0:
        return mask
    flux_rel = np.sqrt(stars.flux / max(np.median(stars.flux), 1e-9))
    radii = np.clip(stars.fwhm * grow * np.clip(flux_rel, 0.7, 4), 1.5, 60)
    for x, y, r in zip(stars.x, stars.y, radii):
        cv2.circle(mask, (int(round(x)), int(round(y))), int(round(r)), 1.0, -1, lineType=cv2.LINE_AA)
    return cv2.GaussianBlur(mask, (0, 0), max(1.0, float(np.median(radii)) / 3))
