"""Optional NVIDIA GPU acceleration through CuPy, with automatic CPU fallback.

If CuPy (``pip install cupy-cuda12x``) and an NVIDIA driver are present, heavy
maths (wavelets, deconvolution, stacking) runs on the GPU. Otherwise the very
same code runs with NumPy on the CPU. Any GPU error (e.g. out of GPU memory)
makes the operation silently retry on the CPU, so a GPU problem can never
lose work.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import numpy as np

log = logging.getLogger("astroflow.gpu")
_lock = threading.Lock()
_cp: Any = None
_checked = False
_info: dict = {"available": False, "reason": "not checked"}
_enabled = True


def _probe() -> None:
    global _cp, _checked, _info
    with _lock:
        if _checked:
            return
        _checked = True
        try:
            import cupy as cp  # type: ignore
            n = cp.cuda.runtime.getDeviceCount()
            if n < 1:
                raise RuntimeError("no CUDA device")
            props = cp.cuda.runtime.getDeviceProperties(0)
            free, total = cp.cuda.runtime.memGetInfo()
            # Tiny smoke test so a broken install is detected now, not mid-job.
            a = cp.arange(16, dtype=cp.float32)
            float((a * 2).sum())
            _cp = cp
            name = props["name"].decode() if isinstance(props["name"], bytes) else str(props["name"])
            _info = {"available": True, "name": name, "memory_gb": round(total / 1024**3, 1),
                     "free_gb": round(free / 1024**3, 1), "backend": "CuPy " + cp.__version__}
            log.info("GPU acceleration available: %s", _info)
        except Exception as e:  # ImportError, CUDA errors, driver missing...
            _cp = None
            _info = {"available": False, "reason": f"{type(e).__name__}: {e}"[:200]}


def info() -> dict:
    _probe()
    return dict(_info, enabled=_enabled)


def set_enabled(flag: bool) -> None:
    global _enabled
    _enabled = bool(flag)


def xp() -> Any:
    """The array module to use right now: cupy (GPU) or numpy (CPU)."""
    _probe()
    return _cp if (_cp is not None and _enabled) else np


def on_gpu() -> bool:
    return xp() is not np


def to_xp(a: np.ndarray, mod: Any) -> Any:
    return a if mod is np else mod.asarray(a)


def to_host(a: Any) -> np.ndarray:
    if isinstance(a, np.ndarray):
        return a
    get = getattr(a, "get", None)
    return get() if get else np.asarray(a)


def run(fn: Callable[[Any], Any], *, what: str = "operation") -> Any:
    """Run ``fn(xp)`` on the GPU if possible, else (or on failure) on the CPU."""
    mod = xp()
    if mod is not np:
        try:
            out = fn(mod)
            return to_host(out) if not isinstance(out, tuple) else tuple(to_host(o) for o in out)
        except Exception as e:
            log.warning("GPU %s failed (%s); falling back to CPU", what, e)
            try:
                mod.get_default_memory_pool().free_all_blocks()
            except Exception:
                pass
    return fn(np)


def ndimage(mod: Any):
    if mod is np:
        import scipy.ndimage as nd
        return nd
    import cupyx.scipy.ndimage as nd  # type: ignore
    return nd
