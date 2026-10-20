"""First-run setup wizard helpers: field of view, downloads, drivers, location."""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from ...config import data_dir, settings
from ...jobs import jobs
from ...solve import astap
from ..common import is_local

router = APIRouter(prefix="/api/wizard")
log = logging.getLogger("astroflow.wizard")


@router.get("/fov")
def fov(focal_mm: float | None = None, pixel_um: float | None = None, width_px: int | None = None,
        height_px: int | None = None, barlow: float = 1.0):
    s = settings()
    f = (focal_mm or s.get("equipment.focal_length_mm")) * barlow
    px = pixel_um or s.get("equipment.pixel_um")
    w = width_px or s.get("equipment.sensor_w_px")
    h = height_px or s.get("equipment.sensor_h_px")
    scale = 206.265 * px / f
    fw, fh = w * scale / 3600, h * scale / 3600
    rec = astap.recommend_database(fw, fh)
    return {"scale_arcsec": round(scale, 3), "fov_deg": [round(fw, 3), round(fh, 3)],
            "fov_arcmin": [round(fw * 60, 1), round(fh * 60, 1)], "recommended": rec,
            "databases": {k: {"min_fov": v[0], "max_fov": v[1], "size_mb": v[2], "text": v[3]} for k, v in astap.DATABASES.items()},
            "explain": ("Plate solving matches the pattern of stars in your photo against a star catalogue to work out exactly "
                        "where the telescope was pointing. It needs a star database sized for your field of view; it is "
                        "downloaded once and then works offline.")}


@router.get("/solver")
def solver_state():
    from ...solve.solver import status
    return status()


def _download(job, url: str, dest: Path, label: str) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "AstroFlow"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        t0 = time.time()
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            got += len(b)
            speed = got / max(time.time() - t0, 1e-3)
            job.update(got / total * 0.9 if total else 0.5,
                       f"Downloading {label}: {got / 1e6:.0f}{f' of {total / 1e6:.0f}' if total else ''} MB ({speed / 1e6:.1f} MB/s)")
    tmp.replace(dest)
    return dest


@router.post("/install_astap")
def install_astap(body: dict, request: Request):
    """Download the ASTAP command-line solver and/or a star database (needs internet once)."""
    if not is_local(request):
        raise HTTPException(403, "Only on the main computer")
    want_cli = bool(body.get("cli", True))
    db = body.get("database")
    if db and db not in astap.DOWNLOADS:
        raise HTTPException(400, "Unknown database")

    def run(job):
        target = astap.default_install_dir()
        target.mkdir(parents=True, exist_ok=True)
        out = {}
        if want_cli:
            key = "cli_win64" if sys.platform.startswith("win") else "cli_linux"
            z = _download(job, astap.DOWNLOADS[key], target / "astap_cli.zip", "ASTAP solver")
            with zipfile.ZipFile(z) as zf:
                zf.extractall(target)
            z.unlink()
            for exe in target.glob("astap_cli*"):
                exe.chmod(0o755)
            out["cli"] = str(astap.find_astap())
            settings().set("solver.astap_path", out["cli"] or "")
        if db:
            url = astap.DOWNLOADS[db]
            if url.endswith(".exe/download"):
                inst = _download(job, url, target / f"{db}_installer.exe", f"{db.upper()} star database")
                out["installer"] = str(inst)
                out["note"] = "Run the downloaded installer to finish (it installs into the ASTAP folder)."
                if sys.platform.startswith("win"):
                    subprocess.Popen([str(inst)])
            else:
                z = _download(job, url, target / f"{db}.zip", f"{db.upper()} star database")
                job.update(0.92, "Unpacking the star database")
                with zipfile.ZipFile(z) as zf:
                    zf.extractall(target)
                z.unlink()
                settings().set("solver.astap_db_dir", str(target))
                out["databases"] = astap.installed_databases(target)
        return out

    j = jobs.submit("download", "Installing plate-solving tools", run, heavy=False)
    return {"job": j.id}


@router.get("/playerone")
def playerone_scan():
    from ...hardware.camera import playerone
    paths = [str(p) for p in playerone.candidate_paths()]
    out = {"found": paths, "configured": settings().get("camera.playerone_dll", ""),
           "help": ("The Player One camera library (PlayerOneCamera.dll) is installed by SharpCap, N.I.N.A., FireCapture or "
                    "Player One's own software. If none is found, download the 'Camera SDK' zip for Windows from "
                    "player-one-astronomy.com (Service > Software) and give it to AstroFlow here.")}
    if paths:
        try:
            out["cameras"] = playerone.list_cameras()
            out["sdk_version"] = playerone._SDK.get().version()
        except Exception as e:
            out["error"] = str(e)
    return out


@router.post("/playerone/upload")
async def playerone_upload(request: Request, file: UploadFile = File(...)):
    if not is_local(request):
        raise HTTPException(403, "Only on the main computer")
    from ...hardware.camera import playerone
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
        shutil.copyfileobj(file.file, tf)
        tmp = tf.name
    try:
        p = playerone.install_from_sdk_zip(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)
    settings().set("camera.playerone_dll", str(p))
    return {"installed": str(p)}


@router.get("/gpu_check")
def gpu_check():
    from ... import gpu
    info = gpu.info()
    nvidia = shutil.which("nvidia-smi") is not None
    name = None
    if nvidia:
        try:
            name = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            pass
    return {"cupy": info, "nvidia_driver": nvidia, "gpu": name,
            "advice": ("GPU acceleration is active." if info.get("available") else
                       "An NVIDIA GPU was found. Installing the optional GPU package (about 1 GB download) speeds up "
                       "sharpening, deconvolution and stacking." if nvidia else
                       "No NVIDIA GPU found - everything runs on the processor (CPU), just a bit slower.")}


@router.post("/install_gpu")
def install_gpu(request: Request):
    if not is_local(request):
        raise HTTPException(403, "Only on the main computer")

    def run(job):
        job.update(0.05, "Installing CuPy for CUDA 12 (this downloads about 1 GB)...")
        p = subprocess.run([sys.executable, "-m", "pip", "install", "cupy-cuda12x"], capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("GPU package install failed: " + p.stderr[-800:])
        return {"ok": True, "note": "Restart AstroFlow to use the GPU."}

    j = jobs.submit("download", "Installing GPU acceleration", run, heavy=False)
    return {"job": j.id}


@router.get("/cities")
def cities(q: str):
    from ...astro.catalog import search_cities
    return search_cities(q)


@router.get("/timezones")
def timezones():
    from zoneinfo import available_timezones
    return sorted(t for t in available_timezones() if "/" in t and not t.startswith(("Etc/", "SystemV", "posix", "right")))


@router.post("/finish")
def finish(body: dict):
    s = settings()
    patch = dict(body or {})
    patch["first_run_done"] = True
    s.update(patch)
    for k in ("captures", "projects", "exports"):
        try:
            s.folder(k)
        except OSError as e:
            raise HTTPException(400, f"Cannot create the {k} folder: {e}")
    return s.all()
