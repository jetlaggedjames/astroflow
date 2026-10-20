"""Deep-sky stacking pipeline.

Steps (each shown as progress in the Stack tab):
 1. Prepare calibration masters (build from raw frames, take from the
    calibration library, or skip).
 2. Pass A - calibrate every light, find and measure its stars.
 3. Pick the best frame as the reference and match every frame's stars to it
    (shift + rotation: needed on an alt-az mount).
 4. Pass B - calibrate again, debayer, warp onto the reference grid and store
    on disk (memory-mapped).
 5. Normalise backgrounds, weight frames by quality, and integrate with
    outlier rejection, stripe by stripe.
 6. Crop to the area covered by (nearly) all frames - field rotation leaves
    ragged edges.
"""
from __future__ import annotations

import logging
import math
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from ..config import temp_dir
from ..io.debayer import debayer
from ..io.images import read_image
from ..io.source import expand_paths, open_source
from .background import remove_gradient
from .calibrate import CalibrationLibrary, CalibrationSet, make_master
from .integrate import FrameStore, frame_stats, integrate_store, normalization
from .register import RegistrationError, Transform, match_stars, warp
from .stars import detect_stars, measure_summary

log = logging.getLogger("astroflow.dso")


@dataclass
class DSOParams:
    registration_model: str = "similarity"   # similarity | affine | homography | poly2
    interpolation: str = "lanczos"
    rejection: str = "auto"                  # auto | none | minmax | percentile | sigma | winsorized | linear_fit
    reject_low: float = 3.0
    reject_high: float = 3.0
    normalization: str = "additive_scaling"  # none | additive | additive_scaling
    weighting: str = "quality"               # none | quality | noise
    debayer_method: str = "ea"
    cosmetic: bool = True
    cosmetic_sigma: float = 0.0              # >0: also remove outliers found in each frame
    hot_pixel_sigma: float = 6.0
    per_frame_gradient: bool = False
    crop: bool = True
    crop_coverage: float = 0.9
    scale: float = 1.0                       # 1 or 2 ("drizzle" style up-scaling)
    max_stars: int = 400
    min_matches: int = 8
    reference: int = -1                      # index into all lights, -1 = automatic
    bayer_override: str = ""
    memory_budget: int = 2 * 1024**3
    threads: int = 0

    @classmethod
    def from_dict(cls, d: dict | None) -> "DSOParams":
        p = cls()
        for k, v in (d or {}).items():
            if hasattr(p, k) and v is not None:
                cur = getattr(p, k)
                setattr(p, k, bool(v) if isinstance(cur, bool) else type(cur)(v))
        return p


@dataclass
class Group:
    """One imaging session: its lights and the calibration to use for them."""
    lights: list[Path]
    bias: Any = None        # list of paths | master path | "auto" | None
    dark: Any = None
    flat: Any = None
    flatdark: Any = None
    exclude: list[int] = field(default_factory=list)
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Group":
        return cls(lights=expand_paths(d.get("lights", [])), bias=d.get("bias"), dark=d.get("dark"),
                   flat=d.get("flat"), flatdark=d.get("flatdark"), exclude=list(d.get("exclude", [])),
                   name=d.get("name", ""))


def estimate(n_frames: int, width: int, height: int, color: bool = True, scale: float = 1.0,
             threads: int = 4, free_disk: int | None = None) -> dict:
    """Rough time / memory / temporary-disk estimate shown before stacking."""
    ch = 3 if color else 1
    px = width * height
    out_px = px * scale * scale
    per_frame_s = (0.15 + px / 8e6 * 1.4) * (1.0 + 0.6 * (scale - 1)) / max(1, threads) * 2.2
    integ_s = out_px * ch * n_frames * 4e-8
    temp_bytes = int(out_px * ch * n_frames * 4)
    return {
        "frames": n_frames,
        "seconds": round(n_frames * per_frame_s * 2 + integ_s + 5, 0),
        "temp_disk_bytes": temp_bytes,
        "temp_disk_bytes_float16": temp_bytes // 2,
        "peak_memory_bytes": int(min(2 * 1024**3, out_px * ch * 4 * max(8, n_frames) * 0.2) + px * ch * 4 * threads * 6),
        "fits_on_disk": None if free_disk is None else bool(free_disk > temp_bytes // 2 * 1.1),
    }


def _resolve_master(spec: Any, kind: str, light_meta: dict, lib: CalibrationLibrary, bias: np.ndarray | None,
                    dark_for_flat: np.ndarray | None, budget: int, log_fn: Callable[[str], None],
                    progress: Callable[[float, str], None]) -> tuple[np.ndarray | None, dict | None]:
    if spec in (None, "", "none", False):
        return None, None
    if spec == "auto":
        entry, why = lib.find(kind, light_meta)
        if entry is None:
            log_fn(f"No matching master {kind} in the calibration library - skipping {kind}.")
            return None, None
        log_fn(f"Using library master {kind} from {entry['created'][:10]} ({why}).")
        return lib.load(entry), entry
    paths = expand_paths(spec if isinstance(spec, (list, tuple)) else [spec])
    if not paths:
        return None, None
    if len(paths) == 1:
        data, meta = read_image(paths[0])
        if str(meta.get("IMAGETYP", "")).lower().startswith("master") or data.dtype == np.float32:
            from ..io.images import to_float
            log_fn(f"Using master {kind}: {paths[0].name}")
            return (data if data.dtype == np.float32 else to_float(data)), {"path": str(paths[0])}
    log_fn(f"Building master {kind} from {len(paths)} frames")
    master, meta = make_master(paths, kind, bias=bias, dark=dark_for_flat, memory_budget=budget, progress=progress)
    entry = lib.add(kind, master, meta, len(paths), note="built while stacking")
    log_fn(f"Saved master {kind} to the calibration library")
    return master, entry


def _largest_rectangle(mask: np.ndarray) -> tuple[int, int, int, int]:
    """Largest axis-aligned rectangle of True cells: (y0, y1, x0, x1)."""
    H, W = mask.shape
    heights = np.zeros(W, int)
    best = (0, (0, H, 0, W))
    for y in range(H):
        heights = np.where(mask[y], heights + 1, 0)
        stack: list[int] = []
        for x in range(W + 1):
            h = heights[x] if x < W else 0
            start = x
            while stack and heights[stack[-1]] >= h:
                top = stack.pop()
                left = stack[-1] + 1 if stack else 0
                area = heights[top] * (x - left)
                if area > best[0]:
                    best = (area, (y - heights[top] + 1, y + 1, left, x))
            stack.append(x)
    return best[1]


def stack(groups: Sequence[Group | dict], params: DSOParams | dict | None = None,
          progress: Callable[[float, str], None] | None = None, frame_quality: dict[str, float] | None = None,
          check_cancel: Callable[[], None] | None = None) -> dict:
    p = params if isinstance(params, DSOParams) else DSOParams.from_dict(params)
    groups = [g if isinstance(g, Group) else Group.from_dict(g) for g in groups]
    prog = progress or (lambda f, m=None: None)
    check = check_cancel or (lambda: None)
    t0 = time.time()
    notes: list[str] = []
    _last = [0.0]

    def note(msg: str) -> None:
        notes.append(msg)
        log.info(msg)
        prog(_last[0], msg)

    def step(frac: float, msg: str | None = None) -> None:
        _last[0] = frac
        prog(frac, msg)
        check()

    lib = CalibrationLibrary()
    threads = p.threads or 4
    # ---- 1. calibration masters -------------------------------------------
    lights: list[tuple[int, Path]] = []
    cal_sets: list[CalibrationSet] = []
    bayers: list[str | None] = []
    for gi, g in enumerate(groups):
        if not g.lights:
            continue
        first = open_source([g.lights[0]], p.bayer_override or None)
        meta = dict(first.meta)
        meta.update({"_width": first.width, "_height": first.height})
        bayer = first.bayer
        first.close()
        sub = lambda f, m=None, gi=gi: step(0.02 + 0.08 * (gi + f) / len(groups), m)  # noqa: E731
        bias, _ = _resolve_master(g.bias, "bias", meta, lib, None, None, p.memory_budget, note, sub)
        flatdark, _ = _resolve_master(g.flatdark, "flatdark", meta, lib, None, None, p.memory_budget, note, sub)
        dark, dentry = _resolve_master(g.dark, "dark", meta, lib, None, None, p.memory_budget, note, sub)
        flat, _ = _resolve_master(g.flat, "flat", meta, lib, bias, flatdark, p.memory_budget, note, sub)
        dexp = (dentry or {}).get("exposure") if isinstance(dentry, dict) else None
        cal_sets.append(CalibrationSet(bias, dark, flat, dexp, p.hot_pixel_sigma))
        bayers.append(bayer)
        excl = set(g.exclude)
        for li, path in enumerate(g.lights):
            if li not in excl:
                lights.append((len(cal_sets) - 1, path))
    if not lights:
        raise ValueError("No light frames to stack")
    n = len(lights)
    step(0.1, f"Measuring stars in {n} light frames")

    def load_calibrated(k: int) -> tuple[np.ndarray, dict]:
        ci, path = lights[k]
        raw, meta = read_image(path)
        cs = cal_sets[ci]
        bayer = p.bayer_override or bayers[ci]
        exp = None
        try:
            exp = float(meta.get("EXPTIME") or meta.get("EXPOSURE") or 0) or None
        except (TypeError, ValueError):
            pass
        img = cs.apply(raw, exp, bayer if raw.ndim == 2 else None, p.cosmetic, p.cosmetic_sigma)
        if bayer and img.ndim == 2:
            img = debayer(img, bayer, p.debayer_method)
            img = np.clip(img, 0, None)
        if p.per_frame_gradient:
            img = remove_gradient(img, "poly", 2)
        return img.astype(np.float32), meta

    # ---- 2. pass A: stars + quality ----------------------------------------
    stars: list[Any] = [None] * n
    qual = np.zeros(n)
    shapes: list[tuple[int, int]] = [(0, 0)] * n
    info_rows: list[dict] = [{} for _ in range(n)]

    def pass_a(k: int) -> None:
        try:
            img, meta = load_calibrated(k)
            lum = img.mean(axis=2) if img.ndim == 3 else img
            st = detect_stars(lum, sigma=5.0, max_stars=p.max_stars)
            s = measure_summary(st)
            stars[k] = st
            shapes[k] = lum.shape
            fq = (frame_quality or {}).get(str(lights[k][1]))
            q = fq if fq is not None else (s["stars"] / (1.0 + (s["fwhm"] or 10.0)))
            qual[k] = q
            info_rows[k] = {"file": lights[k][1].name, "path": str(lights[k][1]), "stars": s["stars"],
                            "fwhm": s["fwhm"], "noise": s["noise"], "quality": float(q), "status": "measured"}
        except Exception as e:
            info_rows[k] = {"file": lights[k][1].name, "path": str(lights[k][1]), "status": f"unreadable: {e}"}
        _done[0] += 1
        step(0.1 + 0.25 * _done[0] / n, f"Measured {_done[0]}/{n} frames")

    _done = [0]
    with ThreadPoolExecutor(max_workers=threads) as ex:
        list(ex.map(pass_a, range(n)))

    usable = [k for k in range(n) if stars[k] is not None and len(stars[k]) >= p.min_matches]
    if not usable:
        raise ValueError("No frame had enough stars to align. Check focus, exposure or that these are deep-sky frames.")
    base_shape = shapes[usable[0]]
    usable = [k for k in usable if shapes[k] == base_shape]
    ref = p.reference if 0 <= p.reference < n and p.reference in usable else max(usable, key=lambda k: qual[k])
    note(f"Reference frame: {lights[ref][1].name} ({len(stars[ref])} stars)")

    # ---- 3. registration ----------------------------------------------------
    transforms: dict[int, Transform] = {}
    prev: Transform | None = None
    for c, k in enumerate(usable):
        try:
            if k == ref:
                transforms[k] = Transform.identity()
                transforms[k].matches = len(stars[ref])
            else:
                try:
                    transforms[k] = match_stars(stars[k], stars[ref], p.registration_model, guess=prev)
                except RegistrationError:
                    transforms[k] = match_stars(stars[k], stars[ref], p.registration_model)
                prev = transforms[k]
            tr = transforms[k]
            if tr.matches < p.min_matches:
                raise RegistrationError(f"only {tr.matches} matching stars")
            info_rows[k].update({"status": "aligned", "rotation_deg": round(tr.rotation_deg, 3),
                                 "shift": [round(v, 2) for v in tr.shift], "matches": tr.matches, "rms": round(tr.rms, 3)})
        except RegistrationError as e:
            transforms.pop(k, None)
            info_rows[k]["status"] = f"not aligned: {e}"
        step(0.35 + 0.05 * (c + 1) / len(usable), f"Aligned {c + 1}/{len(usable)} frames")
    good = [k for k in usable if k in transforms]
    if not good:
        raise ValueError("No frames could be aligned")
    dropped = n - len(good)
    if dropped:
        note(f"{dropped} frame(s) could not be used (see the frame table).")

    # ---- 4. pass B: warp to the reference grid, on disk -------------------
    H, W = base_shape
    sc = float(p.scale) if p.scale and p.scale > 1 else 1.0
    Ho, Wo = int(round(H * sc)), int(round(W * sc))
    color = any(b for b in bayers) or (load_calibrated(ref)[0].ndim == 3)
    C = 3 if color else 1
    need = Ho * Wo * C * len(good) * 4
    tdir = temp_dir()
    free = shutil.disk_usage(tdir).free
    dtype = np.float32 if free > need * 1.2 else np.float16
    if dtype == np.float16:
        if free < need * 0.6:
            raise OSError(f"Not enough free disk space for stacking: need about {need / 2 / 1e9:.1f} GB in {tdir}")
        note("Low disk space: storing aligned frames at half precision.")
    store = FrameStore(tdir / f"stack-{uuid.uuid4().hex}.npy", len(good), (Ho, Wo, C), dtype)
    S = np.diag([sc, sc, 1.0])
    stats: list[tuple[np.ndarray, np.ndarray]] = [None] * len(good)  # type: ignore[list-item]
    noise = np.zeros(len(good))

    def pass_b(j: int) -> None:
        k = good[j]
        img, _ = load_calibrated(k)
        tr = transforms[k]
        tr_s = Transform(S @ tr.matrix, tr.model, tr.poly * sc if tr.poly is not None else None)
        if img.ndim == 2:
            img = img[..., None]
        out = np.empty((Ho, Wo, C), np.float32)
        for c in range(C):
            out[..., c] = warp(img[..., min(c, img.shape[2] - 1)], tr_s, (Ho, Wo), p.interpolation)
        store[j] = out
        stats[j] = frame_stats(out)
        noise[j] = float(np.mean(stats[j][1]))
        _done[0] += 1
        step(0.4 + 0.35 * _done[0] / len(good), f"Aligned and resampled {_done[0]}/{len(good)} frames")

    _done[0] = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(threads, 3))) as ex:
            list(ex.map(pass_b, range(len(good))))
        store.flush()

        # ---- 5. normalise, weight, integrate ---------------------------------
        ref_j = good.index(ref)
        norm = normalization(stats, ref_j, p.normalization)
        if p.weighting == "none":
            weights = np.ones(len(good))
        else:
            sn = np.array([float(np.mean(norm[j][0])) for j in range(len(good))])
            w_noise = 1.0 / np.maximum(noise * sn, 1e-9) ** 2
            weights = w_noise / w_noise.max()
            if p.weighting == "quality":
                qg = np.array([qual[k] for k in good], float)
                weights = weights * np.clip(qg / max(qg.max(), 1e-9), 0.05, 1)
            weights = weights / weights.max()
        for j, k in enumerate(good):
            info_rows[k]["weight"] = round(float(weights[j]), 3)
        res = integrate_store(store, weights, p.rejection, p.reject_low, p.reject_high, norm,
                              memory_budget=p.memory_budget, progress=lambda f, m: step(0.75 + 0.2 * f, m))
    finally:
        store.close()

    img = res["image"]
    cov = res["coverage"]
    crop_box = (0, Ho, 0, Wo)
    if p.crop and len(good) > 1:
        need_n = max(1, int(math.ceil(len(good) * p.crop_coverage)))
        f = 8
        small = cov[::f, ::f] >= need_n
        y0, y1, x0, x1 = _largest_rectangle(small)
        crop_box = (min(Ho, (y0 + 1) * f), max(0, (y1 - 1) * f), min(Wo, (x0 + 1) * f), max(0, (x1 - 1) * f))
        if crop_box[1] - crop_box[0] > Ho * 0.3 and crop_box[3] - crop_box[2] > Wo * 0.3:
            img = img[crop_box[0]:crop_box[1], crop_box[2]:crop_box[3]]
            cov = cov[crop_box[0]:crop_box[1], crop_box[2]:crop_box[3]]
        else:
            crop_box = (0, Ho, 0, Wo)
            note("Frames overlap too little for auto-crop; keeping the full area.")
    img = np.nan_to_num(img, nan=float(np.nanmedian(img)))
    img = np.clip(img, 0, None)
    if not color:
        img = img[..., 0]
    rot = [abs(transforms[k].rotation_deg) for k in good]
    step(1.0, f"Stacked {len(good)} frames in {time.time() - t0:.0f}s")
    return {
        "image": img.astype(np.float32),
        "coverage": cov,
        "frames_used": len(good),
        "frames_total": n,
        "reference": str(lights[ref][1]),
        "rejection": res["method"],
        "rejected_percent": round(res["rejected_percent"], 3),
        "crop_box": crop_box,
        "max_field_rotation_deg": round(max(rot), 3) if rot else 0.0,
        "frames": info_rows,
        "notes": notes,
        "seconds": round(time.time() - t0, 1),
        "calibration": [c.describe() for c in cal_sets],
        "precision": "float32" if dtype == np.float32 else "float16",
    }
