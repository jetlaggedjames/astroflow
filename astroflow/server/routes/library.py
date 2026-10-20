"""Library (file index), clean-up, uploads, catalogue and planner."""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from ...config import settings, uploads_dir
from ...jobs import jobs
from ...library import cleanup
from ...library.index import library, preview_image, thumbnail
from ..common import is_local, jpeg, safe_path

router = APIRouter(prefix="/api")


@router.post("/library/scan")
def scan(body: dict | None = None):
    roots = (body or {}).get("roots")
    j = jobs.submit("scan", "Updating the library", lambda job: library().scan(roots, job.update, job.check), heavy=False)
    return {"job": j.id}


@router.get("/library/query")
def query(request: Request, text: str = "", kind: str = "", target: str = "", night: str = "", session: str = "",
          status: str = "", imagetype: str = "", camera: str = "", min_rating: int = 0, tag: str = "",
          date_from: str = "", date_to: str = "", sort: str = "date_obs", desc: bool = True, limit: int = 300,
          offset: int = 0, include_missing: bool = True):
    return library().query(text, kind, target, night, session, status, imagetype, camera, min_rating, tag, date_from,
                           date_to, sort=sort, desc=desc, limit=min(limit, 2000), offset=offset, include_missing=include_missing)


@router.get("/library/facets")
def facets():
    return library().facets()


@router.get("/library/roots")
def roots():
    return {"roots": library().roots()}


@router.get("/library/file/{file_id}")
def file_info(file_id: int):
    rec = library().get(file_id)
    if not rec:
        raise HTTPException(404, "Unknown file")
    from ...io.images import read_header
    meta = read_header(rec["path"]) if not rec["missing"] else {}
    rec["header"] = {k: v for k, v in meta.items() if not str(k).startswith("_") and k != "HISTORY"}
    rec["history"] = meta.get("HISTORY", [])
    from ...solve.wcs import Solution
    sol = Solution.load_sidecar(rec["path"])
    rec["solution"] = sol.to_dict() if sol else None
    return rec


@router.patch("/library/file/{file_id}")
def file_update(file_id: int, body: dict):
    return library().update(file_id, **body)


@router.get("/library/thumb/{file_id}")
def thumb(file_id: int, size: int = 320):
    p = thumbnail(file_id, size)
    if not p:
        raise HTTPException(404, "No preview")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@router.get("/image/preview")
def image_preview(request: Request, path: str, max: int = 1600, frame: int | None = None, stretch: bool = True):
    p = safe_path(request, path)
    img = preview_image(str(p), max_size=max, frame=frame, stretch=stretch)
    return Response(jpeg(img), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/library/samples")
def samples(request: Request, path: str, n: int = 6):
    """A few evenly spaced frames of a (huge) video, for a quick look without reading the whole file."""
    from ...io.source import open_source
    p = safe_path(request, path)
    src = open_source(p)
    try:
        count = src.count
    finally:
        src.close()
    idx = sorted({int(round(i * (count - 1) / max(n - 1, 1))) for i in range(n)}) if count else []
    return {"frames": count, "indices": idx}


# ------------------------------------------------------------------ cleanup
@router.post("/cleanup/duplicates")
def duplicates():
    j = jobs.submit("cleanup", "Looking for duplicate files", lambda job: cleanup.find_duplicates(job.check), heavy=False)
    return {"job": j.id}


@router.get("/cleanup/problems")
def problems():
    return cleanup.find_problems()


@router.get("/cleanup/space")
def space():
    return cleanup.space_report()


def _safety(fn, *a):
    try:
        return fn(*a)
    except cleanup.SafetyError as e:
        raise HTTPException(409, str(e))


@router.post("/cleanup/plan/trash")
def plan_trash(body: dict):
    return _safety(cleanup.plan_trash, body.get("ids", []), bool(body.get("include_raw", False)))


@router.post("/cleanup/plan/offload")
def plan_offload(body: dict, request: Request):
    if not is_local(request):
        raise HTTPException(403, "Offloading is done on the main computer")
    return _safety(cleanup.plan_offload, body.get("ids", []), body.get("dest", ""))


@router.post("/cleanup/plan/remove_offloaded")
def plan_remove(body: dict):
    return _safety(cleanup.plan_remove_offloaded, body.get("ids", []))


@router.post("/cleanup/execute")
def execute(body: dict):
    token = body.get("token", "")
    _safety(cleanup.get_plan, token)          # fail fast with a clear message
    j = jobs.submit("cleanup", "Applying the confirmed clean-up", lambda job: cleanup.execute(token, job), heavy=False)
    return {"job": j.id}


@router.post("/cleanup/cancel")
def cancel(body: dict):
    cleanup.cancel(body.get("token", ""))
    return {"ok": True}


@router.get("/cleanup/events")
def events():
    return library().events()


# ------------------------------------------------------------------ uploads
@router.post("/upload")
async def upload(file: UploadFile = File(...), subfolder: str = Form("")):
    """Accept a file from another computer (or drag-and-drop) into the uploads folder and index it."""
    name = Path(file.filename or "upload.bin").name
    day = dt.date.today().isoformat()
    folder = uploads_dir() / day / "".join(c for c in subfolder if c.isalnum() or c in "-_ ")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    i = 1
    while dest.exists():
        dest = folder / f"{Path(name).stem}_{i}{Path(name).suffix}"
        i += 1
    part = dest.with_name(dest.name + ".part")
    with open(part, "wb") as f:
        while True:
            chunk = await file.read(8 << 20)
            if not chunk:
                break
            f.write(chunk)
    part.replace(dest)
    ids = library().add_paths([str(dest)])
    return {"path": str(dest), "ids": ids, "size": dest.stat().st_size}


@router.get("/download")
def download(request: Request, path: str):
    p = safe_path(request, path)
    return FileResponse(p, filename=p.name)


# --------------------------------------------------------------- catalogue
@router.get("/catalog/search")
def cat_search(q: str, limit: int = 20):
    from ...astro.catalog import Catalog
    return Catalog.default().search(q, limit)


@router.get("/catalog/tonight")
def tonight(date: str | None = None, interests: str = "planet,moon,galaxy,nebula,cluster", min_alt: float = 25.0,
            limit: int = 40):
    from zoneinfo import ZoneInfo
    from ...astro.planner import tonight as plan
    from ...solve.solver import field_of_view
    s = settings()
    lat, lon = s.get("location.lat"), s.get("location.lon")
    if lat is None or lon is None:
        raise HTTPException(400, "Set your observing location first (setup wizard or Settings).")
    try:
        tz = ZoneInfo(s.get("location.tz") or "UTC")
    except Exception:
        tz = dt.timezone.utc
    d = dt.date.fromisoformat(date) if date else None
    return plan(float(lat), float(lon), tz, d, field_of_view(), interests.split(","), min_alt, limit)


@router.get("/catalog/position")
def position(name: str):
    """Where a named object is right now (RA/Dec, altitude/azimuth) plus field-rotation advice."""
    from ...astro.catalog import Catalog
    from ...astro.coords import format_dec, format_ra, radec_to_altaz
    from ...astro.ephemeris import body_position
    from ...astro.planner import rotation_advice
    s = settings()
    now = dt.datetime.now(dt.timezone.utc)
    low = name.lower()
    if low in ("moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "sun"):
        p = body_position(low, now)
        ra, dec, label, extra = p["ra"], p["dec"], p["name"], {"diameter_arcsec": p["diameter_arcsec"], **({"illumination": p.get("illumination")} if low == "moon" else {})}
    else:
        o = Catalog.default().get(name)
        if not o:
            raise HTTPException(404, f"Unknown object {name}")
        ra, dec, label, extra = o.ra, o.dec, o.label, o.to_dict()
    out = {**extra, "name": label, "ra": ra, "dec": dec, "ra_str": format_ra(ra), "dec_str": format_dec(dec)}
    lat, lon = s.get("location.lat"), s.get("location.lon")
    if lat is not None and lon is not None:
        alt, az = radec_to_altaz(ra, dec, now, float(lat), float(lon))
        out.update({"alt": float(alt), "az": float(az)})
        out["rotation"] = rotation_advice(ra, dec, now, float(lat), float(lon), int(s.get("equipment.sensor_w_px")),
                                          int(s.get("equipment.sensor_h_px")))
    return out
