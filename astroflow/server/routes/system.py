"""Settings, background jobs, logs, folder browsing, network sharing."""
from __future__ import annotations

import hmac
import io
import os
import shutil
import string
import sys
import zipfile
from pathlib import Path

import psutil
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from ... import SOFTWARE_ID, __version__, gpu
from ...config import app_home, settings
from ...jobs import jobs
from ...logsetup import log_file, tail
from ..common import allowed_roots, is_local, local_ips

router = APIRouter(prefix="/api")

LOCAL_ONLY_SETTINGS = {"camera", "mount", "network", "folders", "solver"}


@router.get("/status")
def status(request: Request):
    vm = psutil.virtual_memory()
    proc = psutil.Process()
    return {
        "app": SOFTWARE_ID, "version": __version__, "local": is_local(request), "platform": sys.platform,
        "mode": settings().get("mode"), "theme": settings().get("theme"), "first_run_done": settings().get("first_run_done"),
        "memory": {"process": proc.memory_info().rss, "total": vm.total, "available": vm.available},
        "cpu_count": os.cpu_count(), "gpu": gpu.info(),
        "jobs": [j.to_dict(include_result=False) for j in jobs.active()],
    }


@router.get("/settings")
def get_settings(request: Request):
    s = settings().all()
    if not is_local(request):
        s["network"] = {"allow_lan": s["network"]["allow_lan"]}
        s["solver"] = {k: v for k, v in s["solver"].items() if k != "astrometry_api_key"}
    return s


@router.patch("/settings")
def patch_settings(patch: dict, request: Request):
    if not is_local(request):
        blocked = set(patch) & LOCAL_ONLY_SETTINGS
        if blocked:
            raise HTTPException(403, "These settings can only be changed on the main computer: " + ", ".join(sorted(blocked)))
    if "gpu" in patch and "enabled" in patch["gpu"]:
        gpu.set_enabled(bool(patch["gpu"]["enabled"]))
    return settings().update(patch)


# ---------------------------------------------------------------------- jobs
@router.get("/jobs")
def list_jobs():
    return [j.to_dict(include_result=False) for j in jobs.list()[:30]]


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "No such task")
    d = j.to_dict()
    if j.error_detail:
        d["error_detail"] = j.error_detail[-3000:]
    return d


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    return {"cancelled": jobs.cancel(job_id)}


# ---------------------------------------------------------------------- logs
@router.get("/logs", response_class=PlainTextResponse)
def logs(lines: int = 400):
    return tail(lines)


@router.get("/logs/download")
def logs_download():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(log_file().parent.glob("astroflow.log*")):
            z.write(f, f.name)
        s = settings().all()
        s["solver"]["astrometry_api_key"] = "***" if s["solver"].get("astrometry_api_key") else ""
        s["network"]["access_code"] = "***"
        z.writestr("settings.json", __import__("json").dumps(s, indent=1))
        z.writestr("system.txt", f"{SOFTWARE_ID}\nPython {sys.version}\nPlatform {sys.platform}\nGPU {gpu.info()}\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=astroflow-bug-report.zip"})


# ---------------------------------------------------------------- folders
@router.get("/fs/list")
def fs_list(request: Request, path: str = "", show_files: bool = True):
    """Folder browser. Other computers only see the shared library folders."""
    local = is_local(request)
    if not path:
        if local:
            roots = []
            if sys.platform.startswith("win"):
                for d in string.ascii_uppercase:
                    if os.path.exists(f"{d}:\\"):
                        roots.append({"name": f"{d}:\\", "path": f"{d}:\\", "dir": True})
            else:
                roots.append({"name": "/", "path": "/", "dir": True})
            home = Path.home()
            roots.insert(0, {"name": "Home", "path": str(home), "dir": True})
            for r in allowed_roots():
                roots.append({"name": r.name or str(r), "path": str(r), "dir": True})
            return {"path": "", "parent": None, "entries": roots}
        return {"path": "", "parent": None, "entries": [{"name": r.name, "path": str(r), "dir": True} for r in allowed_roots() if r.exists()]}
    p = Path(path)
    if not local and not any(p.resolve() == r or r in p.resolve().parents for r in allowed_roots()):
        raise HTTPException(403, "Not shared")
    if not p.is_dir():
        raise HTTPException(404, "Folder not found")
    entries = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower())):
            if e.name.startswith(".") or e.name.startswith("$"):
                continue
            try:
                is_dir = e.is_dir()
                if not is_dir and not show_files:
                    continue
                entries.append({"name": e.name, "path": e.path, "dir": is_dir, "size": None if is_dir else e.stat().st_size})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "Windows did not allow reading that folder")
    try:
        usage = shutil.disk_usage(p)
        free = {"free": usage.free, "total": usage.total}
    except OSError:
        free = None
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "entries": entries[:2000], "disk": free}


@router.post("/fs/mkdir")
def fs_mkdir(body: dict, request: Request):
    if not is_local(request):
        raise HTTPException(403, "Only on the main computer")
    p = Path(body["path"])
    p.mkdir(parents=True, exist_ok=True)
    return {"path": str(p)}


@router.post("/fs/reveal")
def fs_reveal(body: dict, request: Request):
    """Open a folder (or the folder containing a file) in Explorer - only on the main computer."""
    if not is_local(request):
        raise HTTPException(403, "Only on the main computer")
    p = Path(body.get("path", ""))
    if not p.exists():
        raise HTTPException(404, "Not found")
    import subprocess
    try:
        if sys.platform.startswith("win"):
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if p.is_file() else "", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
    except Exception as e:
        raise HTTPException(400, f"Could not open the folder: {e}")
    return {"ok": True}


@router.get("/fs/space")
def fs_space(path: str):
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    u = shutil.disk_usage(p)
    return {"path": str(p), "free": u.free, "total": u.total}


# ---------------------------------------------------------------- network
@router.get("/network")
def network(request: Request):
    s = settings()
    port = s.get("network.port", 8765)
    out = {"allow_lan": s.get("network.allow_lan"), "port": port,
           "addresses": [f"http://{ip}:{port}" for ip in local_ips()]}
    if is_local(request):
        out["access_code"] = s.get("network.access_code")
    return out


@router.post("/auth")
def auth(body: dict, request: Request):
    if not hmac.compare_digest(str(body.get("code", "")).strip(), str(settings().get("network.access_code"))):
        raise HTTPException(401, "Wrong access code")
    r = Response('{"ok": true}', media_type="application/json")
    r.set_cookie("af_access", str(settings().get("network.access_code")), httponly=True, samesite="strict", max_age=30 * 86400)
    return r


@router.get("/gpu")
def gpu_info():
    return gpu.info()


@router.post("/shutdown")
def shutdown(request: Request):
    if not is_local(request):
        raise HTTPException(403, "Only on the main computer")
    if jobs.active():
        raise HTTPException(409, "Tasks are still running - cancel them or wait first.")
    import threading
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True}
