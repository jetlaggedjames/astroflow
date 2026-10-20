"""Masks: apply an adjustment to only part of the picture.

A mask is a grey image from 0 (leave alone) to 1 (apply fully). Types:
  * luminance - by brightness (e.g. only the faint nebula, not the bright core)
  * stars     - just the stars (for star reduction, or to protect stars)
  * object    - the planet/Moon disk, or just a ring at its edge (the limb)
  * brush     - painted by hand in the viewer
Every mask can be feathered (soft edge), grown/shrunk and inverted.
Masks are described by small settings (not pixels), so they re-create
exactly at any image size.
"""
from __future__ import annotations

import cv2
import numpy as np

from ..imaging.quality import object_mask
from ..imaging.stars import star_mask
from .core import luminance

MASK_TYPES = {
    "luminance": {"label": "Brightness range", "params": {"low": 0.0, "high": 0.5, "softness": 0.1}},
    "stars": {"label": "Stars", "params": {"sigma": 4.0, "grow": 1.5}},
    "object": {"label": "Planet / Moon disk", "params": {"ring": False, "ring_width": 12.0}},
    "brush": {"label": "Painted by hand", "params": {}},
}


def build_mask(defn: dict, img: np.ndarray, scale: float = 1.0) -> np.ndarray:
    """Create a mask (H, W) float32 0..1 for image ``img`` from a mask definition.

    ``scale`` is the preview scale (pixel sizes in the definition refer to full resolution).
    """
    kind = defn.get("type", "luminance")
    p = {**MASK_TYPES.get(kind, {}).get("params", {}), **(defn.get("params") or {})}
    H, W = img.shape[:2]
    if kind == "luminance":
        L = luminance(img).astype(np.float32)
        lo, hi, soft = float(p["low"]), float(p["high"]), max(float(p["softness"]), 1e-4)
        m = np.clip((L - (lo - soft)) / soft, 0, 1) * np.clip(((hi + soft) - L) / soft, 0, 1)
    elif kind == "stars":
        m = star_mask(img, sigma=float(p["sigma"]), grow=float(p["grow"]))
    elif kind == "object":
        obj, _ = object_mask(luminance(img).astype(np.float32))
        m = obj.astype(np.float32)
        if p.get("ring"):
            w = max(1, int(round(float(p["ring_width"]) * scale)))
            k = np.ones((2 * w + 1, 2 * w + 1), np.uint8)
            inner = cv2.erode(obj.astype(np.uint8), k)
            m = (obj.astype(np.uint8) - inner).astype(np.float32)
    elif kind == "brush":
        m = rasterize_strokes(defn.get("strokes") or [], (H, W))
    else:
        raise ValueError(f"Unknown mask type {kind}")
    grow = float(defn.get("grow", 0.0)) * scale
    if abs(grow) >= 1:
        r = int(abs(round(grow)))
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        m = cv2.dilate(m, k) if grow > 0 else cv2.erode(m, k)
    feather = float(defn.get("feather", 0.0)) * scale
    if feather > 0.3:
        m = cv2.GaussianBlur(m, (0, 0), feather)
    if defn.get("invert"):
        m = 1.0 - m
    return np.clip(m, 0, 1).astype(np.float32)


def rasterize_strokes(strokes: list[dict], shape: tuple[int, int]) -> np.ndarray:
    """Brush strokes use coordinates relative to the image (0..1), so they fit any resolution.

    stroke = {"points": [[x, y], ...], "radius": 0.02 (fraction of width), "hardness": 0..1,
              "mode": "add" | "erase", "strength": 0..1}
    """
    H, W = shape
    m = np.zeros((H, W), np.float32)
    for s in strokes:
        pts = np.array(s.get("points") or [], float)
        if len(pts) == 0:
            continue
        r = max(1, int(round(float(s.get("radius", 0.02)) * W)))
        layer = np.zeros((H, W), np.float32)
        xy = np.round(pts * [W, H]).astype(np.int32)
        if len(xy) == 1:
            cv2.circle(layer, tuple(int(v) for v in xy[0]), r, 1.0, -1, cv2.LINE_AA)
        else:
            cv2.polylines(layer, [xy.reshape(-1, 1, 2)], False, 1.0, 2 * r, cv2.LINE_AA)
            for pt in (xy[0], xy[-1]):
                cv2.circle(layer, tuple(int(v) for v in pt), r, 1.0, -1, cv2.LINE_AA)
        hard = float(np.clip(s.get("hardness", 0.5), 0, 1))
        if hard < 0.99:
            layer = cv2.GaussianBlur(layer, (0, 0), max(0.5, r * (1 - hard) * 0.6))
        strength = float(np.clip(s.get("strength", 1.0), 0, 1))
        if s.get("mode") == "erase":
            m = np.clip(m - layer * strength, 0, 1)
        else:
            m = np.maximum(m, layer * strength)
    return m


def apply_masked(original: np.ndarray, processed: np.ndarray, mask: np.ndarray | None, opacity: float = 1.0) -> np.ndarray:
    """Blend: processed where the mask is 1, original where it is 0."""
    if mask is None and opacity >= 0.999:
        return processed
    if original.shape != processed.shape:
        return processed  # geometry changed (crop/rotate): masks don't apply
    m = (np.ones(original.shape[:2], np.float32) if mask is None else mask) * float(opacity)
    if original.ndim == 3:
        m = m[..., None]
    return (original * (1 - m) + processed * m).astype(np.float32)
