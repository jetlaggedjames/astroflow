"""Optional online plate solving via Astrometry.net (nova.astrometry.net).

Only used when you switch on "Allow online solving" and enter your free API
key from nova.astrometry.net. A small, reduced copy of the image is
uploaded, marked as private (not publicly visible, no commercial use).
"""
from __future__ import annotations

import io
import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
from typing import Callable

import numpy as np

from .wcs import Solution

log = logging.getLogger("astroflow.online")
API = "https://nova.astrometry.net/api/"
WCS_URL = "https://nova.astrometry.net/wcs_file/{job}"


class OnlineSolveError(RuntimeError):
    pass


def _post(url: str, data: dict, timeout: float = 60) -> dict:
    body = urllib.parse.urlencode({"request-json": json.dumps(data)}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url: str, timeout: float = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _multipart(fields: dict, file_name: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = "----AstroFlow" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_name}\"\r\n"
              "Content-Type: application/octet-stream\r\n\r\n".encode())
    out.write(file_bytes)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), boundary


def _jpeg(img: np.ndarray, max_size: int = 1600) -> tuple[bytes, float]:
    import cv2
    mono = img.mean(axis=2) if img.ndim == 3 else img
    lo, hi = np.percentile(mono, 1), np.percentile(mono, 99.8)
    m8 = np.clip((mono - lo) / max(hi - lo, 1e-9) * 255, 0, 255).astype(np.uint8)
    f = 1.0
    while max(m8.shape) > max_size:
        m8 = cv2.resize(m8, (m8.shape[1] // 2, m8.shape[0] // 2), interpolation=cv2.INTER_AREA)
        f *= 2
    ok, buf = cv2.imencode(".jpg", m8, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return buf.tobytes(), f


def solve(img: np.ndarray, api_key: str, hint: dict | None = None, scale_arcsec: float | None = None,
          timeout: float = 300, progress: Callable[[float, str], None] | None = None,
          check: Callable[[], None] | None = None, api: str = API) -> Solution | None:
    if not api_key:
        raise OnlineSolveError("No Astrometry.net API key set (get a free one at nova.astrometry.net)")
    prog = progress or (lambda f, m=None: None)
    H, W = img.shape[:2]
    t0 = time.time()
    prog(0.05, "Logging in to Astrometry.net")
    login = _post(api + "login", {"apikey": api_key})
    if login.get("status") != "success":
        raise OnlineSolveError(f"Astrometry.net login failed: {login.get('errormessage', login)}")
    session = login["session"]
    data, factor = _jpeg(img)
    req = {"session": session, "allow_commercial_use": "n", "allow_modifications": "n", "publicly_visible": "n"}
    if scale_arcsec:
        req.update({"scale_units": "arcsecperpix", "scale_type": "ev", "scale_est": scale_arcsec * factor, "scale_err": 25})
    if hint and hint.get("ra") is not None:
        req.update({"center_ra": float(hint["ra"]), "center_dec": float(hint["dec"]), "radius": 10.0})
    body, boundary = _multipart({"request-json": json.dumps(req)}, "image.jpg", data)
    prog(0.15, "Uploading a small private copy of the image")
    r = urllib.request.Request(api + "upload", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        up = json.loads(resp.read().decode())
    if up.get("status") != "success":
        raise OnlineSolveError(f"Upload failed: {up}")
    subid = up["subid"]
    job = None
    while time.time() - t0 < timeout:
        if check:
            check()
        time.sleep(3)
        sub = json.loads(_get(api + f"submissions/{subid}").decode())
        jobs = [j for j in (sub.get("jobs") or []) if j]
        if jobs:
            job = jobs[0]
            st = json.loads(_get(api + f"jobs/{job}").decode())
            if st.get("status") == "success":
                break
            if st.get("status") == "failure":
                return None
        prog(min(0.9, 0.2 + (time.time() - t0) / timeout), "Waiting for Astrometry.net to solve...")
    else:
        raise OnlineSolveError("Astrometry.net took too long")
    raw = _get(WCS_URL.format(job=job) if api == API else api.replace("/api/", "/") + f"wcs_file/{job}")
    from .astap import parse_wcs_file
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile(suffix=".wcs", delete=False) as tf:
        tf.write(raw)
        p = Path(tf.name)
    try:
        hdr = parse_wcs_file(p)
    finally:
        p.unlink(missing_ok=True)
    if factor != 1.0:
        for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2"):
            if k in hdr:
                hdr[k] = float(hdr[k]) / factor
        for k in ("CRPIX1", "CRPIX2"):
            if k in hdr:
                hdr[k] = (float(hdr[k]) - 0.5) * factor + 0.5
        for k in list(hdr):
            if k.startswith(("A_", "B_", "AP_", "BP_")):
                hdr.pop(k)
    sol = Solution.from_header(hdr, W, H, solver="Astrometry.net")
    sol.seconds = round(time.time() - t0, 1)
    return sol
