"""Running a list of processing steps, non-destructively and fast.

A processing "recipe" is a list of steps:
    {"uid": "a1b2", "op": "wavelets", "params": {...}, "enabled": true,
     "mask": "m1" or null, "opacity": 1.0}
plus mask definitions {"m1": {"type": "luminance", ...}}.

The original image is never changed. The result after every step is cached,
keyed by everything before it, so when you move a slider on step 5 only
steps 5+ are recomputed. Previews run on a reduced-size copy with pixel
sizes scaled to match; "full resolution" renders the real thing.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from ..config import atomic_write_text, presets_dir
from ..imaging.quality import object_mask
from .core import luminance
from .masks import apply_masked, build_mask
from .ops import REGISTRY, Ctx, run_op

log = logging.getLogger("astroflow.process")


def new_uid() -> str:
    return uuid.uuid4().hex[:8]


def make_step(op_id: str, params: dict | None = None, **kw) -> dict:
    o = REGISTRY[op_id]
    p = o.defaults()
    p.update(params or {})
    return {"uid": new_uid(), "op": op_id, "params": p, "enabled": True, "mask": None, "opacity": 1.0, **kw}


class Renderer:
    """Runs recipes with an in-memory cache of intermediate results."""

    def __init__(self, max_bytes: int = 1536 * 1024**2):
        self.max_bytes = max_bytes
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    # -- cache ---------------------------------------------------------------
    def _get(self, key: str) -> np.ndarray | None:
        with self._lock:
            v = self._cache.get(key)
            if v is not None:
                self._cache.move_to_end(key)
            return v

    def _put(self, key: str, img: np.ndarray) -> None:
        with self._lock:
            if key in self._cache:
                return
            self._cache[key] = img
            self._bytes += img.nbytes
            while self._bytes > self.max_bytes and len(self._cache) > 1:
                _, old = self._cache.popitem(last=False)
                self._bytes -= old.nbytes

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._bytes = 0

    # -- rendering -----------------------------------------------------------
    def base_at_scale(self, base: np.ndarray, base_key: str, scale: float) -> np.ndarray:
        if scale >= 0.999:
            return base
        key = f"base|{base_key}|{scale:.4f}"
        got = self._get(key)
        if got is None:
            H, W = base.shape[:2]
            got = cv2.resize(base, (max(1, int(round(W * scale))), max(1, int(round(H * scale)))), interpolation=cv2.INTER_AREA)
            self._put(key, got)
        return got

    def render(self, base: np.ndarray, base_key: str, steps: list[dict], masks: dict | None = None,
               scale: float = 1.0, meta: dict | None = None, load_image: Callable | None = None,
               upto: int | None = None, progress: Callable[[float, str], None] | None = None,
               check: Callable[[], None] | None = None) -> tuple[np.ndarray, dict]:
        """Run ``steps`` (optionally only the first ``upto``) on ``base``. Returns (image, info)."""
        masks = masks or {}
        img = self.base_at_scale(base, base_key, scale)
        ctx = Ctx(scale=scale, meta=meta or {}, load_image=load_image)
        h = hashlib.sha1(f"{base_key}|{scale:.4f}".encode())
        timings = []
        active = [s for s in steps[: upto if upto is not None else len(steps)]]
        for i, st in enumerate(active):
            if not st.get("enabled", True) or st.get("op") not in REGISTRY:
                continue
            mdef = masks.get(st.get("mask")) if st.get("mask") else None
            h.update(json.dumps([st["op"], st.get("params"), mdef, st.get("opacity", 1.0)], sort_keys=True, default=str).encode())
            key = h.hexdigest()
            cached = self._get(key)
            if cached is not None:
                img = cached
                timings.append({"uid": st.get("uid"), "op": st["op"], "ms": 0, "cached": True})
                continue
            if check:
                check()
            if progress:
                progress(i / max(1, len(active)), f"Running {REGISTRY[st['op']].name}")
            t0 = time.time()
            before = img
            out = run_op(st["op"], img, st.get("params"), ctx)
            if mdef is not None or float(st.get("opacity", 1.0)) < 0.999:
                m = build_mask(mdef, before, scale) if mdef is not None else None
                out = apply_masked(before, out, m, float(st.get("opacity", 1.0)))
            img = out
            self._put(key, img)
            timings.append({"uid": st.get("uid"), "op": st["op"], "ms": round((time.time() - t0) * 1000), "cached": False})
        return img, {"notes": ctx.notes, "timings": timings, "shape": list(img.shape)}


renderer = Renderer()


# ---------------------------------------------------------------- Auto ---

def guess_target(img: np.ndarray, meta: dict | None = None) -> str:
    """'planet', 'moon' or 'dso', from metadata or the picture itself."""
    meta = meta or {}
    t = str(meta.get("TARGET_TYPE") or meta.get("target_type") or "").lower()
    if t in ("planet", "moon", "dso"):
        return t
    name = str(meta.get("OBJECT") or "").lower()
    if any(k in name for k in ("jupiter", "saturn", "mars", "venus", "mercury", "uranus", "neptune")):
        return "planet"
    if "moon" in name or "luna" in name:
        return "moon"
    if re.match(r"^(m|ngc|ic|c|sh2-)\s*\d+", name):
        return "dso"
    L = luminance(img).astype(np.float32)
    small = cv2.resize(L, (max(1, L.shape[1] // 4), max(1, L.shape[0] // 4)), interpolation=cv2.INTER_AREA) if L.shape[1] > 800 else L
    from ..imaging.stars import detect_stars
    stars = detect_stars(small, sigma=6.0, max_stars=200)
    mask, _ = object_mask(small)
    frac = mask.mean()
    if len(stars) >= 15:
        # Many point sources: deep sky, unless one big compact planet dominates.
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
        big = (stats[1:, cv2.CC_STAT_AREA] > mask.size * 0.02).sum() if n > 1 else 0
        return "planet" if (big == 1 and n < 6 and frac < 0.4) else "dso"
    if frac > 0.55:
        return "moon"
    if 0.01 < frac <= 0.55:
        # A single bright compact object = planet; many small spots = stars.
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
        big = (stats[1:, cv2.CC_STAT_AREA] > mask.size * 0.005).sum() if n > 1 else 0
        if big >= 1 and n < 30:
            return "planet"
    return "dso"


def auto_pipeline(target: str, img: np.ndarray | None = None) -> list[dict]:
    """A sensible starting recipe. Only used when the user presses Auto."""
    if target == "planet":
        steps = [
            make_step("rgb_align", {"auto": True}),
            make_step("wavelets", {"l1": 0.0, "l2": 6.0, "l3": 3.0, "l4": 1.0, "n1": 0.8, "n2": 0.35, "protect_limb": 10.0}),
            make_step("denoise", {"luminance": 0.3, "chroma": 1.0}),
            make_step("limb_cleanup", {"width": 3.0, "strength": 0.6}),
            make_step("auto_levels", {"clip_low": 0.5, "clip_high": 0.02}),
            make_step("saturation", {"saturation": 0.25, "vibrance": 0.2}),
        ]
    elif target == "moon":
        steps = [
            make_step("deconvolution", {"psf": "auto", "iterations": 10, "regularization": 0.003}),
            make_step("wavelets", {"l1": 0.0, "l2": 4.0, "l3": 2.0, "l4": 0.8, "n1": 0.8, "n2": 0.3, "protect_limb": 0.0}),
            make_step("denoise", {"luminance": 0.25, "chroma": 1.0}),
            make_step("local_contrast", {"amount": 0.2}),
            make_step("auto_levels", {"clip_low": 0.02, "clip_high": 0.02}),
        ]
    else:
        steps = [
            make_step("background_extraction", {"method": "poly", "degree": 2}),
            make_step("color_calibration", {"method": "background+stars"}),
            make_step("auto_stretch", {"target": 0.18}),
            make_step("denoise", {"method": "wavelet", "luminance": 0.6, "chroma": 1.4}),
            make_step("saturation", {"saturation": 0.35, "vibrance": 0.2}),
            make_step("curves", {"points": [[0, 0], [0.25, 0.22], [0.75, 0.8], [1, 1]], "channel": "luminance"}),
        ]
    for s in steps:
        s["auto"] = True
    return steps


# -------------------------------------------------------------- Presets ---

BUILTIN_PRESETS = {
    "Jupiter default": {"target": "planet", "steps": [
        ("rgb_align", {"auto": True}),
        ("wavelets", {"l2": 12.0, "l3": 6.0, "l4": 1.5, "n1": 0.9, "n2": 0.4}),
        ("denoise", {"luminance": 0.3, "chroma": 1.0}),
        ("limb_cleanup", {"width": 3.0}),
        ("auto_levels", {"clip_low": 0.5}),
        ("saturation", {"saturation": 0.3})]},
    "Saturn default": {"target": "planet", "steps": [
        ("rgb_align", {"auto": True}),
        ("wavelets", {"l2": 9.0, "l3": 5.0, "l4": 1.0, "n1": 0.9}),
        ("denoise", {"luminance": 0.3, "chroma": 1.2}),
        ("auto_levels", {"clip_low": 0.5}),
        ("saturation", {"saturation": 0.2})]},
    "Mars default": {"target": "planet", "steps": [
        ("rgb_align", {"auto": True}),
        ("wavelets", {"l2": 10.0, "l3": 6.0, "l4": 2.0, "n1": 0.9}),
        ("denoise", {"luminance": 0.3, "chroma": 1.0}),
        ("auto_levels", {"clip_low": 0.5}),
        ("saturation", {"saturation": 0.35})]},
    "Moon sharpen": {"target": "moon", "steps": [
        ("deconvolution", {"psf": "auto", "iterations": 12}),
        ("wavelets", {"l2": 4.0, "l3": 2.0, "l4": 0.8, "protect_limb": 0.0}),
        ("local_contrast", {"amount": 0.35}),
        ("auto_levels", {"clip_low": 0.05, "clip_high": 0.05})]},
    "Mineral Moon": {"target": "moon", "steps": [
        ("wavelets", {"l2": 3.0, "l3": 1.5, "protect_limb": 0.0}),
        ("lunar_color", {"amount": 4.0, "smooth": 4.0}),
        ("auto_levels", {"clip_low": 0.05, "clip_high": 0.05})]},
    "Deep-sky default": {"target": "dso", "steps": [
        ("background_extraction", {}),
        ("color_calibration", {"method": "background+stars"}),
        ("auto_stretch", {"target": 0.18}),
        ("denoise", {"luminance": 0.6, "chroma": 1.4}),
        ("saturation", {"saturation": 0.35})]},
    "Galaxy (bright core)": {"target": "dso", "steps": [
        ("background_extraction", {}),
        ("color_calibration", {"method": "background+stars"}),
        ("arcsinh_stretch", {"strength": 120.0}),
        ("hdr_compress", {"amount": 0.5}),
        ("denoise", {"luminance": 0.6, "chroma": 1.4}),
        ("saturation", {"saturation": 0.4})]},
    "Nebula": {"target": "dso", "steps": [
        ("background_extraction", {}),
        ("color_calibration", {"method": "background+stars"}),
        ("ghs", {"D": 8.0, "b": 3.0, "SP": 0.02}),
        ("denoise", {"method": "nlmeans", "luminance": 0.7, "chroma": 1.5}),
        ("star_reduction", {"amount": 0.4}),
        ("saturation", {"saturation": 0.6})]},
}


def _preset_file(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip() or "preset"
    return presets_dir() / f"{safe}.json"


def list_presets() -> list[dict]:
    out = [{"name": n, "target": v["target"], "builtin": True, "steps": len(v["steps"])} for n, v in BUILTIN_PRESETS.items()]
    for f in sorted(presets_dir().glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"name": d["name"], "target": d.get("target", ""), "builtin": False, "steps": len(d.get("steps", []))})
        except Exception:
            continue
    return out


def load_preset(name: str) -> dict:
    if name in BUILTIN_PRESETS:
        b = BUILTIN_PRESETS[name]
        return {"name": name, "target": b["target"], "steps": [make_step(o, p) for o, p in b["steps"]], "masks": {}}
    f = _preset_file(name)
    if not f.exists():
        raise KeyError(name)
    d = json.loads(f.read_text(encoding="utf-8"))
    steps = []
    for s in d.get("steps", []):
        if s.get("op") in REGISTRY:
            st = make_step(s["op"], s.get("params"))
            st.update({k: s[k] for k in ("enabled", "mask", "opacity") if k in s})
            steps.append(st)
    return {"name": d["name"], "target": d.get("target", ""), "steps": steps, "masks": d.get("masks", {})}


def save_preset(name: str, steps: list[dict], target: str = "", masks: dict | None = None) -> dict:
    if name in BUILTIN_PRESETS:
        raise ValueError("That name belongs to a built-in preset; choose another name.")
    data = {"name": name, "target": target, "masks": masks or {},
            "steps": [{k: copy.deepcopy(s.get(k)) for k in ("op", "params", "enabled", "mask", "opacity")} for s in steps]}
    atomic_write_text(_preset_file(name), json.dumps(data, indent=2))
    return {"name": name, "steps": len(steps)}


def delete_preset(name: str) -> None:
    if name in BUILTIN_PRESETS:
        raise ValueError("Built-in presets cannot be deleted.")
    f = _preset_file(name)
    if f.exists():
        f.unlink()


def describe_steps(steps: list[dict]) -> list[str]:
    """Human-readable history lines written into output files."""
    out = []
    for s in steps:
        if not s.get("enabled", True) or s.get("op") not in REGISTRY:
            continue
        o = REGISTRY[s["op"]]
        ps = ", ".join(f"{k}={v}" for k, v in (s.get("params") or {}).items() if not isinstance(v, (list, dict)))
        out.append(f"{o.name}: {ps}" + (f" [mask {s['mask']}]" if s.get("mask") else ""))
    return out
