"""Lucky imaging: stacking planetary and lunar videos.

How it works, in plain words:
 1. Every frame has already been scored for sharpness (Analyze tab).
 2. We keep the best frames, line them up as a whole ("global alignment"),
    and average the very best few into a clean *reference* image.
 3. The atmosphere doesn't just shift the whole picture - it bends small
    patches in different directions. So we place many small *alignment
    points* (APs) over the planet/Moon and measure, for every frame, how
    each patch is displaced compared with the reference.
 4. Each frame is then "un-warped" with a smooth correction field built from
    those AP shifts, and only the locally sharpest frames contribute in each
    patch (a frame can be sharp on one side of the Moon and blurry on the other).
 5. Optional: de-rotate fast-spinning Jupiter/Saturn/Mars to one moment in
    time, and drizzle/upsample to a finer output grid.

Everything streams from disk in two passes, so the input size doesn't matter.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

from ..io.source import FrameSource
from .quality import object_mask
from .register import template_shift, xcorr_shift

log = logging.getLogger("astroflow.lucky")

ROTATION_PERIOD_S = {"jupiter": 35740.6, "saturn": 38018.0, "mars": 88642.7}
FLATTENING = {"jupiter": 0.0649, "saturn": 0.098, "mars": 0.0059}


@dataclass
class LuckyParams:
    ap_size: int = 0                 # alignment-point box size in pixels (0 = automatic)
    ap_min_brightness: float = 0.15  # skip APs on dark sky (fraction of the brightest)
    ap_min_structure: float = 0.002  # skip featureless patches
    multipoint: bool = True
    local_keep_percent: float = 60.0  # within the kept frames, % used per AP
    upsample: float = 1.0            # 1, 1.5, 2, 3
    upsample_method: str = "interpolate"  # "interpolate" | "drizzle"
    drizzle_pixfrac: float = 0.7
    align_mode: str = "auto"         # "auto" | "disk" | "surface"
    debayer_method: str = "bilinear"
    normalize_brightness: bool = True
    derotate: bool = False
    planet: str = "jupiter"
    pole_angle_deg: float = 0.0      # position angle of the planet's north pole in the image
    rotation_dir: int = 1            # +1: features move toward +x when north is up
    reference_frames: int = 0        # 0 = automatic (~10% of kept, 10..60)
    reference_iterations: int = 1    # local refinement rounds (more rounds start to amplify noise)
    max_shift: float = 0.0           # max local AP shift accepted (0 = AP size / 4)
    threads: int = 0
    memory_budget: int = 2 * 1024**3

    @classmethod
    def from_dict(cls, d: dict | None) -> "LuckyParams":
        p = cls()
        for k, v in (d or {}).items():
            if hasattr(p, k) and v is not None:
                cur = getattr(p, k)
                setattr(p, k, type(cur)(v) if not isinstance(cur, bool) else bool(v))
        return p


@dataclass
class LuckyResult:
    image: np.ndarray
    reference: np.ndarray
    coverage: np.ndarray
    aps: list[tuple[int, int]]
    ap_size: int
    frames_used: int
    info: dict = field(default_factory=dict)


# ----------------------------------------------------------- helpers ---

def _gray(img: np.ndarray) -> np.ndarray:
    return img.mean(axis=2) if img.ndim == 3 else img


def _shift_image(img: np.ndarray, dx: float, dy: float) -> np.ndarray:
    M = np.float32([[1, 0, -dx], [0, 1, -dy]])
    return cv2.warpAffine(img, M, (img.shape[1], img.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def _centroid(gray: np.ndarray) -> tuple[float, float, np.ndarray]:
    mask, bg = object_mask(gray)
    w = np.clip(gray - bg, 0, None) * mask
    s = float(w.sum()) + 1e-12
    ys, xs = np.indices(gray.shape)
    return float((xs * w).sum() / s), float((ys * w).sum() / s), mask


def _phase(ref: np.ndarray, img: np.ndarray, win: np.ndarray) -> tuple[float, float, float]:
    return xcorr_shift(ref, img, win)


class Derotator:
    """Moves surface features of a rotating planet back to where they were at t_ref."""

    def __init__(self, shape: tuple[int, int], center: tuple[float, float], radius: float, planet: str,
                 pole_angle_deg: float, direction: int, limb_darkening: float = 0.45):
        h, w = shape
        self.shape = shape
        self.cx, self.cy = center
        self.R = radius
        self.f = FLATTENING.get(planet, 0.0)
        self.omega = 2 * math.pi / ROTATION_PERIOD_S.get(planet, 1e12) * (1 if direction >= 0 else -1)
        phi = math.radians(pole_angle_deg)
        self.e = np.array([math.cos(phi), math.sin(phi)], np.float32)   # equator direction (+lon)
        self.p = np.array([math.sin(phi), -math.cos(phi)], np.float32)  # pole direction
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        u = ((xx - self.cx) * self.e[0] + (yy - self.cy) * self.e[1]) / self.R
        v = ((xx - self.cx) * self.p[0] + (yy - self.cy) * self.p[1]) / (self.R * (1 - self.f))
        r2 = u * u + v * v
        self.inside = r2 < 0.995
        z = np.sqrt(np.clip(1 - r2, 0, 1))
        self.lat_cos = np.sqrt(np.clip(1 - v * v, 1e-6, 1))
        self.lon = np.arctan2(u, z)
        self.u = u
        self.v = v
        self.z = z
        self.k = limb_darkening
        # Fade the correction out at the limb where features run off the disk.
        self.fade = np.clip((0.995 - r2) / 0.05, 0, 1).astype(np.float32)

    def field(self, dt_s: float) -> tuple[np.ndarray, np.ndarray]:
        """Displacement (dx, dy) such that frame(x + d) shows what was at x at t_ref."""
        lon2 = np.clip(self.lon + self.omega * dt_s, -math.pi / 2, math.pi / 2)
        u2 = self.lat_cos * np.sin(lon2)
        du = (u2 - self.u) * self.R * self.inside * self.fade
        return (du * self.e[0]).astype(np.float32), (du * self.e[1]).astype(np.float32)

    def gain(self, dt_s: float) -> np.ndarray:
        """Brightness correction for limb darkening: a feature moved from the limb toward the
        centre (or back) looks brighter (or darker) at its new position."""
        if self.k <= 0:
            return np.ones(self.shape, np.float32)
        lon2 = np.clip(self.lon + self.omega * dt_s, -math.pi / 2, math.pi / 2)
        z2 = self.lat_cos * np.cos(lon2)
        g = np.where(self.inside, (np.maximum(self.z, 1e-3) / np.maximum(z2, 1e-3)) ** self.k, 1.0)
        g = 1.0 + (np.clip(g, 0.5, 2.0) - 1.0) * self.fade
        return g.astype(np.float32)


# -------------------------------------------------------- alignment points ---

class APModel:
    """A grid of alignment points over the reference image."""

    def __init__(self, ref_gray: np.ndarray, mask: np.ndarray, ap: int, p: "LuckyParams", enabled: bool = True):
        self.ap = ap
        self.max_shift = p.max_shift or max(3.0, ap / 4)
        self.margin = int(math.ceil(self.max_shift)) + 1
        if enabled:
            self.aps, self.grid_shape, self.origin, self.spacing = _place_aps(ref_gray, mask, ap, p, self.margin)
        else:
            self.aps, self.grid_shape, self.origin, self.spacing = [], (0, 0), (0, 0), ap
        self.rows = np.array([(y - self.origin[0]) // self.spacing for _, y in self.aps], int)
        self.cols = np.array([(x - self.origin[1]) // self.spacing for x, _ in self.aps], int)
        self.set_reference(ref_gray)

    def __len__(self) -> int:
        return len(self.aps)

    def set_reference(self, ref_gray: np.ndarray) -> None:
        ap = self.ap
        blur = cv2.GaussianBlur(ref_gray, (0, 0), 1.0)
        self.patches = [blur[y - ap // 2: y - ap // 2 + ap, x - ap // 2: x - ap // 2 + ap].copy() for x, y in self.aps]
        # Which directions each AP can measure (structure tensor). Along a direction
        # without detail (e.g. along Jupiter's belts) a measured shift is just noise.
        self.proj = np.zeros((len(self.aps), 2, 2), np.float32)
        for a, patch in enumerate(self.patches):
            gx = cv2.Sobel(patch, cv2.CV_32F, 1, 0)
            gy = cv2.Sobel(patch, cv2.CV_32F, 0, 1)
            J = np.array([[np.sum(gx * gx), np.sum(gx * gy)], [np.sum(gx * gy), np.sum(gy * gy)]], np.float64)
            evals, evecs = np.linalg.eigh(J)
            ratio = evals[0] / max(evals[1], 1e-30)
            trust_weak = float(np.clip((ratio - 0.04) / 0.2, 0, 1))
            v_weak, v_strong = evecs[:, 0], evecs[:, 1]
            self.proj[a] = np.outer(v_strong, v_strong) + trust_weak * np.outer(v_weak, v_weak)

    def measure(self, aligned_gray: np.ndarray):
        """Local shift (n,2), validity (n,) and sharpness (n,) of every AP in a globally aligned frame."""
        n, ap, m = len(self.aps), self.ap, self.margin
        shift = np.zeros((n, 2), np.float32)
        valid = np.zeros(n, bool)
        qual = np.zeros(n, np.float32)
        if n == 0:
            return shift, valid, qual
        b = cv2.GaussianBlur(aligned_gray, (0, 0), 1.0)
        for a, (x, y) in enumerate(self.aps):
            y0, x0 = y - ap // 2, x - ap // 2
            search = b[y0 - m: y0 + ap + m, x0 - m: x0 + ap + m]
            dx, dy, conf = template_shift(self.patches[a], search, m)
            ok = conf > 0.3 and abs(dx) <= self.max_shift and abs(dy) <= self.max_shift
            valid[a] = ok
            if ok:
                shift[a] = self.proj[a] @ np.array([dx, dy], np.float32)
            patch = b[y0: y0 + ap, x0: x0 + ap]
            mu = float(patch.mean()) + 1e-6
            qual[a] = float(cv2.Laplacian(patch, cv2.CV_32F).var()) / (mu * mu)
        _clean_ap_field(shift, valid, self.rows, self.cols, self.grid_shape)
        return shift, valid, qual

    def output_grid(self, xr: np.ndarray, yr: np.ndarray) -> "APGrid":
        return APGrid(self, xr, yr)


class APGrid:
    """Turns per-AP values into smooth full-resolution maps on an output grid."""

    def __init__(self, model: APModel, xr: np.ndarray, yr: np.ndarray):
        self.m = model
        if len(model):
            gy0, gx0 = model.origin
            self.gmx = ((xr - gx0) / model.spacing).astype(np.float32)
            self.gmy = ((yr - gy0) / model.spacing).astype(np.float32)
            cover = np.zeros(model.grid_shape, np.float32)
            cover[model.rows, model.cols] = 1.0
            self.coverage = np.clip(self._up(cover), 0, 1)
        else:
            self.coverage = np.zeros(xr.shape, np.float32)

    def _up(self, g: np.ndarray) -> np.ndarray:
        return cv2.remap(g, self.gmx, self.gmy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    def dense(self, values: np.ndarray, fill: float = 0.0) -> np.ndarray:
        g = np.full(self.m.grid_shape, fill, np.float32)
        g[self.m.rows, self.m.cols] = values
        return self._up(g)


# --------------------------------------------------------------- main ---

def stack(src: FrameSource, frames: list[int], params: LuckyParams | dict | None = None,
          progress: Callable[[float, str], None] | None = None,
          reference_image: np.ndarray | None = None, debug: dict | None = None) -> LuckyResult:
    """Stack ``frames`` (indices, best first) of ``src``.

    ``reference_image`` (optional) replaces the automatically built reference,
    e.g. a previous stack of the same target. ``debug`` (optional dict) receives
    internal arrays for testing.
    """
    p = params if isinstance(params, LuckyParams) else LuckyParams.from_dict(params)
    if not frames:
        raise ValueError("No frames selected for stacking")
    frames = list(frames)  # best first
    prog = progress or (lambda f, m=None: None)
    threads = p.threads or 4
    t0 = time.time()
    H, W = src.height, src.width
    color = src.is_color
    nF = len(frames)

    # Cache decoded frames in RAM when they fit the memory budget (saves a second read).
    frame_bytes = H * W * (3 if color else 1) * 4
    cache_ok = frame_bytes * nF < p.memory_budget * 0.6
    cache: dict[int, np.ndarray] = {}
    cache_lock = threading.Lock()

    def get(i: int) -> np.ndarray:
        if cache_ok:
            with cache_lock:
                if i in cache:
                    return cache[i]
        img = src.get_float(i, p.debayer_method)
        if cache_ok:
            with cache_lock:
                cache[i] = img
        return img

    # ---- 1. global alignment helpers ---------------------------------------
    prog(0.01, "Building the reference image from the sharpest frames")
    best_gray = _gray(get(frames[0])).astype(np.float32)
    bcx, bcy, bmask = _centroid(best_gray)
    disk = (np.count_nonzero(bmask) / bmask.size < 0.6) and not _touches_edge(bmask)
    mode = p.align_mode if p.align_mode != "auto" else ("disk" if disk else "surface")
    win = cv2.createHanningWindow((W, H), cv2.CV_32F)

    def global_shift(gray: np.ndarray, ref_g: np.ndarray, ref_c: tuple[float, float]) -> tuple[float, float]:
        if mode == "disk":
            cx, cy, _ = _centroid(gray)
            dx0, dy0 = cx - ref_c[0], cy - ref_c[1]
            ddx, ddy, _ = _phase(ref_g, _shift_image(gray, dx0, dy0), win)
            return dx0 + ddx, dy0 + ddy
        dx, dy, _ = _phase(ref_g, gray, win)
        return dx, dy

    # ---- 2. reference: global mean of the best frames, then refined locally --
    nref = min(p.reference_frames or int(np.clip(nF * 0.1, 10, 60)), nF)
    ref_frames = [get(i) for i in frames[:nref]]
    ref_shifts = [global_shift(_gray(im).astype(np.float32), best_gray, (bcx, bcy)) for im in ref_frames]
    reference = np.mean([_shift_image(im, dx, dy) for im, (dx, dy) in zip(ref_frames, ref_shifts)], axis=0).astype(np.float32)
    ref_gray = _gray(reference).astype(np.float32)
    rcx, rcy, rmask = _centroid(ref_gray)
    ap = p.ap_size or _auto_ap_size(rmask, mode, (H, W))
    model = APModel(ref_gray, rmask, ap, p, enabled=p.multipoint)
    if reference_image is not None and reference_image.shape[:2] == (H, W):
        ri = reference_image.astype(np.float32)
        reference = ri if ri.ndim == reference.ndim else (ri.mean(axis=2) if ri.ndim == 3 else np.repeat(ri[..., None], 3, axis=2))
        ref_gray = _gray(reference).astype(np.float32)
        model.set_reference(ref_gray)
    elif len(model) and nref >= 3:
        # Local refinement: un-warp each reference frame with its own AP shifts, re-average.
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        grid = model.output_grid(xx, yy)
        rounds = max(1, p.reference_iterations)
        for it in range(rounds):
            acc = np.zeros_like(reference)
            for k, im in enumerate(ref_frames):
                gray = _gray(im).astype(np.float32)
                dx, dy = global_shift(gray, ref_gray, (rcx, rcy))
                sh, ok, _ = model.measure(_shift_image(gray, dx, dy))
                mx = (xx + dx + grid.dense(sh[:, 0] * ok) * grid.coverage).astype(np.float32)
                my = (yy + dy + grid.dense(sh[:, 1] * ok) * grid.coverage).astype(np.float32)
                acc += cv2.remap(im, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
                prog(0.02 + 0.04 * (it * nref + k + 1) / (rounds * nref))
            reference = (acc / nref).astype(np.float32)
            ref_gray = _gray(reference).astype(np.float32)
            model.set_reference(ref_gray)
            rcx, rcy, rmask = _centroid(ref_gray)
    del ref_frames

    # ---- 3. frame times for de-rotation -----------------------------------
    derot = None
    times = None
    if p.derotate:
        ts = [src.frame_time(i) for i in frames]
        times = np.array([t.timestamp() if t else float("nan") for t in ts])
        if np.all(np.isfinite(times)) and mode == "disk":
            times = times - float(np.median(times))
            derot = Derotator((H, W), (rcx, rcy), _disk_radius(rmask), p.planet, p.pole_angle_deg, p.rotation_dir)
        else:
            log.warning("De-rotation skipped: frames have no time stamps or the planet disk was not found")

    # ---- 4. pass 1: measure every frame ------------------------------------
    nA = len(model)
    gshift = np.zeros((nF, 2), np.float32)
    lshift = np.zeros((nF, nA, 2), np.float32)
    lvalid = np.zeros((nF, nA), bool)
    lqual = np.zeros((nF, nA), np.float32)
    bright = np.ones(nF, np.float32)
    ref_mean = float(np.mean(ref_gray[rmask])) if np.any(rmask) else float(np.mean(ref_gray))
    if derot is not None:
        dgy, dgx = np.mgrid[0:H, 0:W].astype(np.float32)
    prog(0.06, f"Measuring {nA} alignment points in {nF:,} frames")

    def pass1(k: int) -> None:
        gray = _gray(get(frames[k])).astype(np.float32)
        dx, dy = global_shift(gray, ref_gray, (rcx, rcy))
        gshift[k] = (dx, dy)
        if p.normalize_brightness:
            m = gray[rmask].mean() if np.any(rmask) else gray.mean()
            bright[k] = ref_mean / max(float(m), 1e-6)
        if nA == 0:
            return
        if derot is not None:
            fx, fy = derot.field(float(times[k]))
            aligned = cv2.remap(gray, (dgx + fx + dx).astype(np.float32), (dgy + fy + dy).astype(np.float32),
                                cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        else:
            aligned = _shift_image(gray, dx, dy)
        lshift[k], lvalid[k], lqual[k] = model.measure(aligned)

    _parallel(pass1, nF, threads, lambda d: prog(0.06 + 0.44 * d / nF, f"Aligning frames: {d:,}/{nF:,}"))

    # ---- 5. local frame selection -----------------------------------------
    weights = np.ones((nF, nA), np.float32)
    if nA:
        keep_n = max(1, int(round(nF * p.local_keep_percent / 100)))
        q = np.where(lvalid, lqual, -1)
        rank = np.argsort(np.argsort(-q, axis=0), axis=0)
        weights = (rank < keep_n).astype(np.float32) * lvalid
        empty = weights.sum(axis=0) == 0
        weights[:, empty] = 1.0

    # ---- 6. pass 2: warp and accumulate -----------------------------------
    scale = float(p.upsample) if p.upsample and p.upsample > 1 else 1.0
    Ho, Wo = int(round(H * scale)), int(round(W * scale))
    yo, xo = np.mgrid[0:Ho, 0:Wo].astype(np.float32)
    xr = (xo + 0.5) / scale - 0.5          # output pixel centre -> reference coordinates
    yr = (yo + 0.5) / scale - 0.5
    grid = model.output_grid(xr, yr)
    ch = 3 if color else 1
    nacc = max(1, min(threads, int(p.memory_budget * 0.3 // max(1, Ho * Wo * ch * 4 * 4))))
    accs = [{"num": np.zeros((Ho, Wo, ch), np.float32), "den": np.zeros((Ho, Wo), np.float32),
             "gnum": np.zeros((Ho, Wo, ch), np.float32), "lock": threading.Lock()} for _ in range(nacc)]
    prog(0.5, "Stacking")

    def pass2(k: int) -> None:
        img = get(frames[k])
        if img.ndim == 2:
            img = img[..., None]
        dx, dy = gshift[k]
        mx = xr + dx
        my = yr + dy
        if derot is not None:
            fx, fy = derot.field(float(times[k]))
            if scale != 1.0:
                fx = cv2.resize(fx, (Wo, Ho), interpolation=cv2.INTER_LINEAR)
                fy = cv2.resize(fy, (Wo, Ho), interpolation=cv2.INTER_LINEAR)
            mx = mx + fx
            my = my + fy
            dgain = derot.gain(float(times[k]))
            if scale != 1.0:
                dgain = cv2.resize(dgain, (Wo, Ho), interpolation=cv2.INTER_LINEAR)
        else:
            dgain = None
        wmap = None
        if nA:
            mx = mx + grid.dense(lshift[k, :, 0] * lvalid[k]) * grid.coverage
            my = my + grid.dense(lshift[k, :, 1] * lvalid[k]) * grid.coverage
            wmap = grid.dense(weights[k], 0.0) * grid.coverage + (1 - grid.coverage)
        mx = mx.astype(np.float32)
        my = my.astype(np.float32)
        if p.upsample_method == "drizzle" and scale > 1:
            warped = _drizzle_frame(img, mx, my, scale, p.drizzle_pixfrac)
        else:
            warped = cv2.remap(img, mx, my, cv2.INTER_CUBIC if scale > 1 else cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REFLECT)
            if warped.ndim == 2:
                warped = warped[..., None]
        warped *= bright[k]
        if dgain is not None:
            warped *= dgain[..., None]
        acc = accs[k % nacc]
        with acc["lock"]:
            if wmap is None:
                acc["num"] += warped
                acc["den"] += 1.0
            else:
                acc["num"] += warped * wmap[..., None]
                acc["den"] += wmap
            acc["gnum"] += warped

    _parallel(pass2, nF, threads, lambda d: prog(0.5 + 0.48 * d / nF, f"Stacking: {d:,}/{nF:,} frames"))
    num = sum(a["num"] for a in accs)
    den = sum(a["den"] for a in accs)
    gmean = sum(a["gnum"] for a in accs) / nF
    out = np.where(den[..., None] > 0.5, num / np.maximum(den[..., None], 1e-6), gmean)
    # Smooth transition where only a few frames contributed.
    blend = np.clip(den / max(1.0, nF * p.local_keep_percent / 100 * 0.25), 0, 1)[..., None]
    out = out * blend + gmean * (1 - blend)
    out = out[..., 0] if not color else out
    cache.clear()
    if debug is not None:
        debug.update({"gshift": gshift, "lshift": lshift, "lvalid": lvalid, "lqual": lqual, "aps": model.aps,
                      "weights": weights, "ap_size": ap})
    info = {
        "mode": mode, "aps": nA, "ap_size": ap, "frames": nF, "reference_frames": nref,
        "upsample": scale, "derotated": derot is not None, "seconds": round(time.time() - t0, 1),
        "mean_global_shift_px": float(np.mean(np.hypot(gshift[:, 0], gshift[:, 1]))),
        "mean_local_shift_px": float(np.mean(np.hypot(lshift[..., 0], lshift[..., 1])[lvalid])) if nA and lvalid.any() else 0.0,
    }
    prog(1.0, f"Stacked {nF:,} frames with {nA} alignment points in {info['seconds']}s")
    return LuckyResult(np.clip(out, 0, None).astype(np.float32), reference, den, model.aps, ap, nF, info)


def _parallel(fn, n: int, threads: int, on_progress) -> None:
    done = 0
    lock = threading.Lock()
    last = [0.0]

    def run(k):
        nonlocal done
        fn(k)
        with lock:
            done += 1
            if time.time() - last[0] > 0.25 or done == n:
                last[0] = time.time()
                on_progress(done)

    with ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
        # Consume results so exceptions propagate.
        for _ in ex.map(run, range(n)):
            pass


def _clean_ap_field(shift: np.ndarray, valid: np.ndarray, rows: np.ndarray, cols: np.ndarray,
                    grid_shape: tuple[int, int], tol: float = 1.5) -> None:
    """Replace AP shifts that disagree with their neighbours (in place)."""
    if len(rows) == 0:
        return
    gx = np.full(grid_shape, np.nan, np.float32)
    gy = np.full(grid_shape, np.nan, np.float32)
    gx[rows[valid], cols[valid]] = shift[valid, 0]
    gy[rows[valid], cols[valid]] = shift[valid, 1]
    H, W = grid_shape
    for a in range(len(rows)):
        r, c = rows[a], cols[a]
        nx = gx[max(r - 1, 0): r + 2, max(c - 1, 0): c + 2]
        ny = gy[max(r - 1, 0): r + 2, max(c - 1, 0): c + 2]
        if np.count_nonzero(np.isfinite(nx)) < 3:
            continue
        mx, my = float(np.nanmedian(nx)), float(np.nanmedian(ny))
        if not valid[a] or abs(shift[a, 0] - mx) > tol or abs(shift[a, 1] - my) > tol:
            shift[a] = (mx, my)
            valid[a] = True


def _touches_edge(mask: np.ndarray) -> bool:
    return bool(mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any())


def _disk_radius(mask: np.ndarray) -> float:
    return math.sqrt(max(np.count_nonzero(mask), 1) / math.pi)


def _auto_ap_size(mask: np.ndarray, mode: str, shape: tuple[int, int]) -> int:
    if mode == "disk":
        d = 2 * _disk_radius(mask)
        size = int(np.clip(d / 6, 32, 96))
    else:
        size = int(np.clip(min(shape) / 10, 32, 128))
    return size + (size % 2)


def _place_aps(ref_gray: np.ndarray, mask: np.ndarray, ap: int, p: LuckyParams, margin: int):
    H, W = ref_gray.shape
    spacing = max(8, ap // 2)
    half = ap // 2
    y0 = half + margin
    x0 = half + margin
    ys = list(range(y0, H - half - margin, spacing))
    xs = list(range(x0, W - half - margin, spacing))
    hi = float(np.percentile(ref_gray, 99.5))
    aps = []
    for y in ys:
        for x in xs:
            patch = ref_gray[y - half: y - half + ap, x - half: x - half + ap]
            if patch.shape != (ap, ap):
                continue
            if patch.mean() < p.ap_min_brightness * hi:
                continue
            mu = patch.mean() + 1e-6
            if patch.std() / mu < p.ap_min_structure * 10 and np.std(cv2.Laplacian(patch, cv2.CV_32F)) / mu < p.ap_min_structure:
                continue
            aps.append((x, y))
    return aps, (len(ys), len(xs)), (y0, x0), spacing


def _drizzle_frame(img: np.ndarray, mx: np.ndarray, my: np.ndarray, scale: float, pixfrac: float) -> np.ndarray:
    """Approximate drizzle: sample with a reduced (pixfrac) footprint instead of smooth interpolation.

    Each output pixel takes the input sample under its centre using a sharp
    kernel; with many dithered frames this recovers finer detail than plain
    interpolation (and is noisier with few frames, as real drizzle is).
    """
    # A sharper-than-bilinear kernel: nearest neighbour blended with linear by pixfrac.
    near = cv2.remap(img, mx, my, cv2.INTER_NEAREST, borderMode=cv2.BORDER_REFLECT)
    lin = cv2.remap(img, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    out = near * (1 - pixfrac) + lin * pixfrac
    return out if out.ndim == 3 else out[..., None]


def mean_quality_gain(frames_quality: list[float]) -> float:
    return float(np.mean(frames_quality)) if frames_quality else 0.0
