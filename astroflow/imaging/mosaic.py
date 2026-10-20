"""Stitching several stacked panels into one mosaic (e.g. the whole Moon at high magnification).

Each panel is matched to its neighbours with SIFT features (patent-free since 2020,
included in OpenCV). Panels are placed relative to a central anchor panel,
brightness-matched in the overlaps, and blended with soft feathered seams.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Callable

import cv2
import numpy as np

log = logging.getLogger("astroflow.mosaic")


def _to8(img: np.ndarray) -> np.ndarray:
    g = img.mean(axis=2) if img.ndim == 3 else img
    lo, hi = np.percentile(g, 1), np.percentile(g, 99.7)
    return np.clip((g - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)


def stitch(panels: list[np.ndarray], model: str = "similarity", progress: Callable[[float, str], None] | None = None,
           min_matches: int = 25) -> dict:
    """Returns {"image", "placed", "transforms"}; panels that could not be matched are reported."""
    prog = progress or (lambda f, m=None: None)
    n = len(panels)
    if n < 2:
        raise ValueError("A mosaic needs at least two panels")
    sift = cv2.SIFT_create(nfeatures=4000)
    feats = []
    for i, p in enumerate(panels):
        kp, des = sift.detectAndCompute(_to8(p), None)
        feats.append((kp, des))
        prog(0.3 * (i + 1) / n, f"Finding features in panel {i + 1}/{n}")
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    pair: dict[tuple[int, int], tuple[np.ndarray, int]] = {}
    for i in range(n):
        for j in range(i + 1, n):
            ki, di = feats[i]
            kj, dj = feats[j]
            if di is None or dj is None or len(ki) < 10 or len(kj) < 10:
                continue
            m = matcher.knnMatch(di, dj, k=2)
            good = [a for a, b in (x for x in m if len(x) == 2) if a.distance < 0.75 * b.distance]
            if len(good) < min_matches:
                continue
            src = np.float32([ki[g.queryIdx].pt for g in good])
            dst = np.float32([kj[g.trainIdx].pt for g in good])
            if model == "homography":
                M, inl = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
            else:
                A, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
                M = None if A is None else np.vstack([A, [0, 0, 1]])
            if M is None or inl is None or int(inl.sum()) < min_matches:
                continue
            pair[(i, j)] = (M, int(inl.sum()))          # maps panel i coords -> panel j coords
            pair[(j, i)] = (np.linalg.inv(M), int(inl.sum()))
        prog(0.3 + 0.3 * (i + 1) / n, "Matching panels")
    # Anchor = the panel with the most connections; place others by breadth-first search.
    deg = [sum(v[1] for (a, b), v in pair.items() if a == i) for i in range(n)]
    anchor = int(np.argmax(deg))
    T: dict[int, np.ndarray] = {anchor: np.eye(3)}
    q = deque([anchor])
    while q:
        a = q.popleft()
        for (i, j), (M, _) in pair.items():
            if i == a and j not in T:
                # coords in j -> coords in a -> anchor
                T[j] = T[a] @ np.linalg.inv(M)
                q.append(j)
    placed = sorted(T)
    if len(placed) < 2:
        raise ValueError("Panels do not overlap enough to be stitched (need ~20% overlap)")
    # Canvas bounds
    corners = []
    for i in placed:
        h, w = panels[i].shape[:2]
        c = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], float) @ T[i].T
        corners.append(c[:, :2] / c[:, 2:3])
    allc = np.vstack(corners)
    x0, y0 = np.floor(allc.min(0)).astype(int)
    x1, y1 = np.ceil(allc.max(0)).astype(int)
    Wc, Hc = int(x1 - x0), int(y1 - y0)
    if Wc * Hc > 400e6:
        raise ValueError("Mosaic would be too large")
    shift = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]], float)
    C = 3 if panels[placed[0]].ndim == 3 else 1
    warped, weights = {}, {}
    for k, i in enumerate(placed):
        p = panels[i] if panels[i].ndim == 3 else panels[i][..., None]
        h, w = p.shape[:2]
        M = shift @ T[i]
        ones = np.ones((h, w), np.uint8)
        dist = cv2.distanceTransform(cv2.copyMakeBorder(ones, 1, 1, 1, 1, cv2.BORDER_CONSTANT, 0), cv2.DIST_L2, 5)[1:-1, 1:-1]
        wgt = cv2.warpPerspective(dist.astype(np.float32), M, (Wc, Hc), flags=cv2.INTER_LINEAR)
        im = np.stack([cv2.warpPerspective(p[..., c].astype(np.float32), M, (Wc, Hc), flags=cv2.INTER_LANCZOS4) for c in range(C)], -1)
        warped[i], weights[i] = im, wgt
        prog(0.6 + 0.3 * (k + 1) / len(placed), f"Placing panel {k + 1}/{len(placed)}")
    # Gain compensation: scale each panel so overlaps agree with the anchor's brightness.
    gains = {i: 1.0 for i in placed}
    for _ in range(3):
        for i in placed:
            if i == anchor:
                continue
            num = den = 0.0
            for j in placed:
                if j == i:
                    continue
                ov = (weights[i] > 2) & (weights[j] > 2)
                if ov.sum() < 500:
                    continue
                num += float(warped[j][ov].mean()) * gains[j]
                den += float(warped[i][ov].mean())
            if den > 0:
                gains[i] = num / den
    acc = np.zeros((Hc, Wc, C), np.float32)
    wsum = np.zeros((Hc, Wc), np.float32)
    for i in placed:
        acc += warped[i] * gains[i] * weights[i][..., None]
        wsum += weights[i]
    out = np.where(wsum[..., None] > 0, acc / np.maximum(wsum[..., None], 1e-6), 0)
    prog(1.0, f"Stitched {len(placed)} of {n} panels")
    return {"image": (out[..., 0] if C == 1 else out).astype(np.float32), "placed": placed,
            "missing": [i for i in range(n) if i not in T], "size": (Wc, Hc)}
