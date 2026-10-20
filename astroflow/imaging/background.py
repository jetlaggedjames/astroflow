"""Sky background modelling (light-pollution gradients, Moon glow, vignetting left-overs).

We sample the sky in a grid of boxes, skipping boxes with stars or the
target itself (robust statistics + rejection of bright boxes), then fit a
smooth surface: a low-order polynomial (good for simple gradients) or a
smoothed thin-plate spline (follows more complex glows).
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.interpolate import RBFInterpolator


def sample_background(img: np.ndarray, grid: int = 16, box: int = 0, reject: float = 2.0,
                      exclude_mask: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Return sample points (N,2: x,y) and values (N,C) of the sky background."""
    mono_in = img.ndim == 2
    data = img[..., None] if mono_in else img
    H, W, C = data.shape
    box = box or max(8, min(H, W) // (grid * 3))
    xs = np.linspace(box, W - box - 1, grid).astype(int)
    ys = np.linspace(box, H - box - 1, max(2, int(grid * H / W))).astype(int)
    pts, vals, lum = [], [], []
    for y in ys:
        for x in xs:
            patch = data[y - box // 2: y + box // 2 + 1, x - box // 2: x + box // 2 + 1]
            if exclude_mask is not None and exclude_mask[y, x] > 0.5:
                continue
            flat = patch.reshape(-1, C)
            finite = np.all(np.isfinite(flat), axis=1)
            if finite.mean() < 0.8:
                continue
            flat = flat[finite]
            med = np.median(flat, axis=0)
            # Reject bright pixels (stars) inside the box before taking the level.
            l = flat.mean(axis=1)
            lm = np.median(l)
            mad = np.median(np.abs(l - lm)) * 1.4826 + 1e-9
            keep = l < lm + 2.5 * mad
            if keep.sum() > 10:
                med = np.median(flat[keep], axis=0)
            pts.append((x, y))
            vals.append(med)
            lum.append(float(np.mean(med)))
    if not pts:
        return np.zeros((0, 2)), np.zeros((0, C))
    pts, vals, lum = np.array(pts, float), np.array(vals, float), np.array(lum)
    # Iteratively reject samples brighter than the surrounding trend (the galaxy/nebula).
    keep = np.ones(len(lum), bool)
    for _ in range(3):
        if keep.sum() < 8:
            break
        A = _poly_terms(pts[keep], W, H, 2)
        coef, *_ = np.linalg.lstsq(A, lum[keep], rcond=None)
        resid = lum - _poly_terms(pts, W, H, 2) @ coef
        s = np.median(np.abs(resid[keep])) * 1.4826 + 1e-9
        keep = resid < reject * s
    return pts[keep], vals[keep]


def _poly_terms(pts: np.ndarray, W: int, H: int, degree: int) -> np.ndarray:
    x = pts[:, 0] / W * 2 - 1
    y = pts[:, 1] / H * 2 - 1
    cols = []
    for i in range(degree + 1):
        for j in range(degree + 1 - i):
            cols.append((x ** i) * (y ** j))
    return np.column_stack(cols)


def fit_background(img: np.ndarray, method: str = "poly", degree: int = 2, grid: int = 16,
                   smoothing: float = 0.5, exclude_mask: np.ndarray | None = None) -> np.ndarray:
    """Model of the sky background, same shape as ``img``."""
    H, W = img.shape[:2]
    pts, vals = sample_background(img, grid=grid, exclude_mask=exclude_mask)
    C = 1 if img.ndim == 2 else img.shape[2]
    if len(pts) < 6:
        med = np.nanmedian(img.reshape(-1, C), axis=0)
        return np.broadcast_to(med if C > 1 else med[0], img.shape).astype(np.float32).copy()
    # Evaluate on a coarse grid, then upsample (fast for big images).
    step = max(1, min(H, W) // 128)
    gy, gx = np.mgrid[0:H:step, 0:W:step]
    gpts = np.column_stack([gx.ravel(), gy.ravel()]).astype(float)
    out = np.zeros((gy.shape[0], gy.shape[1], C), np.float32)
    for c in range(C):
        v = vals[:, c]
        if method == "rbf" and len(pts) >= 10:
            scale = np.array([W, H], float)
            rbf = RBFInterpolator(pts / scale, v, kernel="thin_plate_spline",
                                  smoothing=float(smoothing) * len(pts) * 1e-3 * np.var(v) / (np.var(v) + 1e-12) + 1e-6)
            out[..., c] = rbf(gpts / scale).reshape(gy.shape)
        else:
            A = _poly_terms(pts, W, H, degree)
            coef, *_ = np.linalg.lstsq(A, v, rcond=None)
            out[..., c] = (_poly_terms(gpts, W, H, degree) @ coef).reshape(gy.shape)
    full = cv2.resize(out, (W, H), interpolation=cv2.INTER_CUBIC) if step > 1 else out
    if full.ndim == 2:
        full = full[..., None]
    return (full[..., 0] if C == 1 else full).astype(np.float32)


def remove_gradient(img: np.ndarray, method: str = "poly", degree: int = 2, mode: str = "subtract",
                    keep_level: bool = True, **kw) -> np.ndarray:
    bg = fit_background(img, method, degree, **kw)
    if mode == "divide":
        lvl = np.nanmedian(bg, axis=(0, 1)) if img.ndim == 3 else np.nanmedian(bg)
        return (img / np.maximum(bg, 1e-6) * lvl).astype(np.float32)
    out = img - bg
    if keep_level:
        # Put the (now flat) background back at its median level so nothing goes negative.
        out = out + (np.nanmedian(bg, axis=(0, 1)) if img.ndim == 3 else np.nanmedian(bg))
    return out.astype(np.float32)
