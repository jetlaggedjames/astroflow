"""Turning a colour camera's raw mosaic (Bayer pattern) into an RGB image.

Colour astro cameras like the Uranus-C have a grid of tiny red, green and blue
filters over the pixels (the "Bayer" pattern, RGGB for the IMX585). Each raw
pixel only saw one colour; debayering fills in the missing two.

Note: OpenCV names its Bayer codes by a shifted 2x2 window, so a standard RGGB
sensor needs ``COLOR_BayerBG2RGB``. This mapping was verified by test
(tests/test_io.py::test_debayer_patterns).
"""
from __future__ import annotations

import cv2
import numpy as np

_CV_NAME = {"RGGB": "BG", "BGGR": "RG", "GRBG": "GB", "GBRG": "GR"}
PATTERNS = tuple(_CV_NAME)


def _code(pattern: str, method: str) -> int:
    name = _CV_NAME[pattern.upper()]
    suffix = {"bilinear": "", "vng": "_VNG", "ea": "_EA"}.get(method, "")
    return getattr(cv2, f"COLOR_Bayer{name}2RGB{suffix}")


def debayer(raw: np.ndarray, pattern: str, method: str = "bilinear") -> np.ndarray:
    """Debayer a 2-D uint8/uint16 (or float) mosaic into an (H, W, 3) RGB image.

    method: "bilinear" (fast, good for planetary), "vng" (8-bit only in OpenCV,
    sharper), "ea" (edge-aware, good default for deep sky).
    """
    if raw.ndim != 2:
        return raw
    pattern = pattern.upper()
    if pattern not in _CV_NAME:
        return raw
    src = raw
    if src.dtype not in (np.uint8, np.uint16):
        # OpenCV debayers integer data only: scale float [0,1] into 16-bit.
        src = np.clip(src * 65535.0, 0, 65535).astype(np.uint16)
        out = cv2.cvtColor(src, _code(pattern, method if method != "vng" else "ea"))
        return out.astype(np.float32) / 65535.0
    if method == "vng" and src.dtype != np.uint8:
        method = "ea"
    return cv2.cvtColor(src, _code(pattern, method))


def superpixel(raw: np.ndarray, pattern: str) -> np.ndarray:
    """Half-size RGB image (each 2x2 cell -> one pixel). No interpolation at all."""
    pattern = pattern.upper()
    h, w = raw.shape[0] // 2 * 2, raw.shape[1] // 2 * 2
    r = raw[:h, :w].astype(np.float32)
    # Pattern letters repeat (G twice): average them.
    acc: dict[str, list[np.ndarray]] = {"R": [], "G": [], "B": []}
    for i, ch in enumerate(pattern):
        sl = (slice(i // 2, None, 2), slice(i % 2, None, 2))
        acc[ch].append(r[sl])
    out = np.dstack([np.mean(acc[c], axis=0) for c in "RGB"])
    return out


def bayer_luminance(raw: np.ndarray) -> np.ndarray:
    """Fast mono view of a mosaic (2x2 box average), used for scoring and alignment."""
    r = raw.astype(np.float32)
    h, w = r.shape[0] // 2 * 2, r.shape[1] // 2 * 2
    r = r[:h, :w]
    return 0.25 * (r[0::2, 0::2] + r[0::2, 1::2] + r[1::2, 0::2] + r[1::2, 1::2])


def bayer_channel_masks(shape: tuple[int, int], pattern: str) -> dict[str, np.ndarray]:
    """Boolean masks telling which raw pixels are R, G and B."""
    masks = {c: np.zeros(shape, bool) for c in "RGB"}
    for i, ch in enumerate(pattern.upper()):
        masks[ch][i // 2::2, i % 2::2] = True
    return masks
