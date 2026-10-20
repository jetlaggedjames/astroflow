"""Shared building blocks for post-processing: blurs, colour spaces, wavelet layers, PSFs.

Functions taking ``xp`` work with NumPy (CPU) or CuPy (GPU) arrays alike.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from .. import gpu

LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)


def blur(xp, img, sigma: float):
    """Gaussian blur on CPU (OpenCV, fastest) or GPU (CuPy)."""
    if sigma <= 0.01:
        return img
    if xp is np:
        return cv2.GaussianBlur(img, (0, 0), float(sigma), borderType=cv2.BORDER_REFLECT)
    nd = gpu.ndimage(xp)
    if img.ndim == 3:
        return nd.gaussian_filter(img, (sigma, sigma, 0), mode="reflect")
    return nd.gaussian_filter(img, sigma, mode="reflect")


def luminance(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    return img @ LUMA if img.shape[2] == 3 else img.mean(axis=2)


def with_luminance(img: np.ndarray, new_lum: np.ndarray, old_lum: np.ndarray | None = None) -> np.ndarray:
    """Replace the luminance of an RGB image while keeping its colour ratios."""
    if img.ndim == 2:
        return new_lum
    old = luminance(img) if old_lum is None else old_lum
    ratio = new_lum / np.maximum(old, 1e-6)
    out = img * ratio[..., None]
    # Very dark pixels: avoid amplifying noise colour.
    dark = old < 1e-4
    if np.any(dark):
        out[dark] = new_lum[dark][..., None]
    return out


def rgb_to_lab(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(np.clip(img, 0, 1).astype(np.float32), cv2.COLOR_RGB2Lab)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_Lab2RGB)


def rgb_to_ycc(img: np.ndarray) -> np.ndarray:
    """Linear luma/chroma split (works outside 0..1 unlike Lab)."""
    y = img @ LUMA
    cb = img[..., 2] - y
    cr = img[..., 0] - y
    return np.dstack([y, cb, cr]).astype(np.float32)


def ycc_to_rgb(ycc: np.ndarray) -> np.ndarray:
    y, cb, cr = ycc[..., 0], ycc[..., 1], ycc[..., 2]
    r = cr + y
    b = cb + y
    g = (y - LUMA[0] * r - LUMA[2] * b) / LUMA[1]
    return np.dstack([r, g, b]).astype(np.float32)


def wavelet_layers(xp, img, n_layers: int, base_sigma: float = 0.5, scale: float = 1.0):
    """Multiscale decomposition: detail layers (fine to coarse) + residual.

    Layer j holds structures around 2^j pixels (at full resolution). ``scale``
    < 1 is used for reduced-size previews so layers keep their real-world meaning.
    """
    layers = []
    prev = img
    for j in range(n_layers):
        sigma = base_sigma * (2 ** j) * scale * 2.0
        cur = blur(xp, img, sigma) if sigma > 0.15 else img
        layers.append(prev - cur)
        prev = cur
    return layers, prev


def mad_sigma(xp, a) -> float:
    sub = a[:: max(1, a.shape[0] // 512), :: max(1, a.shape[1] // 512)]
    med = xp.median(sub)
    return float(1.4826 * xp.median(xp.abs(sub - med)))


# ------------------------------------------------------------------ PSFs ---

def gaussian_psf(sigma: float, size: int | None = None) -> np.ndarray:
    size = size or int(2 * math.ceil(4 * sigma) + 1)
    r = np.arange(size) - size // 2
    g = np.exp(-(r ** 2) / (2 * sigma * sigma))
    k = np.outer(g, g)
    return (k / k.sum()).astype(np.float32)


def moffat_psf(fwhm: float, beta: float = 3.0, size: int | None = None) -> np.ndarray:
    alpha = fwhm / (2 * math.sqrt(2 ** (1 / beta) - 1))
    size = size or int(2 * math.ceil(3 * fwhm) + 1)
    yy, xx = np.mgrid[0:size, 0:size] - size // 2
    k = (1 + (xx * xx + yy * yy) / (alpha * alpha)) ** (-beta)
    return (k / k.sum()).astype(np.float32)


def estimate_psf_sigma(img: np.ndarray) -> tuple[float, str]:
    """Estimate the blur (Gaussian sigma, px) from stars, or from a planet's edge."""
    from ..imaging.stars import detect_stars, measure_summary
    lum = luminance(img).astype(np.float32)
    st = detect_stars(lum, sigma=6.0, max_stars=300)
    s = measure_summary(st)
    if s["stars"] >= 8 and s["fwhm"]:
        return float(s["fwhm"]) / 2.3548, f"from {s['stars']} stars (FWHM {s['fwhm']:.2f}px)"
    # Planet/Moon: the steepness of the limb (edge) tells the blur.
    sm = cv2.GaussianBlur(lum, (0, 0), 0.7)
    gx = cv2.Sobel(sm, cv2.CV_32F, 1, 0, ksize=3) / 8
    gy = cv2.Sobel(sm, cv2.CV_32F, 0, 1, ksize=3) / 8
    g = np.hypot(gx, gy)
    thr = np.percentile(g, 99.7)
    edges = g >= thr
    if edges.sum() < 20:
        return 1.2, "default (no stars or edges found)"
    lo, hi = np.percentile(lum, 2), np.percentile(lum, 99.5)
    step = max(hi - lo, 1e-6)
    peak = float(np.median(g[edges]))
    # For an edge of height `step` blurred by a Gaussian, max gradient = step / (sigma*sqrt(2*pi)).
    sigma = step / (peak * math.sqrt(2 * math.pi) + 1e-9)
    sigma = float(np.clip(math.sqrt(max(sigma * sigma - 0.7 ** 2, 0.09)), 0.3, 6.0))
    return sigma, "from the sharpness of the planet's edge"


def psf_otf(xp, psf: np.ndarray, shape: tuple[int, int]):
    """FFT of a PSF padded/centred for an image of ``shape``."""
    H, W = shape
    pad = np.zeros((H, W), np.float32)
    kh, kw = psf.shape
    pad[:kh, :kw] = psf
    pad = np.roll(pad, (-(kh // 2), -(kw // 2)), axis=(0, 1))
    return xp.fft.rfft2(xp.asarray(pad))


def fft_convolve(xp, img, otf):
    return xp.fft.irfft2(xp.fft.rfft2(img) * otf, s=img.shape)
