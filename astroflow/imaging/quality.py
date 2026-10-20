"""Scoring every frame of a recording, and spotting problems.

Planetary / lunar frames are scored on sharpness (how much fine detail the
atmosphere let through in that instant). Deep-sky frames are scored on their
stars (how many, how small, how round) and their sky background.

Everything streams: frames are read one at a time on several threads, so
thousands of frames per minute can be scored without loading the file.
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import cv2
import numpy as np

from ..io.source import FrameSource
from .stars import detect_stars, measure_summary

log = logging.getLogger("astroflow.quality")

PLANETARY_METRICS = ["quality", "sharpness", "gradient", "brightness", "contrast", "noise", "cx", "cy", "clipped", "area"]
DSO_METRICS = ["quality", "stars", "fwhm", "eccentricity", "background", "noise", "snr", "trail"]


# --------------------------------------------------------- per-frame work ---

def _scoring_view(src: FrameSource, i: int, max_width: int = 1200) -> tuple[np.ndarray, float]:
    """A mono float view for scoring; downscaled for very wide frames. Returns (img, scale)."""
    img = src.get_mono(i, half=bool(src.bayer))
    scale = 2.0 if src.bayer else 1.0
    while img.shape[1] > max_width:
        img = cv2.resize(img, (img.shape[1] // 2, img.shape[0] // 2), interpolation=cv2.INTER_AREA)
        scale *= 2
    return img, scale


def object_mask(img: np.ndarray) -> tuple[np.ndarray, float]:
    """Where the planet/Moon is: pixels clearly above the sky background."""
    blur = cv2.GaussianBlur(img, (0, 0), 2.0)
    lo = float(np.percentile(blur, 5))
    hi = float(np.percentile(blur, 99.8))
    if hi - lo < 1e-4:
        return np.ones(img.shape, bool), lo
    thr = lo + 0.18 * (hi - lo)
    return blur > thr, lo


def planetary_metrics(img: np.ndarray, full_scale_max: float = 1.0) -> dict:
    mask, bg = object_mask(img)
    area = int(np.count_nonzero(mask))
    if area < 25:
        mask = np.ones(img.shape, bool)
        area = mask.size
    ys, xs = np.nonzero(mask)
    w = img[ys, xs] - bg
    wsum = float(np.sum(np.clip(w, 0, None))) + 1e-9
    cx = float(np.sum(xs * np.clip(w, 0, None)) / wsum)
    cy = float(np.sum(ys * np.clip(w, 0, None)) / wsum)
    inner = cv2.erode(mask.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    if np.count_nonzero(inner) < 25:
        inner = mask
    mean = float(np.mean(img[inner])) + 1e-6
    sm = cv2.GaussianBlur(img, (0, 0), 1.0)
    lap = cv2.Laplacian(sm, cv2.CV_32F, ksize=3)
    sharp = float(np.var(lap[inner])) / (mean * mean) * 1e4
    gx = cv2.Sobel(sm, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(sm, cv2.CV_32F, 0, 1, ksize=3)
    grad = float(np.mean((gx * gx + gy * gy)[inner])) / (mean * mean) * 1e2
    contrast = float(np.std(img[inner])) / mean
    bgmask = ~cv2.dilate(mask.astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool)
    hp = img - cv2.blur(img, (3, 3))
    region = hp[bgmask] if np.count_nonzero(bgmask) > 500 else hp
    noise = float(1.4826 * np.median(np.abs(region - np.median(region))))
    clipped = float(np.count_nonzero(img[mask] >= 0.985 * full_scale_max)) / max(area, 1)
    return {"sharpness": sharp, "gradient": grad, "brightness": mean, "contrast": contrast, "noise": noise,
            "cx": cx, "cy": cy, "clipped": clipped, "area": area}


def _trail(img: np.ndarray, stars, noise: float) -> float:
    """Length (as a fraction of the frame size) of the longest straight bright line, e.g. a satellite."""
    h, w = img.shape
    bgsub = img - cv2.GaussianBlur(img, (0, 0), 25)
    mask = (bgsub > 2.5 * noise).astype(np.uint8)
    for x, y, f in zip(stars.x, stars.y, stars.fwhm):
        cv2.circle(mask, (int(x), int(y)), int(max(4, 3 * f)), 0, -1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    min_len = int(0.15 * min(h, w))
    lines = cv2.HoughLinesP(mask * 255, 1, np.pi / 360, threshold=max(30, min_len // 3), minLineLength=min_len, maxLineGap=6)
    if lines is None:
        return 0.0
    best = max(math.hypot(l[0][2] - l[0][0], l[0][3] - l[0][1]) for l in lines)
    return float(best / math.hypot(h, w))


def dso_metrics(img: np.ndarray, scale: float) -> dict:
    stars = detect_stars(img, sigma=5.0, max_stars=1500)
    s = measure_summary(stars)
    out = {"stars": s["stars"], "fwhm": (s["fwhm"] or 0.0) * scale, "eccentricity": s["eccentricity"] or 0.0,
           "background": s["background"], "noise": s["noise"], "snr": s["snr"]}
    out["trail"] = _trail(img, stars, s["noise"])
    return out


# --------------------------------------------------------- whole sequence ---

def analyze(src: FrameSource, kind: str = "auto", progress: Callable[[float, str], None] | None = None,
            threads: int = 4, step: int = 1) -> dict:
    """Score all frames. kind: "planetary" | "dso" | "auto"."""
    n = src.count
    if n == 0:
        raise ValueError("The recording has no frames")
    if kind == "auto":
        kind = guess_kind(src)
    idx = list(range(0, n, max(1, step)))
    full_max = 1.0
    t0 = time.time()
    results: dict[int, dict] = {}

    def work(i: int) -> tuple[int, dict]:
        try:
            img, scale = _scoring_view(src, i)
            if kind == "planetary":
                m = planetary_metrics(img, full_max)
                m["cx"] *= scale
                m["cy"] *= scale
                m["area"] = int(m["area"] * scale * scale)
            else:
                m = dso_metrics(img, scale)
            return i, m
        except Exception as e:  # unreadable frame -> mark it bad, keep going
            log.warning("Frame %s could not be analysed: %s", i, e)
            return i, {"error": str(e)}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
        for chunk_start in range(0, len(idx), 256):
            chunk = idx[chunk_start: chunk_start + 256]
            for i, m in ex.map(work, chunk):
                results[i] = m
            done += len(chunk)
            if progress:
                rate = done / max(time.time() - t0, 1e-6)
                progress(done / len(idx), f"Scored {done:,} of {len(idx):,} frames ({rate * 60:,.0f} frames/min)")
    elapsed = time.time() - t0
    report = _finish(src, kind, idx, results)
    report["timing"] = {"seconds": elapsed, "frames_per_minute": len(idx) / max(elapsed, 1e-6) * 60}
    return report


def guess_kind(src: FrameSource) -> str:
    """Video -> planetary. Long exposures or star-like frames -> deep sky."""
    from ..io.images import kind_of
    if src.paths and kind_of(src.paths[0]) in ("ser", "video"):
        return "planetary"
    exp = src.meta.get("EXPTIME") or src.meta.get("EXPOSURE")
    try:
        if exp is not None and float(exp) >= 1.0:
            return "dso"
    except (TypeError, ValueError):
        pass
    img, _ = _scoring_view(src, 0)
    mask, _ = object_mask(img)
    frac = np.count_nonzero(mask) / mask.size
    return "planetary" if frac > 0.02 else "dso"


def _robust_z(a: np.ndarray) -> np.ndarray:
    med = np.nanmedian(a)
    mad = np.nanmedian(np.abs(a - med)) * 1.4826 + 1e-12
    return (a - med) / mad


def _finish(src: FrameSource, kind: str, idx: list[int], results: dict[int, dict]) -> dict:
    metrics = PLANETARY_METRICS if kind == "planetary" else DSO_METRICS
    cols: dict[str, list] = {m: [] for m in metrics if m != "quality"}
    errors = []
    for i in idx:
        r = results.get(i, {})
        if "error" in r:
            errors.append(i)
        for m in cols:
            v = r.get(m)
            cols[m].append(float(v) if v is not None else float("nan"))
    arr = {m: np.array(v, float) for m, v in cols.items()}
    nfr = len(idx)
    flags: list[list[str]] = [[] for _ in range(nfr)]

    if kind == "planetary":
        q = arr["sharpness"].copy()
        # Quality 0..100 relative to the best frames (robust to a single outlier).
        top = np.nanpercentile(q, 99) if nfr > 20 else np.nanmax(q)
        quality = np.clip(q / max(top, 1e-12) * 100, 0, 100)
        b = arr["brightness"]
        bz = _robust_z(b)
        area_med = np.nanmedian(arr["area"])
        # Lunar/solar surface filling the frame: "edge"/"partial" checks don't apply.
        surface = area_med > 0.5 * src.width * src.height
        cxm, cym = np.nanmedian(arr["cx"]), np.nanmedian(arr["cy"])
        drift = np.hypot(arr["cx"] - cxm, arr["cy"] - cym)
        for k in range(nfr):
            if bz[k] < -6 or b[k] < 0.6 * np.nanmedian(b):
                flags[k].append("cloud")
            if arr["clipped"][k] > 0.002:
                flags[k].append("clipped")
            if not surface and arr["area"][k] < 0.5 * area_med:
                flags[k].append("partial")
            if not surface and _near_edge(arr["cx"][k], arr["cy"][k], arr["area"][k], src.width, src.height):
                flags[k].append("edge")
            if math.isnan(q[k]):
                flags[k].append("unreadable")
        summary_extra = {"max_drift_px": float(np.nanmax(drift)) if nfr else 0.0,
                         "median_brightness": float(np.nanmedian(b))}
    else:
        stars = arr["stars"]
        fwhm = np.where(arr["fwhm"] > 0, arr["fwhm"], np.nan)
        snr = arr["snr"]
        # Weighted quality: more stars, smaller and rounder stars, better SNR.
        smed = max(np.nanmedian(stars), 1)
        fmed = np.nanmedian(fwhm) if np.any(np.isfinite(fwhm)) else 1.0
        qraw = (np.clip(stars / smed, 0, 2) * 0.35 + np.clip(fmed / fwhm, 0, 2) * 0.4
                + np.clip(1 - arr["eccentricity"], 0, 1) * 0.1 + np.clip(snr / max(np.nanmedian(snr), 1e-9), 0, 2) * 0.15)
        qraw = np.nan_to_num(qraw, nan=0.0)
        quality = np.clip(qraw / max(np.nanpercentile(qraw, 95), 1e-9) * 100, 0, 100)
        bgz = _robust_z(arr["background"])
        for k in range(nfr):
            if stars[k] < 0.5 * smed or bgz[k] > 8:
                flags[k].append("cloud")
            if arr["trail"][k] > 0:
                flags[k].append("trail")
            if np.isfinite(fwhm[k]) and fwhm[k] > 1.6 * fmed:
                flags[k].append("blurred")
            if arr["eccentricity"][k] > 0.6:
                flags[k].append("elongated")
            if stars[k] == 0:
                flags[k].append("no-stars")
        summary_extra = {"median_fwhm": float(fmed) if np.isfinite(fmed) else None,
                         "median_stars": float(smed)}
    for e in errors:
        flags[idx.index(e)].append("unreadable")
        quality[idx.index(e)] = 0.0

    session = _session_flags(kind, arr, quality)
    t_first = src.frame_time(idx[0]) if idx else None
    if t_first is not None:
        t_last = src.frame_time(idx[-1])
        duration = (t_last - t_first).total_seconds() if t_last else None
    else:
        duration = None
    report = {
        "kind": kind,
        "frames": idx,
        "n_frames": src.count,
        "metrics": {m: [None if not np.isfinite(x) else round(float(x), 6) for x in arr[m]] for m in arr},
        "quality": [round(float(x), 3) for x in quality],
        "flags": flags,
        "session_flags": session,
        "summary": {
            "frames": nfr,
            "duration_s": duration,
            "start_utc": t_first.isoformat() if t_first else None,
            "best_frame": int(idx[int(np.nanargmax(quality))]) if nfr else None,
            "median_quality": float(np.nanmedian(quality)),
            "flagged": int(sum(1 for f in flags if f)),
            **summary_extra,
        },
        "source": src.describe(),
    }
    report["recommendation"] = recommend(report)
    return report


def _near_edge(cx: float, cy: float, area: float, w: int, h: int) -> bool:
    if not np.isfinite(cx) or area <= 0:
        return False
    r = math.sqrt(area / math.pi)
    return cx - r < 2 or cy - r < 2 or cx + r > w - 2 or cy + r > h - 2


def _trend(y: np.ndarray) -> float:
    """Relative change from the start to the end of the session (linear fit)."""
    ok = np.isfinite(y)
    if np.count_nonzero(ok) < 10:
        return 0.0
    x = np.arange(len(y))[ok]
    slope, icept = np.polyfit(x, y[ok], 1)
    start = icept
    end = icept + slope * (len(y) - 1)
    return float((end - start) / (abs(start) + 1e-12))


def _session_flags(kind: str, arr: dict, quality: np.ndarray) -> list[dict]:
    out = []
    if kind == "planetary":
        tr = _trend(arr["sharpness"])
        if tr < -0.25:
            out.append({"flag": "focus-drift", "text": f"Sharpness fell about {abs(tr) * 100:.0f}% during the recording - focus may have drifted (temperature change) or the planet sank lower."})
        trc = _trend(arr["contrast"])
        trb = _trend(arr["brightness"])
        if trc < -0.2 and trb < -0.1:
            out.append({"flag": "dew", "text": "Contrast and brightness slowly dropped - check the mirror/eyepiece for dew or thin cloud."})
        if np.nanmax(arr["clipped"]) > 0.002:
            out.append({"flag": "clipped", "text": "Some frames have over-exposed (clipped) highlights - lower the gain or exposure next time."})
    else:
        f = np.where(arr["fwhm"] > 0, arr["fwhm"], np.nan)
        tr = _trend(f)
        if tr > 0.3:
            out.append({"flag": "focus-drift", "text": f"Stars grew about {tr * 100:.0f}% larger during the session - re-focus every hour, or dew may be forming."})
        trb = _trend(arr["background"])
        if trb > 0.5:
            out.append({"flag": "brightening", "text": "The sky background brightened a lot (dawn, Moon rise or light pollution). Gradient removal will help."})
        trs = _trend(arr["stars"])
        if trs < -0.4:
            out.append({"flag": "dew", "text": "Fewer and fewer stars were detected - dew, thin cloud or the target sinking into haze."})
    return out


def recommend(report: dict) -> dict:
    """Plain-language suggestion: how many frames to keep and what stacking should gain."""
    q = np.array(report["quality"], float)
    n = len(q)
    bad = np.array([bool({"cloud", "unreadable", "no-stars", "partial", "edge"} & set(f)) for f in report["flags"]])
    good = np.count_nonzero(~bad)
    if report["kind"] == "planetary":
        pct = 25 if n >= 2000 else 40 if n >= 500 else 60
    else:
        pct = 90
    keep = max(1, int(round(good * pct / 100)))
    order = np.argsort(-np.where(bad, -1, q))
    kept = order[:keep]
    gain = math.sqrt(keep)
    return {
        "keep_percent": pct,
        "keep_count": int(keep),
        "noise_reduction_x": round(gain, 1),
        "mean_quality_kept": round(float(np.mean(q[kept])), 1) if keep else 0,
        "mean_quality_all": round(float(np.mean(q)), 1) if n else 0,
        "text": (f"Keep the best {pct}% ({keep:,} frames). Stacking them should reduce noise about {gain:.0f}x "
                 f"compared with one frame" + (", while throwing away the blurriest moments of seeing." if report["kind"] == "planetary" else "."))
    }


def select_frames(report: dict, mode: str = "percent", value: float = 25, manual_include: list[int] | None = None,
                  manual_exclude: list[int] | None = None, exclude_flags: tuple[str, ...] = ("cloud", "unreadable", "no-stars", "partial", "edge", "trail")) -> list[int]:
    """Frame indices to stack, best first."""
    frames = np.array(report["frames"])
    q = np.array(report["quality"], float)
    flags = report["flags"]
    bad = np.array([bool(set(exclude_flags) & set(f)) for f in flags])
    order = np.argsort(-np.where(bad, -1e9, q))
    order = [o for o in order if not bad[o]]
    if mode == "count":
        chosen = order[: int(value)]
    elif mode == "threshold":
        chosen = [o for o in order if q[o] >= value]
    elif mode == "all":
        chosen = order
    else:  # percent
        chosen = order[: max(1, int(round(len(order) * value / 100)))]
    sel = [int(frames[o]) for o in chosen]
    excl = set(manual_exclude or [])
    sel = [i for i in sel if i not in excl]
    for i in manual_include or []:
        if i not in sel:
            sel.append(int(i))
    return sel
