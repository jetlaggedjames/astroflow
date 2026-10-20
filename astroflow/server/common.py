"""Shared helpers for the web API: local-vs-remote access, path safety, image cache."""
from __future__ import annotations

import ipaddress
import os
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from fastapi import HTTPException, Request

from ..config import settings, uploads_dir

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host in LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_local(request: Request) -> None:
    """Camera and mount control only from the computer they are plugged into."""
    if not is_local(request):
        raise HTTPException(403, "Camera and mount can only be controlled on the computer they are connected to.")


def allowed_roots() -> list[Path]:
    s = settings()
    roots = [Path(p) for p in (s.get("folders.library_roots") or [])]
    for k in ("captures", "projects", "exports"):
        if s.get(f"folders.{k}"):
            roots.append(Path(s.get(f"folders.{k}")))
    roots.append(uploads_dir())
    return [r.resolve() for r in roots]


def safe_path(request: Request, path: str, must_exist: bool = True) -> Path:
    """Validate a file path from the UI. Remote users may only touch library folders and uploads."""
    if not path:
        raise HTTPException(400, "No file given")
    p = Path(path).expanduser()
    try:
        rp = p.resolve()
    except OSError:
        raise HTTPException(400, "Invalid path")
    if not is_local(request):
        if not any(rp == r or r in rp.parents for r in allowed_roots()):
            raise HTTPException(403, "That location is not shared with other computers")
    if must_exist and not rp.exists():
        raise HTTPException(404, f"Not found: {p.name}")
    return rp


class ImageCache:
    """Keeps a few decoded full-resolution images in memory (keyed by path + modification time)."""

    def __init__(self, max_items: int = 4):
        self.max_items = max_items
        self._d: OrderedDict[str, tuple[np.ndarray, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: str | Path) -> tuple[np.ndarray, dict, str]:
        from ..io.debayer import debayer
        from ..io.images import read_image, to_float
        p = Path(path)
        st = p.stat()
        key = f"{p}|{st.st_mtime_ns}|{st.st_size}"
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                img, meta = self._d[key]
                return img, meta, key
        data, meta = read_image(p)
        pat = str(meta.get("BAYERPAT") or "").upper()
        if data.ndim == 2 and pat in ("RGGB", "BGGR", "GRBG", "GBRG") and data.dtype != np.float32:
            data = debayer(data, pat)
        img = to_float(data)
        with self._lock:
            self._d[key] = (img, meta)
            while len(self._d) > self.max_items:
                self._d.popitem(last=False)
        return img, meta, key


images = ImageCache()


def local_ips() -> list[str]:
    import socket
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def jpeg(img8: np.ndarray, quality: int = 88) -> bytes:
    import cv2
    if img8.ndim == 3:
        img8 = cv2.cvtColor(img8, cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".jpg", img8, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes()


def to8(img: np.ndarray) -> np.ndarray:
    a = np.clip(np.nan_to_num(img), 0, 1)
    if a.ndim == 2:
        a = np.repeat(a[..., None], 3, axis=2)
    return (a * 255 + 0.5).astype(np.uint8)


def histogram(img: np.ndarray, bins: int = 256) -> dict:
    a = np.clip(np.nan_to_num(img), 0, 1)
    step = max(1, int((a.shape[0] * a.shape[1] / 300_000) ** 0.5))
    a = a[::step, ::step]
    if a.ndim == 2:
        return {"L": np.histogram(a, bins=bins, range=(0, 1))[0].tolist()}
    out = {c: np.histogram(a[..., i], bins=bins, range=(0, 1))[0].tolist() for i, c in enumerate("RGB")}
    out["clipped_low"] = float(np.mean(np.all(a <= 0.0005, axis=2)) * 100)
    out["clipped_high"] = float(np.mean(np.any(a >= 0.9995, axis=2)) * 100)
    return out


def file_size_str(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)
