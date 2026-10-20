"""Combining many aligned frames into one, with outlier rejection.

Rejection removes values that don't belong - satellite and plane trails,
cosmic-ray hits, hot pixels - by comparing each pixel across all frames.

Methods:
  * "none"        - plain (weighted) average
  * "minmax"      - drop the highest and lowest value(s)
  * "percentile"  - drop values far from the median (good for < 8 frames)
  * "sigma"       - iterative sigma-kappa clipping around the median
  * "winsorized"  - sigma clipping with robust (winsorized) statistics (10-25 frames)
  * "linear_fit"  - fit a line to the sorted values; best for many frames (> 25)
  * "auto"        - picks one of the above from the number of frames

Memory: frames live on disk (memory-mapped). We process horizontal stripes
sized to the memory budget, so any number of frames works.
"""
from __future__ import annotations

import math
from typing import Callable, Sequence

import numpy as np


def auto_method(n: int) -> str:
    if n < 3:
        return "none"
    if n < 8:
        return "percentile"
    if n < 25:
        return "winsorized"
    return "linear_fit"


def _nanmedian(a: np.ndarray) -> np.ndarray:
    """Median along axis 0 ignoring NaN - much faster than np.nanmedian for small N."""
    srt = np.sort(a, axis=0)  # NaN sorts last
    cnt = np.sum(np.isfinite(a), axis=0)
    lo = np.maximum((cnt - 1) // 2, 0)[None]
    hi = np.maximum(cnt // 2, 0)[None]
    med = 0.5 * (np.take_along_axis(srt, lo, 0)[0] + np.take_along_axis(srt, hi, 0)[0])
    return np.where(cnt > 0, med, np.nan)


def _nanstd(a: np.ndarray, keep: np.ndarray) -> np.ndarray:
    cnt = np.maximum(keep.sum(0), 1)
    x = np.where(keep, a, 0.0)
    mean = x.sum(0) / cnt
    var = np.where(keep, (a - mean) ** 2, 0.0).sum(0) / cnt
    return np.sqrt(var)


def reject(stack: np.ndarray, method: str, low: float = 3.0, high: float = 3.0, iterations: int = 5) -> np.ndarray:
    """Return a boolean mask (same shape as ``stack``) of values to KEEP. NaN = no data."""
    keep = np.isfinite(stack)
    n = stack.shape[0]
    if method == "none" or n < 3:
        return keep
    if method == "minmax":
        lo = np.argmin(np.where(keep, stack, np.inf), axis=0)
        hi = np.argmax(np.where(keep, stack, -np.inf), axis=0)
        idx = np.arange(n).reshape((n,) + (1,) * (stack.ndim - 1))
        return keep & (idx != lo[None]) & (idx != hi[None])
    if method == "percentile":
        med = _nanmedian(np.where(keep, stack, np.nan))
        rel = (stack - med) / np.maximum(np.abs(med), 1e-6)
        return keep & (rel > -low * 0.1) & (rel < high * 0.1)
    if method in ("sigma", "winsorized"):
        for _ in range(iterations):
            data = np.where(keep, stack, np.nan)
            med = _nanmedian(data)
            sd = _nanstd(stack, keep)
            if method == "winsorized":
                # Winsorize: clamp extreme values to med +- 1.5 sigma and recompute sigma.
                # (Clamping never changes which value is the median, so med stays valid.)
                for _w in range(3):
                    w = np.clip(stack, med - 1.5 * sd, med + 1.5 * sd)
                    sd = 1.134 * _nanstd(w, keep)
            new = keep & (stack >= med - low * sd) & (stack <= med + high * sd)
            if np.array_equal(new, keep):
                break
            keep = new
        return keep
    if method == "linear_fit":
        idx = np.arange(n, dtype=np.float32).reshape((n,) + (1,) * (stack.ndim - 1))
        for _ in range(max(2, iterations // 2)):
            data = np.where(keep, stack, np.inf)
            order = np.argsort(data, axis=0)
            srt = np.take_along_axis(data, order, 0)
            cnt = keep.sum(0)
            valid = idx < cnt[None]
            x = np.where(valid, idx, 0.0)
            y = np.where(valid, srt, 0.0)
            c = np.maximum(cnt, 1).astype(np.float32)
            sx, sy = x.sum(0), y.sum(0)
            sxx, sxy = (x * x).sum(0), (x * y).sum(0)
            den = c * sxx - sx * sx
            slope = np.where(den > 0, (c * sxy - sx * sy) / np.where(den > 0, den, 1), 0.0)
            icpt = (sy - slope * sx) / c
            resid = np.where(valid, y - (slope * x + icpt), 0.0)
            sd = np.sqrt((resid * resid).sum(0) / np.maximum(c - 2, 1))
            # Rank of each value among the kept ones = inverse of the sort order.
            rank = np.empty_like(order)
            np.put_along_axis(rank, order, np.broadcast_to(idx.astype(order.dtype), order.shape), 0)
            dev = stack - (slope * rank + icpt)
            new = keep & (dev >= -low * sd) & (dev <= high * sd)
            if np.array_equal(new, keep):
                break
            keep = new
        return keep
    raise ValueError(f"Unknown rejection method: {method}")


def combine(stack: np.ndarray, weights: np.ndarray | None, method: str = "auto", low: float = 3.0,
            high: float = 3.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Combine a (N, ...) block. Returns (result, rejected_low_count, rejected_high_count)."""
    n = stack.shape[0]
    if method == "auto":
        method = auto_method(n)
    keep = reject(stack, method, low, high)
    finite = np.isfinite(stack)
    w = np.ones(n, np.float32) if weights is None else np.asarray(weights, np.float32)
    wb = w.reshape((n,) + (1,) * (stack.ndim - 1)) * keep
    num = np.where(keep, stack, 0.0) * wb
    den = wb.sum(0)
    with np.errstate(all="ignore"):
        out = np.where(den > 0, num.sum(0) / np.where(den > 0, den, 1), np.nan)
    med = _nanmedian(np.where(finite, stack, np.nan))
    rej = finite & ~keep
    rlow = np.sum(rej & (stack < med), axis=0)
    rhigh = np.sum(rej & (stack >= med), axis=0)
    # Pixels where everything was rejected: fall back to the median.
    out = np.where(np.isfinite(out), out, med)
    return out.astype(np.float32), rlow.astype(np.uint16), rhigh.astype(np.uint16)


class FrameStore:
    """Aligned frames on disk as one memory-mapped array (N, H, W, C)."""

    def __init__(self, path, n: int, shape: tuple[int, ...], dtype=np.float32):
        self.path = str(path)
        self.n = n
        self.shape = tuple(shape)
        self.arr = np.lib.format.open_memmap(self.path, mode="w+", dtype=dtype, shape=(n,) + self.shape)

    def __setitem__(self, i: int, frame: np.ndarray) -> None:
        self.arr[i] = frame.reshape(self.shape).astype(self.arr.dtype)

    def flush(self) -> None:
        self.arr.flush()

    def close(self, delete: bool = True) -> None:
        import os
        try:
            del self.arr
        except AttributeError:
            pass
        if delete:
            try:
                os.remove(self.path)
            except OSError:
                pass


def integrate_store(store: FrameStore, weights: Sequence[float] | None = None, method: str = "auto",
                    low: float = 3.0, high: float = 3.0, norm: Sequence[tuple[np.ndarray, np.ndarray]] | None = None,
                    memory_budget: int = 2 * 1024**3, valid: Sequence[bool] | None = None,
                    progress: Callable[[float, str], None] | None = None, threads: int = 0) -> dict:
    """Integrate a FrameStore stripe by stripe, several stripes in parallel.

    ``norm``: per frame (scale, offset) arrays (per channel), applied as x*scale + offset.
    """
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor

    arr = store.arr
    n = arr.shape[0]
    idx = [i for i in range(n) if valid is None or valid[i]]
    if not idx:
        raise ValueError("No usable frames to integrate")
    H = arr.shape[1]
    rest = int(np.prod(arr.shape[2:]))
    threads = threads or min(4, os.cpu_count() or 1)
    bytes_per_row = len(idx) * rest * 4 * 8  # stack + sort + masks + temporaries
    # Small stripes stay cache-friendly; together they respect the memory budget.
    rows = int(max(1, min(H, memory_budget // max(bytes_per_row * threads, 1), max(8, 4_000_000 // max(len(idx) * rest, 1)))))
    out = np.zeros(arr.shape[1:], np.float32)
    rlo = np.zeros(arr.shape[1:], np.uint16)
    rhi = np.zeros(arr.shape[1:], np.uint16)
    cov = np.zeros(arr.shape[1:3], np.uint16)
    w = None if weights is None else np.array([weights[i] for i in idx], np.float32)
    starts = list(range(0, H, rows))
    done = [0]
    lock = threading.Lock()
    used_method = method if method != "auto" else auto_method(len(idx))

    def work(y0: int) -> None:
        y1 = min(H, y0 + rows)
        block = np.stack([arr[i, y0:y1] for i in idx]).astype(np.float32)
        if norm is not None:
            for k, i in enumerate(idx):
                sc, off = norm[i]
                block[k] = block[k] * sc + off
        o, a, b = combine(block, w, used_method, low, high)
        out[y0:y1], rlo[y0:y1], rhi[y0:y1] = o, a, b
        fin = np.isfinite(block)
        cov[y0:y1] = fin.all(axis=-1).sum(0) if fin.ndim == 4 else fin.sum(0)
        with lock:
            done[0] += 1
            if progress:
                progress(done[0] / len(starts), f"Integrating {len(idx)} frames ({used_method} rejection): {done[0] * 100 // len(starts)}%")

    with ThreadPoolExecutor(max_workers=threads) as ex:
        list(ex.map(work, starts))
    return {"image": out, "rejected_low": rlo, "rejected_high": rhi, "coverage": cov,
            "frames": len(idx), "method": used_method,
            "rejected_percent": float((rlo.sum() + rhi.sum()) / max(1, rlo.size * len(idx)) * 100)}


def normalization(frames_stats: list[tuple[np.ndarray, np.ndarray]], ref: int = 0, mode: str = "additive_scaling") -> list[tuple[np.ndarray, np.ndarray]]:
    """Per-frame (scale, offset) so every frame matches the reference's background level and noise scale.

    frames_stats: list of (median, mad) per channel.
    """
    med_r, mad_r = frames_stats[ref]
    out = []
    for med, mad in frames_stats:
        if mode == "none":
            out.append((np.ones_like(med), np.zeros_like(med)))
        elif mode == "additive":
            out.append((np.ones_like(med), med_r - med))
        else:
            sc = mad_r / np.maximum(mad, 1e-9)
            out.append((sc, med_r - med * sc))
    return out


def frame_stats(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Median and MAD per channel, from a subsample, ignoring NaN."""
    data = img if img.ndim == 3 else img[..., None]
    step = max(1, int(math.sqrt(data.shape[0] * data.shape[1] / 250_000)))
    sub = data[::step, ::step].reshape(-1, data.shape[2])
    med = np.nanmedian(sub, axis=0)
    mad = np.nanmedian(np.abs(sub - med), axis=0) * 1.4826
    return med.astype(np.float32), np.maximum(mad, 1e-9).astype(np.float32)
