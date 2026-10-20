"""Plate solving with ASTAP (free, open source, MPL-2.0, by Han Kleijn - www.hnsky.org).

We never pass your original file to ASTAP. A temporary, solver-friendly copy
(mono, 16-bit FITS, optionally binned) is written and solved, so originals
are never touched.
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from ..config import data_dir
from .wcs import Solution

log = logging.getLogger("astroflow.astap")

DATABASES = {
    # name: (min fov deg, max fov deg, approx download MB, description)
    "d80": (0.15, 6.0, 1300, "8000 stars per square degree - best for long focal lengths (FOV 0.15-6 deg)"),
    "d50": (0.2, 6.0, 900, "5000 stars per square degree - FOV 0.2-6 deg"),
    "d20": (0.3, 6.0, 400, "2000 stars per square degree - FOV 0.3-6 deg"),
    "d05": (0.6, 6.0, 102, "500 stars per square degree - FOV 0.6-6 deg (small download)"),
    "g05": (3.0, 20.0, 102, "for wide fields 3-20 deg (camera lenses)"),
    "w08": (20.0, 180.0, 1, "for very wide fields > 20 deg"),
}
SF = "https://sourceforge.net/projects/astap-program/files"
DOWNLOADS = {
    "cli_win64": f"{SF}/windows_installer/astap_command-line_version_win64.zip/download",
    "cli_linux": f"{SF}/linux_installer/astap_command-line_version_Linux_amd64.zip/download",
    "d80": f"{SF}/star_databases/d80_star_database.exe/download",   # Windows installer (zip not offered for D80)
    "d50": f"{SF}/star_databases/d50_star_database.zip/download",
    "d20": f"{SF}/star_databases/d20_star_database.zip/download",
    "d05": f"{SF}/star_databases/d05_star_database.zip/download",
    "g05": f"{SF}/star_databases/g05_star_database.zip/download",
    "w08": f"{SF}/star_databases/w08_star_database_mag08_astap.zip/download",
}


def recommend_database(fov_w_deg: float, fov_h_deg: float) -> dict:
    """Which ASTAP star database suits this field of view (by its smaller side)."""
    f = min(fov_w_deg, fov_h_deg)
    if f < 0.15:
        return {"name": None, "text": (f"Your field of view ({fov_w_deg:.2f} x {fov_h_deg:.2f} deg) is too narrow to plate-solve "
                                       "reliably. Remove the Barlow for solving (the app can solve a quick frame without it).")}
    for name in ("d80", "d50", "d20", "d05", "g05", "w08"):
        lo, hi, mb, desc = DATABASES[name]
        if name in ("d80",) and f >= 0.3:
            continue  # d50/d20 are enough and smaller
        if name == "d50" and f >= 0.6:
            continue
        if lo <= f <= hi:
            return {"name": name, "size_mb": mb, "text": f"{name.upper()}: {desc}. Download about {mb} MB.",
                    "alternatives": [n for n in DATABASES if DATABASES[n][0] <= f <= DATABASES[n][1] and n != name]}
    return {"name": "w08", "size_mb": 1, "text": DATABASES["w08"][3]}


def default_install_dir() -> Path:
    return data_dir() / "astap"


def find_astap(configured: str = "") -> Path | None:
    """Locate the ASTAP executable: settings, our own data folder, common install places, PATH."""
    cands: list[Path] = []
    if configured:
        cands.append(Path(configured))
    own = default_install_dir()
    cands += [own / "astap_cli.exe", own / "astap_cli", own / "astap.exe", own / "astap"]
    if sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            cands += [Path(base) / "astap" / "astap_cli.exe", Path(base) / "astap" / "astap.exe"]
    else:
        cands += [Path("/opt/astap/astap_cli"), Path("/opt/astap/astap"), Path("/usr/local/opt/astap/astap_cli")]
    for name in ("astap_cli", "astap"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    for c in cands:
        if c.exists() and c.is_file():
            return c
    return None


def find_database_dir(exe: Path | None, configured: str = "") -> Path | None:
    """Folder containing ASTAP star database files (*.1476 / *.290)."""
    dirs = [Path(configured)] if configured else []
    dirs += [default_install_dir(), default_install_dir() / "db"]
    if exe is not None:
        dirs.append(exe.parent)
    if sys.platform.startswith("win"):
        dirs.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "astap")
    else:
        dirs += [Path("/opt/astap"), Path("/usr/local/opt/astap")]
    for d in dirs:
        if d.exists() and any(d.glob("*.1476")) or (d.exists() and any(d.glob("*.290"))):
            return d
    return None


def installed_databases(db_dir: Path | None) -> list[str]:
    if not db_dir or not db_dir.exists():
        return []
    names = set()
    for f in list(db_dir.glob("*.1476")) + list(db_dir.glob("*.290")):
        names.add(f.name[:3].lower())
    return sorted(names)


def _prepare(img: np.ndarray, max_size: int = 2400) -> tuple[np.ndarray, float]:
    """Mono 16-bit copy, binned down if very large (ASTAP likes ~1000-2500 px)."""
    mono = img.mean(axis=2) if img.ndim == 3 else img
    mono = np.nan_to_num(mono.astype(np.float32))
    # Keep the data linear with a small pedestal: ASTAP's star detection needs a real
    # background with noise, not a background clipped to black.
    lo = float(np.percentile(mono, 0.1))
    hi = float(np.percentile(mono, 99.99))
    mono = np.clip((mono - lo) / max(hi - lo, 1e-9), 0, 1) * 0.9 + 0.02
    factor = 1.0
    while max(mono.shape) > max_size:
        mono = cv2.resize(mono, (mono.shape[1] // 2, mono.shape[0] // 2), interpolation=cv2.INTER_AREA)
        factor *= 2
    return (mono * 65535).astype(np.uint16), factor


def parse_ini(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'").strip()
        try:
            out[k] = float(v) if v not in ("T", "F") else (v == "T")
        except ValueError:
            out[k] = v
    return out


def parse_wcs_file(path: Path) -> dict:
    """ASTAP's .wcs output is a FITS header (80-char cards, no data)."""
    raw = path.read_bytes()
    cards = [raw[i:i + 80].decode("ascii", "replace") for i in range(0, len(raw), 80)]
    out = {}
    for c in cards:
        key = c[:8].strip()
        if key == "END":
            break
        if c[8:10] != "= ":
            continue
        val = c[10:].split("/")[0].strip() if not c[10:].strip().startswith("'") else c[10:].split("'")[1].strip()
        try:
            out[key] = float(val) if not val.startswith("'") else val
        except ValueError:
            out[key] = val.strip("'").strip()
        if out[key] in ("T", "F"):
            out[key] = out[key] == "T"
    return out


def solve(img: np.ndarray, exe: Path, db_dir: Path | None = None, hint: dict | None = None,
          fov_h_deg: float | None = None, timeout: float = 120.0, search_radius: float | None = None) -> Solution | None:
    """Solve an image. ``hint`` = {"ra": deg, "dec": deg} (optional). Returns None if unsolved."""
    from astropy.io import fits
    H, W = img.shape[:2]
    data, factor = _prepare(img)
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="astroflow-solve-") as td:
        f = Path(td) / "solve.fits"
        fits.PrimaryHDU(data).writeto(f)
        cmd = [str(exe), "-f", str(f), "-wcs"]
        if fov_h_deg:
            cmd += ["-fov", f"{fov_h_deg:.4f}"]
        else:
            cmd += ["-fov", "0"]  # 0 = let ASTAP try a range of field sizes
        if hint and hint.get("ra") is not None and hint.get("dec") is not None:
            cmd += ["-ra", f"{float(hint['ra']) / 15:.5f}", "-spd", f"{float(hint['dec']) + 90:.5f}",
                    "-r", f"{search_radius or 15:.1f}"]
        else:
            cmd += ["-r", "180"]
        if db_dir:
            cmd += ["-d", str(db_dir)]
        cmd += ["-z", "0"]
        log.info("Running ASTAP: %s", " ".join(cmd))
        try:
            kw = {}
            if sys.platform.startswith("win"):
                kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        except subprocess.TimeoutExpired:
            log.warning("ASTAP timed out")
            return None
        ini = Path(td) / "solve.ini"
        wcsf = Path(td) / "solve.wcs"
        info = parse_ini(ini.read_text(encoding="utf-8", errors="replace")) if ini.exists() else {}
        if not info.get("PLTSOLVD") or not wcsf.exists():
            log.info("ASTAP did not solve (exit %s): %s %s", proc.returncode, info.get("WARNING", ""), proc.stdout[-300:])
            return None
        hdr = parse_wcs_file(wcsf)
    # Scale the solution back to the original (un-binned) image size.
    if factor != 1.0:
        for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2", "CDELT1", "CDELT2"):
            if k in hdr:
                hdr[k] = float(hdr[k]) / factor
        for k in ("CRPIX1", "CRPIX2"):
            if k in hdr:
                hdr[k] = (float(hdr[k]) - 0.5) * factor + 0.5
        for k in list(hdr):
            if k.startswith(("A_", "B_", "AP_", "BP_")) and "ORDER" not in k:
                hdr.pop(k)  # drop SIP terms rather than rescale them
    sol = Solution.from_header(hdr, W, H, solver="ASTAP")
    sol.seconds = round(time.time() - t0, 2)
    return sol
