"""Lining up deep-sky frames by matching star patterns.

On an alt-az mount the sky slowly turns relative to the camera (field
rotation), so frames differ by a shift AND a rotation. We find the same
star triangles in both frames (triangle shapes don't change when the
picture is shifted, rotated or scaled), then fit the exact transform.

Models:
  * "similarity" - shift + rotation + scale (default; right for alt-az)
  * "affine"     - also allows shear
  * "homography" - perspective; tolerates mild distortion
  * "poly2"      - similarity followed by a smooth 2nd-order distortion fit
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

import cv2
import numpy as np
from scipy.spatial import cKDTree

from .stars import StarList


@dataclass
class Transform:
    """Maps coordinates of the *moving* frame onto the *reference* frame."""
    matrix: np.ndarray                      # 3x3
    model: str = "similarity"
    poly: np.ndarray | None = None          # (2, 6) residual polynomial coefficients for poly2
    matches: int = 0
    rms: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def rotation_deg(self) -> float:
        return float(np.degrees(np.arctan2(self.matrix[1, 0], self.matrix[0, 0])))

    @property
    def scale(self) -> float:
        return float(np.sqrt(abs(np.linalg.det(self.matrix[:2, :2]))))

    @property
    def shift(self) -> tuple[float, float]:
        return float(self.matrix[0, 2]), float(self.matrix[1, 2])

    def apply(self, xy: np.ndarray) -> np.ndarray:
        pts = np.column_stack([xy, np.ones(len(xy))]) @ self.matrix.T
        out = pts[:, :2] / pts[:, 2:3]
        if self.poly is not None:
            out = out + _poly_terms(out) @ self.poly.T
        return out

    def to_dict(self) -> dict:
        return {"matrix": self.matrix.tolist(), "model": self.model, "matches": self.matches, "rms": self.rms,
                "rotation_deg": self.rotation_deg, "scale": self.scale, "shift": self.shift,
                "poly": self.poly.tolist() if self.poly is not None else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Transform":
        return cls(np.array(d["matrix"], float), d.get("model", "similarity"),
                   np.array(d["poly"]) if d.get("poly") is not None else None, d.get("matches", 0), d.get("rms", 0.0))

    @classmethod
    def identity(cls) -> "Transform":
        return cls(np.eye(3))


class RegistrationError(RuntimeError):
    pass


def _poly_terms(xy: np.ndarray) -> np.ndarray:
    # Normalised coordinates keep the fit well-conditioned.
    x, y = xy[:, 0] / 1000.0, xy[:, 1] / 1000.0
    return np.column_stack([np.ones_like(x), x, y, x * x, x * y, y * y])


def _triangles(xy: np.ndarray, k: int = 6) -> tuple[np.ndarray, np.ndarray]:
    """Triangle invariants from each star and its nearest neighbours.

    Returns (invariants (M,2), vertex indices (M,3) ordered consistently).
    """
    n = len(xy)
    if n < 3:
        return np.zeros((0, 2)), np.zeros((0, 3), int)
    tree = cKDTree(xy)
    kk = min(k + 1, n)
    _, nn = tree.query(xy, kk)
    tris = set()
    for i in range(n):
        for a, b in combinations(nn[i][1:], 2):
            tris.add(tuple(sorted((i, int(a), int(b)))))
    tri = np.array(sorted(tris), int)
    p = xy[tri]  # (M,3,2)
    # Side opposite each vertex.
    s = np.stack([np.linalg.norm(p[:, 1] - p[:, 2], axis=1),
                  np.linalg.norm(p[:, 0] - p[:, 2], axis=1),
                  np.linalg.norm(p[:, 0] - p[:, 1], axis=1)], axis=1)
    order = np.argsort(s, axis=1)  # vertex opposite the shortest side first
    s_sorted = np.take_along_axis(s, order, 1)
    valid = s_sorted[:, 0] > 2.0  # ignore degenerate (tiny) triangles
    inv = np.column_stack([s_sorted[:, 2] / np.maximum(s_sorted[:, 1], 1e-9),
                           s_sorted[:, 1] / np.maximum(s_sorted[:, 0], 1e-9)])
    verts = np.take_along_axis(tri, order, 1)
    keep = valid & (inv[:, 1] < 10)
    return inv[keep], verts[keep]


def _fit(model: str, src: np.ndarray, dst: np.ndarray, ransac: bool, thresh: float = 2.0):
    if model == "homography" and len(src) >= 4:
        H, inl = cv2.findHomography(src, dst, cv2.RANSAC if ransac else 0, thresh)
        return H, inl
    if model == "affine" and len(src) >= 3:
        A, inl = cv2.estimateAffine2D(src, dst, method=cv2.RANSAC if ransac else cv2.LMEDS,
                                      ransacReprojThreshold=thresh, maxIters=4000, confidence=0.999)
    else:
        A, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC if ransac else cv2.LMEDS,
                                             ransacReprojThreshold=thresh, maxIters=4000, confidence=0.999)
    if A is None:
        return None, None
    return np.vstack([A, [0, 0, 1]]), inl


def match_stars(moving: StarList | np.ndarray, reference: StarList | np.ndarray, model: str = "similarity",
                n_stars: int = 60, guess: Transform | None = None, tolerance: float = 3.0,
                allow_scale: bool = True) -> Transform:
    """Find the transform that maps ``moving`` star positions onto ``reference``."""
    mv = moving.xy() if isinstance(moving, StarList) else np.asarray(moving, float)
    rf = reference.xy() if isinstance(reference, StarList) else np.asarray(reference, float)
    if len(mv) < 3 or len(rf) < 3:
        raise RegistrationError("Not enough stars to align (need at least 3)")
    mv_b, rf_b = mv[:n_stars], rf[:n_stars]
    base_model = "similarity" if model in ("similarity", "poly2") else model

    T0 = None
    if guess is not None:
        # Fast path: refine around a known transform (e.g. the previous frame's).
        T0 = _refine(guess.matrix, mv, rf, base_model, tolerance * 2)
    if T0 is None:
        T0 = _triangle_match(mv_b, rf_b, base_model)
        if T0 is None and len(mv) > n_stars:
            T0 = _triangle_match(mv[: n_stars * 2], rf[: n_stars * 2], base_model)
    if T0 is None:
        raise RegistrationError("Could not match the star pattern to the reference frame")
    M, inl_src, inl_dst = _refine_full(T0, mv, rf, base_model, tolerance)
    if M is None or len(inl_src) < 3:
        raise RegistrationError("Star match was too weak to trust")
    if not allow_scale and base_model == "similarity":
        sc = np.sqrt(abs(np.linalg.det(M[:2, :2])))
        M[:2, :2] /= sc
    tr = Transform(M, model, None, len(inl_src))
    pred = tr.apply(inl_src)
    if model == "poly2" and len(inl_src) >= 12:
        resid = inl_dst - pred
        terms = _poly_terms(pred)
        coef, *_ = np.linalg.lstsq(terms, resid, rcond=None)
        tr.poly = coef.T
        pred = tr.apply(inl_src)
    tr.rms = float(np.sqrt(np.mean(np.sum((pred - inl_dst) ** 2, axis=1))))
    sc = tr.scale
    if not (0.5 < sc < 2.0):
        raise RegistrationError(f"Implausible scale change ({sc:.2f}x) - wrong match")
    return tr


def _triangle_match(mv: np.ndarray, rf: np.ndarray, model: str) -> np.ndarray | None:
    inv_m, ver_m = _triangles(mv)
    inv_r, ver_r = _triangles(rf)
    if len(inv_m) == 0 or len(inv_r) == 0:
        return None
    tree = cKDTree(inv_r)
    d, j = tree.query(inv_m, 1, distance_upper_bound=0.02)
    ok = np.isfinite(d)
    if np.count_nonzero(ok) < 1:
        return None
    # Vote for star correspondences implied by matched triangles.
    votes: dict[tuple[int, int], int] = {}
    for vm, vr in zip(ver_m[ok], ver_r[j[ok]]):
        for a, b in zip(vm, vr):
            votes[(int(a), int(b))] = votes.get((int(a), int(b)), 0) + 1
    pairs = sorted(votes.items(), key=lambda kv: -kv[1])
    used_m, used_r, src, dst = set(), set(), [], []
    for (a, b), v in pairs:
        if a in used_m or b in used_r:
            continue
        used_m.add(a); used_r.add(b)
        src.append(mv[a]); dst.append(rf[b])
    if len(src) < 3:
        return None
    src_a, dst_a = np.array(src, np.float64), np.array(dst, np.float64)
    M, inl = _fit(model, src_a, dst_a, ransac=True, thresh=3.0)
    if M is None or inl is None or int(inl.sum()) < 3:
        return None
    return M


def _refine(M: np.ndarray, mv: np.ndarray, rf: np.ndarray, model: str, tol: float) -> np.ndarray | None:
    M2, s, _ = _refine_full(M, mv, rf, model, tol)
    if M2 is None or len(s) < max(4, min(len(mv), len(rf)) // 4):
        return None
    return M2


def _refine_full(M: np.ndarray, mv: np.ndarray, rf: np.ndarray, model: str, tol: float):
    tree = cKDTree(rf)
    for t in (tol * 2, tol, tol * 0.6):
        pts = np.column_stack([mv, np.ones(len(mv))]) @ M.T
        pts = pts[:, :2] / pts[:, 2:3]
        d, j = tree.query(pts, 1, distance_upper_bound=t)
        ok = np.isfinite(d)
        # one-to-one: keep the closest moving star per reference star
        if np.count_nonzero(ok) < 3:
            return None, [], []
        jj, dd, ii = j[ok], d[ok], np.nonzero(ok)[0]
        order = np.argsort(dd)
        seen, sel = set(), []
        for o in order:
            if jj[o] not in seen:
                seen.add(jj[o]); sel.append(o)
        src, dst = mv[ii[sel]], rf[jj[sel]]
        M_new, inl = _fit(model, src, dst, ransac=len(src) > 6, thresh=max(1.0, t / 2))
        if M_new is None:
            return None, [], []
        M = M_new
        if inl is not None:
            m = inl.ravel().astype(bool)
            src, dst = src[m], dst[m]
    return M, src, dst


def warp(img: np.ndarray, tr: Transform, out_shape: tuple[int, int], interp: str = "lanczos",
         border_value: float = np.nan) -> np.ndarray:
    """Resample ``img`` (moving frame) onto the reference grid. Uncovered pixels -> NaN."""
    flags = {"nearest": cv2.INTER_NEAREST, "bilinear": cv2.INTER_LINEAR, "bicubic": cv2.INTER_CUBIC,
             "lanczos": cv2.INTER_LANCZOS4}.get(interp, cv2.INTER_LANCZOS4)
    h, w = out_shape
    src = img.astype(np.float32, copy=False)
    if tr.poly is None:
        Minv = np.linalg.inv(tr.matrix)
        out = cv2.warpPerspective(src, Minv, (w, h), flags=flags | cv2.WARP_FILL_OUTLIERS,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=border_value)
    else:
        # Invert the polynomial numerically: for each reference pixel find the moving pixel.
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        ref = np.column_stack([xx.ravel(), yy.ravel()]).astype(np.float64)
        approx = ref - _poly_terms(ref) @ tr.poly.T
        for _ in range(2):
            fwd = approx + _poly_terms(approx) @ tr.poly.T
            approx = approx - (fwd - ref)
        Minv = np.linalg.inv(tr.matrix)
        pts = np.column_stack([approx, np.ones(len(approx))]) @ Minv.T
        pts = pts[:, :2] / pts[:, 2:3]
        mx = pts[:, 0].reshape(h, w).astype(np.float32)
        my = pts[:, 1].reshape(h, w).astype(np.float32)
        out = cv2.remap(src, mx, my, flags, borderMode=cv2.BORDER_CONSTANT, borderValue=border_value)
    if flags == cv2.INTER_LANCZOS4 or flags == cv2.INTER_CUBIC:
        # Ringing guard: Lanczos can undershoot around bright stars.
        lo = float(np.nanmin(src))
        np.maximum(out, lo, out=out, where=~np.isnan(out))
    return out


_FREQ_CACHE: dict[tuple, np.ndarray] = {}


def _freq_weight(shape: tuple[int, int], lowpass: float) -> np.ndarray:
    key = (shape, round(lowpass, 4))
    w = _FREQ_CACHE.get(key)
    if w is None:
        fy = np.fft.fftfreq(shape[0])[:, None]
        fx = np.fft.rfftfreq(shape[1])[None, :]
        w = np.exp(-(fx * fx + fy * fy) / (2 * lowpass * lowpass)).astype(np.float32)
        if len(_FREQ_CACHE) > 64:
            _FREQ_CACHE.clear()
        _FREQ_CACHE[key] = w
    return w


def xcorr_shift(ref: np.ndarray, img: np.ndarray, window: np.ndarray | None = None,
                whiten: float = 0.6, lowpass: float = 0.12) -> tuple[float, float, float]:
    """Sub-pixel translation (dx, dy) of ``img`` relative to ``ref``, plus a 0..1 confidence.

    If ``img`` shows the same scene moved by +d, the result is +d. This is a
    noise-robust cross-correlation: the spectrum is only partly whitened
    (pure phase correlation lets noise dominate) and high frequencies are
    down-weighted.
    """
    a = ref.astype(np.float32, copy=False)
    b = img.astype(np.float32, copy=False)
    h, w = a.shape
    if window is None:
        window = cv2.createHanningWindow((w, h), cv2.CV_32F)
    A = np.fft.rfft2((a - a.mean()) * window)
    B = np.fft.rfft2((b - b.mean()) * window)
    R = B * np.conj(A)
    mag = np.abs(R)
    R = R / (mag ** whiten + 1e-12 * (mag.max() + 1e-30)) * _freq_weight((h, w), lowpass)
    c = np.fft.irfft2(R, s=(h, w))
    iy, ix = np.unravel_index(int(np.argmax(c)), c.shape)
    peak = c[iy, ix]

    def sub(cm, c0, cp):
        den = cm - 2 * c0 + cp
        return 0.0 if abs(den) < 1e-20 else float(np.clip(0.5 * (cm - cp) / den, -0.5, 0.5))

    dx = ix + sub(c[iy, (ix - 1) % w], peak, c[iy, (ix + 1) % w])
    dy = iy + sub(c[(iy - 1) % h, ix], peak, c[(iy + 1) % h, ix])
    if dx > w / 2:
        dx -= w
    if dy > h / 2:
        dy -= h
    # Confidence: how much the peak stands out from the rest of the correlation surface.
    conf = float(np.clip((peak - c.mean()) / (c.std() * 8 + 1e-30), 0, 1))
    return float(dx), float(dy), conf


def refine_shift(ref: np.ndarray, img: np.ndarray, window: np.ndarray | None = None,
                 iterations: int = 2, **kw) -> tuple[float, float, float]:
    """xcorr_shift, then re-measure after undoing the shift (removes window bias)."""
    dx, dy, conf = xcorr_shift(ref, img, window, **kw)
    h, w = img.shape
    for _ in range(iterations):
        M = np.float32([[1, 0, -dx], [0, 1, -dy]])
        moved = cv2.warpAffine(img.astype(np.float32, copy=False), M, (w, h), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REFLECT)
        ex, ey, conf = xcorr_shift(ref, moved, window, **kw)
        dx, dy = dx + ex, dy + ey
        if abs(ex) < 0.02 and abs(ey) < 0.02:
            break
    return dx, dy, conf


def template_shift(template: np.ndarray, search: np.ndarray, margin: int) -> tuple[float, float, float]:
    """Where does ``template`` sit inside ``search`` (which is template-sized + ``margin`` on every side)?

    Returns the sub-pixel offset (dx, dy) of the matching content relative to the
    centred position, and the normalised correlation (0..1) as confidence.
    """
    res = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    _, peak, _, (ix, iy) = cv2.minMaxLoc(res)
    h, w = res.shape

    def sub(cm, c0, cp):
        den = cm - 2 * c0 + cp
        return 0.0 if abs(den) < 1e-12 else float(np.clip(0.5 * (cm - cp) / den, -0.5, 0.5))

    fx = sub(res[iy, ix - 1], peak, res[iy, ix + 1]) if 0 < ix < w - 1 else 0.0
    fy = sub(res[iy - 1, ix], peak, res[iy + 1, ix]) if 0 < iy < h - 1 else 0.0
    edge = ix in (0, w - 1) or iy in (0, h - 1)
    return float(ix + fx - margin), float(iy + fy - margin), float(peak) * (0.0 if edge else 1.0)


def phase_shift(ref: np.ndarray, img: np.ndarray, window: np.ndarray | None = None) -> tuple[float, float, float]:
    """Backward-compatible name for :func:`xcorr_shift`."""
    return xcorr_shift(ref, img, window)
