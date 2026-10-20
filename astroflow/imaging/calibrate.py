"""Calibration frames: darks, flats, flat-darks and bias.

Plain language:
  * Bias  - zero-length exposures: the camera's electronic "floor".
  * Dark  - lens cap on, same exposure/gain/temperature as your lights:
            records sensor glow and hot pixels so they can be subtracted.
  * Flat  - an evenly lit white field (e.g. a T-shirt over the tube at
            dusk): records dust donuts and vignetting so they can be divided out.
  * Flat-dark - darks with the same exposure as the flats.

Several raw frames of each kind are combined into one low-noise "master".
Masters are stored in a calibration library (one folder) indexed by camera,
gain, offset, binning, temperature and exposure, so the right master is
found automatically next time.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
from scipy import ndimage

from ..config import atomic_write_text, calibration_dir, temp_dir
from ..io.images import read_image, to_float, write_fits
from ..io.source import open_source
from .integrate import FrameStore, integrate_store

log = logging.getLogger("astroflow.calibration")
KINDS = ("bias", "dark", "flat", "flatdark")


def frame_key(meta: dict) -> dict:
    """The properties that must match between a master and the frames it calibrates."""
    def num(k, *alts, default=None):
        for key in (k, *alts):
            v = meta.get(key)
            if v is not None and v != "":
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return default
    return {
        "camera": str(meta.get("INSTRUME") or meta.get("CAMERA") or "").strip(),
        "gain": num("GAIN", "EGAIN_SET"),
        "offset": num("OFFSET", "BLKLEVEL"),
        "binning": int(num("XBINNING", default=1) or 1),
        "temperature": num("CCD-TEMP", "SET-TEMP", "TEMPERAT"),
        "exposure": num("EXPTIME", "EXPOSURE"),
        "width": int(meta.get("_width") or meta.get("NAXIS1") or 0),
        "height": int(meta.get("_height") or meta.get("NAXIS2") or 0),
        "bayer": str(meta.get("BAYERPAT") or "").upper() or None,
    }


def make_master(paths: Sequence[str | Path], kind: str, bias: np.ndarray | None = None,
                dark: np.ndarray | None = None, method: str = "winsorized",
                memory_budget: int = 2 * 1024**3, progress: Callable[[float, str], None] | None = None) -> tuple[np.ndarray, dict]:
    """Combine raw calibration frames into a master. Works on raw (un-debayered) data."""
    if kind not in KINDS:
        raise ValueError(kind)
    src = open_source(list(paths))
    n = src.count
    if n == 0:
        raise ValueError("No frames")
    shape = (src.height, src.width) + ((3,) if src.channels == 3 else ())
    store = FrameStore(temp_dir() / f"master-{uuid.uuid4().hex}.npy", n, shape)
    prog = progress or (lambda f, m=None: None)
    try:
        for i in range(n):
            img = to_float(src.get_raw(i))
            if kind == "flat":
                # Remove the flat's own dark signal (flat-dark master, else bias), then normalise.
                if dark is not None:
                    img = img - dark
                elif bias is not None:
                    img = img - bias
                img = _normalize_flat(img, src.bayer)
            # Darks keep their bias level so they can be subtracted from lights directly.
            store[i] = img
            prog(0.6 * (i + 1) / n, f"Reading {kind} frame {i + 1}/{n}")
        store.flush()
        res = integrate_store(store, None, method if n >= 3 else "none", 3.0, 3.0, memory_budget=memory_budget,
                              progress=lambda f, m: prog(0.6 + 0.4 * f, m))
    finally:
        store.close()
    master = res["image"]
    if kind == "flat":
        master = _normalize_flat(master, src.bayer)
        master = np.where(master > 0.05, master, 1.0).astype(np.float32)
    meta = dict(src.meta)
    meta.update({"IMAGETYP": f"Master {kind}", "NCOMBINE": n, "BAYERPAT": src.bayer or ""})
    src.close()
    return master, meta


def _normalize_flat(img: np.ndarray, bayer: str | None) -> np.ndarray:
    """Scale a flat so each colour channel averages 1 (keeps colour balance unchanged)."""
    out = img.astype(np.float32, copy=True)
    if bayer and img.ndim == 2:
        for dy in (0, 1):
            for dx in (0, 1):
                sub = out[dy::2, dx::2]
                m = float(np.nanmedian(sub))
                out[dy::2, dx::2] = sub / max(m, 1e-6)
    elif img.ndim == 3:
        for c in range(3):
            out[..., c] /= max(float(np.nanmedian(out[..., c])), 1e-6)
    else:
        out /= max(float(np.nanmedian(out)), 1e-6)
    return out


def hot_pixel_map(master_dark: np.ndarray, sigma: float = 6.0) -> np.ndarray:
    """Pixels much brighter than the rest of the dark frame (hot/warm pixels)."""
    d = master_dark if master_dark.ndim == 2 else master_dark.mean(axis=2)
    med = np.median(d)
    mad = np.median(np.abs(d - med)) * 1.4826 + 1e-9
    return d > med + sigma * mad


def cosmetic_correct(raw: np.ndarray, bad: np.ndarray | None = None, bayer: str | None = None,
                     auto_sigma: float = 0.0) -> np.ndarray:
    """Replace hot/cold pixels with the median of their same-colour neighbours.

    ``bad``: known bad-pixel mask (from the master dark). ``auto_sigma`` > 0 also
    finds isolated outliers in the frame itself.
    """
    img = raw.astype(np.float32, copy=True)
    step = 2 if (bayer and img.ndim == 2) else 1
    mask = np.zeros(img.shape[:2], bool) if bad is None else bad.copy()
    if img.ndim == 2:
        planes = [img]
    else:
        planes = [img[..., c] for c in range(img.shape[2])]
    # Same-colour neighbourhood median (for Bayer data neighbours are 2 px apart).
    fp = np.zeros((2 * step + 1, 2 * step + 1), bool)
    fp[::step, ::step] = True
    fp[step, step] = False
    for pl in planes:
        med = ndimage.median_filter(pl, footprint=fp, mode="mirror")
        m = mask
        if auto_sigma > 0:
            diff = pl - med
            mad = np.median(np.abs(diff)) * 1.4826 + 1e-9
            m = m | (np.abs(diff) > auto_sigma * mad) & (np.abs(diff) > 0.02)
        pl[m] = med[m]
    return img


class CalibrationSet:
    """Masters to apply to light frames."""

    def __init__(self, bias: np.ndarray | None = None, dark: np.ndarray | None = None,
                 flat: np.ndarray | None = None, dark_exposure: float | None = None,
                 hot_sigma: float = 6.0):
        self.bias, self.dark, self.flat = bias, dark, flat
        self.dark_exposure = dark_exposure
        self.hot = hot_pixel_map(dark, hot_sigma) if dark is not None else None

    def apply(self, raw: np.ndarray, exposure: float | None = None, bayer: str | None = None,
              cosmetic: bool = True, auto_sigma: float = 0.0) -> np.ndarray:
        img = to_float(raw)
        if self.dark is not None:
            d = self.dark
            if (exposure and self.dark_exposure and self.bias is not None
                    and abs(exposure - self.dark_exposure) / self.dark_exposure > 0.05):
                # Scale the thermal part of the dark to the light's exposure.
                d = self.bias + (self.dark - self.bias) * (exposure / self.dark_exposure)
            img = img - d
        elif self.bias is not None:
            img = img - self.bias
        if self.flat is not None:
            img = img / self.flat
        if cosmetic and (self.hot is not None or auto_sigma > 0):
            img = cosmetic_correct(img, self.hot, bayer, auto_sigma)
        return np.clip(img, 0, None).astype(np.float32)

    def describe(self) -> dict:
        return {"bias": self.bias is not None, "dark": self.dark is not None, "flat": self.flat is not None,
                "hot_pixels": int(self.hot.sum()) if self.hot is not None else 0}


# ------------------------------------------------------------ library ---

class CalibrationLibrary:
    """Master frames stored on disk with an index for automatic matching."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else calibration_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def list(self) -> list[dict]:
        with self._lock:
            return [e for e in self._load() if Path(e["path"]).exists()]

    def add(self, kind: str, master: np.ndarray, meta: dict, n_frames: int, note: str = "") -> dict:
        key = frame_key(meta)
        key["width"], key["height"] = int(master.shape[1]), int(master.shape[0])
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"master_{kind}_{key['camera'] or 'camera'}_g{key['gain'] if key['gain'] is not None else 'x'}_{key['exposure'] or 0:g}s_{stamp}.fits"
        name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
        path = self.root / name
        write_fits(path, master, {**{k: v for k, v in meta.items() if not str(k).startswith("_")},
                                   "IMAGETYP": f"Master {kind}", "NCOMBINE": n_frames}, bits=32)
        entry = {"id": uuid.uuid4().hex[:10], "kind": kind, "path": str(path), "frames": n_frames,
                 "created": dt.datetime.now().isoformat(timespec="seconds"), "note": note, **key}
        with self._lock:
            idx = self._load()
            idx.append(entry)
            atomic_write_text(self.index_path, json.dumps(idx, indent=1))
        return entry

    def remove_entry(self, entry_id: str) -> None:
        """Forget an index entry (the FITS file itself is left alone)."""
        with self._lock:
            idx = [e for e in self._load() if e["id"] != entry_id]
            atomic_write_text(self.index_path, json.dumps(idx, indent=1))

    def load(self, entry: dict) -> np.ndarray:
        data, _ = read_image(entry["path"])
        return to_float(data) if data.dtype != np.float32 else data

    def find(self, kind: str, meta: dict) -> tuple[dict | None, str]:
        """Best master for a light frame described by ``meta``; returns (entry, explanation)."""
        want = frame_key(meta)
        best, best_score, why = None, -1e9, ""
        for e in self.list():
            if e["kind"] != kind:
                continue
            if want["width"] and e.get("width") and (e["width"], e["height"]) != (want["width"], want["height"]):
                continue
            score = 0.0
            notes = []
            if want["camera"] and e.get("camera") and want["camera"] != e["camera"]:
                continue
            if kind != "flat" and want["gain"] is not None and e.get("gain") is not None:
                if abs(want["gain"] - e["gain"]) > 0.5:
                    continue
                score += 10
            if kind != "flat" and want["offset"] is not None and e.get("offset") is not None and abs(want["offset"] - e["offset"]) > 0.5:
                continue
            if e.get("binning", 1) != want["binning"]:
                continue
            if kind == "dark" and want["exposure"] and e.get("exposure"):
                rel = abs(want["exposure"] - e["exposure"]) / want["exposure"]
                score -= rel * 50
                if rel > 0.05:
                    notes.append(f"exposure {e['exposure']:g}s vs {want['exposure']:g}s (will be scaled)")
            if kind == "dark" and want["temperature"] is not None and e.get("temperature") is not None:
                dtemp = abs(want["temperature"] - e["temperature"])
                score -= dtemp * 3
                if dtemp > 3:
                    notes.append(f"temperature differs by {dtemp:.0f} C")
            age = (dt.datetime.now() - dt.datetime.fromisoformat(e["created"])).days
            score -= age * (0.5 if kind == "flat" else 0.05)
            if score > best_score:
                best, best_score, why = e, score, "; ".join(notes) or "good match"
        return best, why
