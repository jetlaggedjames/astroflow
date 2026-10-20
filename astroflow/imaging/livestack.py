"""Live stacking (EAA - "electronically assisted astronomy").

While you capture deep-sky frames, each new frame is calibrated, its stars
are matched to the first frame (shift + rotation, for the alt-az mount) and
it is added to a running average. The picture on screen keeps getting
cleaner as frames come in. Pixels that suddenly jump far above their running
average (satellites, planes) are left out.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from ..io.debayer import debayer
from ..io.images import to_float
from .calibrate import CalibrationSet
from .register import RegistrationError, Transform, match_stars, warp
from .stars import detect_stars, measure_summary


class LiveStacker:
    def __init__(self, bayer: str | None = None, calibration: CalibrationSet | None = None,
                 model: str = "similarity", reject_sigma: float = 4.0, min_stars: int = 8,
                 max_fwhm_factor: float = 1.8):
        self.bayer = bayer
        self.cal = calibration
        self.model = model
        self.reject_sigma = reject_sigma
        self.min_stars = min_stars
        self.max_fwhm_factor = max_fwhm_factor
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self.ref_stars = None
            self.ref_fwhm = None
            self.mean: np.ndarray | None = None
            self.m2: np.ndarray | None = None
            self.count: np.ndarray | None = None
            self.frames = 0
            self.rejected_frames = 0
            self.last_tr: Transform | None = None
            self.log: list[dict] = []
            self.started = time.time()

    def add(self, raw: np.ndarray, exposure: float | None = None) -> dict:
        """Add one frame. Returns a small report about what happened to it."""
        if self.cal is not None:
            img = self.cal.apply(raw, exposure, self.bayer)
        else:
            img = to_float(raw)
        if self.bayer and img.ndim == 2:
            img = debayer(img, self.bayer, "bilinear")
        img = img.astype(np.float32)
        lum = img.mean(axis=2) if img.ndim == 3 else img
        stars = detect_stars(lum, sigma=5.0, max_stars=300)
        summ = measure_summary(stars)
        with self._lock:
            if len(stars) < self.min_stars:
                self.rejected_frames += 1
                return self._report(False, f"Only {len(stars)} stars - frame skipped (cloud? out of focus?)", summ)
            if self.ref_stars is None:
                self.ref_stars = stars
                self.ref_fwhm = summ["fwhm"]
                tr = Transform.identity()
            else:
                if self.ref_fwhm and summ["fwhm"] and summ["fwhm"] > self.ref_fwhm * self.max_fwhm_factor:
                    self.rejected_frames += 1
                    return self._report(False, "Stars much larger than in the first frame - skipped (wind? focus?)", summ)
                try:
                    tr = match_stars(stars, self.ref_stars, self.model, guess=self.last_tr)
                except RegistrationError:
                    try:
                        tr = match_stars(stars, self.ref_stars, self.model)
                    except RegistrationError as e:
                        self.rejected_frames += 1
                        return self._report(False, f"Could not align: {e}", summ)
            self.last_tr = tr
            shape = lum.shape
        planes = img if img.ndim == 3 else img[..., None]
        warped = np.stack([warp(planes[..., c], tr, shape, "bilinear") for c in range(planes.shape[2])], axis=-1)
        with self._lock:
            self._accumulate(warped)
            self.frames += 1
            return self._report(True, "Stacked", summ, tr)

    def _accumulate(self, x: np.ndarray) -> None:
        valid = np.isfinite(x)
        if self.mean is None:
            self.mean = np.where(valid, x, 0).astype(np.float32)
            self.m2 = np.zeros_like(self.mean)
            self.count = valid.astype(np.float32)
            return
        n = self.count
        if self.frames >= 4:
            sd = np.sqrt(self.m2 / np.maximum(n - 1, 1))
            valid &= ~((x - self.mean) > self.reject_sigma * sd + 0.01)
        xv = np.where(valid, x, 0)
        n_new = n + valid
        delta = np.where(valid, xv - self.mean, 0)
        self.mean += delta / np.maximum(n_new, 1)
        self.m2 += delta * np.where(valid, xv - self.mean, 0)
        self.count = n_new

    def _report(self, ok: bool, msg: str, summ: dict, tr: Transform | None = None) -> dict:
        r = {"stacked": ok, "message": msg, "frames": self.frames, "skipped": self.rejected_frames,
             "stars": summ.get("stars"), "fwhm": summ.get("fwhm"),
             "rotation_deg": round(tr.rotation_deg, 3) if tr else None,
             "elapsed_s": round(time.time() - self.started, 1)}
        self.log.append(r)
        del self.log[:-500]
        return r

    def image(self) -> np.ndarray | None:
        with self._lock:
            if self.mean is None:
                return None
            img = self.mean.copy()
        return img[..., 0] if img.shape[2] == 1 else img

    def total_exposure_note(self, exposure: float | None) -> str:
        if not exposure:
            return f"{self.frames} frames"
        return f"{self.frames} frames x {exposure:g}s = {self.frames * exposure / 60:.1f} min"
