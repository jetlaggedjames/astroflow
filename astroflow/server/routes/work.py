"""Projects, Analyze, Stack, Solve, Post-process, Export and the one-click workflow."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ... import export as exporter
from ... import project as projects
from ...config import atomic_write_text, cache_dir, settings
from ...imaging import dso, lucky, mosaic, quality
from ...io.images import read_header, write_fits, write_tiff
from ...io.source import expand_paths, open_source
from ...jobs import jobs
from ...library.index import library, preview_image
from ...process import pipeline
from ...process.ops import CATEGORIES, REGISTRY
from ..common import histogram, images, is_local, jpeg, safe_path, to8

log = logging.getLogger("astroflow.api.work")
router = APIRouter(prefix="/api")


def _paths(request: Request, items: list[str]) -> list[str]:
    return [str(safe_path(request, p)) for p in items]


def _project(pid: str | None) -> dict | None:
    if not pid:
        return None
    try:
        return projects.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "Project not found")


# ================================================================ projects ===
@router.get("/projects")
def list_projects():
    return projects.list_projects()


@router.post("/projects")
def create_project(body: dict, request: Request):
    srcs = _paths(request, body.get("sources", [])) if body.get("sources") else []
    name = body.get("name") or (Path(srcs[0]).stem if srcs else f"Project {dt.date.today()}")
    return projects.new_project(name, srcs, body.get("target") or {})


@router.get("/projects/{pid}")
def get_project(pid: str):
    return _project(pid)


@router.patch("/projects/{pid}")
def patch_project(pid: str, body: dict, request: Request):
    p = _project(pid)
    if "sources" in body:
        body["sources"] = _paths(request, body["sources"])
    return projects.update(p, **body)


@router.post("/projects/{pid}/history/commit")
def history_commit(pid: str, body: dict):
    p = _project(pid)
    if body.get("base"):
        p["process"]["base"] = body["base"]
    return projects.commit_process(p, body.get("steps", []), body.get("masks", {}), body.get("label", "edit"))["process"]


@router.post("/projects/{pid}/history/{action}")
def history_move(pid: str, action: str, body: dict | None = None):
    p = _project(pid)
    if action == "undo":
        projects.undo(p)
    elif action == "redo":
        projects.redo(p)
    elif action == "jump":
        projects.jump(p, int((body or {}).get("index", -1)))
    else:
        raise HTTPException(400, "Unknown action")
    return p["process"]


# ================================================================= analyze ===
def _report_key(paths: list[str], kind: str) -> str:
    h = hashlib.sha1(kind.encode())
    for p in paths:
        st = Path(p).stat()
        h.update(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()[:20]


def _report_file(key: str) -> Path:
    d = cache_dir() / "analysis"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


@router.post("/analyze")
def analyze(body: dict, request: Request):
    srcs = _paths(request, body.get("sources", []))
    if not srcs:
        raise HTTPException(400, "Choose a recording or some image files to analyse")
    kind = body.get("kind", "auto")
    key = _report_key(srcs, kind)
    rf = _report_file(key)
    if rf.exists() and not body.get("force"):
        return {"key": key, "cached": True}
    pid = body.get("project_id")

    def run(job):
        src = open_source(srcs)
        try:
            job.update(0.01, f"Opening {src.count:,} frames")
            rep = quality.analyze(src, kind, job.update, threads=settings().threads())
            files = [str(x) for x in getattr(src, "paths", [])]
        finally:
            src.close()
        rep["sources"] = srcs
        if len(files) > 1:
            rep["files"] = files      # frame i of the report = files[i] (folders are expanded)
        rep["key"] = key
        atomic_write_text(rf, json.dumps(rep))
        lib = library()
        # Store a quality figure per file in the library (still images = one value each).
        if len(srcs) == len(rep["frames"]):
            for path, q in zip(srcs, rep["quality"]):
                rec = lib.get_by_path(path)
                if rec:
                    lib.update(rec["id"], quality=q)
        elif len(srcs) == 1:
            rec = lib.get_by_path(srcs[0])
            if rec:
                lib.update(rec["id"], quality=float(rep["summary"]["median_quality"]))
        if pid:
            p = projects.load(pid)
            projects.update(p, analysis={"key": key, "kind": rep["kind"], "summary": rep["summary"]},
                            sources=srcs, selection={"mode": "percent", "value": rep["recommendation"]["keep_percent"]})
        return {"key": key, "summary": rep["summary"], "recommendation": rep["recommendation"]}

    j = jobs.submit("analyze", f"Analysing {Path(srcs[0]).name}{' and others' if len(srcs) > 1 else ''}", run)
    return {"job": j.id, "key": key}


@router.get("/analyze/report/{key}")
def report(key: str):
    rf = _report_file(key)
    if not rf.exists():
        raise HTTPException(404, "Analysis not found - run Analyze again")
    return Response(rf.read_bytes(), media_type="application/json")


@router.post("/analyze/select")
def select(body: dict):
    rep = json.loads(_report_file(body["key"]).read_text())
    sel = quality.select_frames(rep, body.get("mode", "percent"), float(body.get("value", 25)),
                                body.get("include"), body.get("exclude"))
    q = np.array(rep["quality"])
    pos = {f: i for i, f in enumerate(rep["frames"])}
    kept_q = [q[pos[i]] for i in sel if i in pos]
    return {"count": len(sel), "frames": sel, "mean_quality": float(np.mean(kept_q)) if kept_q else 0.0,
            "noise_reduction_x": round(float(np.sqrt(max(len(sel), 1))), 1)}


@router.get("/analyze/frame")
def frame(request: Request, key: str, index: int, max: int = 900):
    rep = json.loads(_report_file(key).read_text())
    files = rep.get("files") or [str(x) for x in expand_paths(rep["sources"])]
    if len(files) > 1:
        img = preview_image(str(safe_path(request, files[index])), max)
    else:
        img = preview_image(str(safe_path(request, files[0])), max, frame=index)
    return Response(jpeg(img), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# =================================================================== stack ===
@router.post("/stack/estimate")
def estimate(body: dict, request: Request):
    import shutil
    srcs = _paths(request, body.get("sources", []))
    if not srcs:
        raise HTTPException(400, "No input")
    src = open_source(srcs if len(srcs) > 1 else srcs[0])
    try:
        w, h, color, n_all = src.width, src.height, src.is_color, src.count
    finally:
        src.close()
    n = int(body.get("frames") or n_all)
    kind = body.get("kind", "lucky")
    from ...config import temp_dir
    free = shutil.disk_usage(temp_dir()).free
    if kind == "lucky":
        up = float((body.get("params") or {}).get("upsample", 1.0))
        secs = n * (w * h / 3e5) * 0.03 * (1 + 0.8 * (up - 1)) + 3
        mem = int(w * h * up * up * (3 if color else 1) * 4 * 3 * 4 + min(n, 400) * w * h * 12)
        return {"frames": n, "seconds": round(secs), "peak_memory_bytes": min(mem, settings().memory_budget_bytes()),
                "temp_disk_bytes": 0, "fits_on_disk": True, "width": w, "height": h}
    est = dso.estimate(n, w, h, color, float((body.get("params") or {}).get("scale", 1.0)), settings().threads(), free)
    est.update({"width": w, "height": h})
    return est


def _save_stack(img: np.ndarray, proj: dict | None, name: str, meta: dict, history: list[str], status: str = "stacked") -> dict:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = Path(proj["folder"]) if proj else settings().folder("projects") / "Stacks"
    base = folder / f"{name}_{stamp}"
    meta = {**meta, "AFSTATUS": status, "SWCREATE": None}
    fits_p = write_fits(base.with_suffix(".fits"), img, meta, history, bits=32)
    tif_p = write_tiff(base.with_suffix(".tif"), np.clip(img / max(float(np.percentile(img, 99.99)), 1e-6), 0, 1)
                       if status == "stacked" else img, 16, {**meta, "history": history})
    library().add_paths([str(fits_p), str(tif_p)], status=status)
    return {"fits": str(fits_p), "tiff": str(tif_p)}


@router.post("/stack/run")
def stack_run(body: dict, request: Request):
    kind = body.get("kind", "lucky")
    pid = body.get("project_id")
    params = body.get("params") or {}
    params.setdefault("memory_budget", settings().memory_budget_bytes())
    params.setdefault("threads", settings().threads())

    if kind == "lucky":
        srcs = _paths(request, body.get("sources", []))
        key = body.get("analysis_key")

        def run(job):
            src = open_source(srcs if len(srcs) > 1 else srcs[0])
            try:
                if body.get("frames"):
                    frames = [int(i) for i in body["frames"]]
                else:
                    rf = _report_file(key) if key else None
                    if rf and rf.exists():
                        rep = json.loads(rf.read_text())
                    else:
                        job.update(0.0, "Scoring frames first")
                        rep = quality.analyze(src, "planetary", lambda f, m=None: job.update(f * 0.3, m))
                    sel = body.get("selection") or {}
                    frames = quality.select_frames(rep, sel.get("mode", "percent"), float(sel.get("value", 25)),
                                                   sel.get("include"), sel.get("exclude"))
                res = lucky.stack(src, frames, params, progress=lambda f, m=None: job.update(f, m))
                meta = dict(src.meta)
            finally:
                src.close()
            meta.update({"NCOMBINE": res.frames_used, "IMAGETYP": "Stack"})
            hist = [f"AstroFlow lucky stack of {res.frames_used} frames from {', '.join(Path(s).name for s in srcs[:3])}",
                    "params: " + json.dumps({k: v for k, v in params.items() if k not in ('memory_budget', 'threads')})]
            proj = projects.load(pid) if pid else None
            name = (meta.get("OBJECT") or Path(srcs[0]).stem).replace(" ", "")
            out = _save_stack(res.image, proj, f"{name}_stack", meta, hist)
            info = {**res.info, **out, "aps": len(res.aps), "ap_size": res.ap_size, "shape": list(res.image.shape)}
            if proj:
                proj["stack"] = {"kind": "lucky", "params": params, "frames": len(frames), "output": out["fits"], "info": info}
                proj["process"]["base"] = out["fits"]
                projects.save(proj)
            return info

        j = jobs.submit("stack", f"Stacking {Path(srcs[0]).name}", run)
        return {"job": j.id}

    if kind == "dso":
        groups = body.get("groups") or [{"lights": body.get("sources", [])}]
        for g in groups:
            g["lights"] = _paths(request, g.get("lights", []))
            for k in ("dark", "flat", "bias", "flatdark"):
                v = g.get(k)
                if isinstance(v, list):
                    g[k] = _paths(request, v)
                elif isinstance(v, str) and v not in ("", "auto", "none"):
                    g[k] = str(safe_path(request, v))

        def run(job):
            res = dso.stack(groups, params, progress=job.update, check_cancel=job.check)
            first = expand_paths(groups[0]["lights"])[0]
            meta = read_header(first)
            meta.update({"NCOMBINE": res["frames_used"], "IMAGETYP": "Stack"})
            hist = [f"AstroFlow deep-sky stack: {res['frames_used']} of {res['frames_total']} frames, {res['rejection']} rejection",
                    "params: " + json.dumps({k: v for k, v in params.items() if k not in ('memory_budget', 'threads')})]
            proj = projects.load(pid) if pid else None
            name = str(meta.get("OBJECT") or first.parent.name).replace(" ", "")
            out = _save_stack(res["image"], proj, f"{name}_stack", {k: v for k, v in meta.items() if not str(k).startswith("_")}, hist)
            info = {k: v for k, v in res.items() if k not in ("image", "coverage")}
            info.update(out)
            if proj:
                proj["stack"] = {"kind": "dso", "params": params, "groups": groups, "output": out["fits"], "info": info}
                proj["process"]["base"] = out["fits"]
                projects.save(proj)
            return info

        j = jobs.submit("stack", "Stacking deep-sky frames", run)
        return {"job": j.id}

    if kind == "mosaic":
        panels = _paths(request, body.get("sources", []))

        def run(job):
            imgs = [images.get(p)[0] for p in panels]
            res = mosaic.stitch(imgs, params.get("model", "similarity"), job.update)
            proj = projects.load(pid) if pid else None
            out = _save_stack(res["image"], proj, "mosaic", {"OBJECT": "Moon mosaic", "IMAGETYP": "Mosaic"},
                              [f"AstroFlow mosaic of {len(res['placed'])} panels"], status="stacked")
            if proj:
                proj["stack"] = {"kind": "mosaic", "params": params, "output": out["fits"], "info": {"placed": res["placed"]}}
                proj["process"]["base"] = out["fits"]
                projects.save(proj)
            return {**out, "placed": res["placed"], "missing": res["missing"], "size": res["size"]}

        j = jobs.submit("stack", f"Stitching {len(panels)} panels", run)
        return {"job": j.id}
    raise HTTPException(400, "Unknown stacking type")


@router.get("/stack/calibration")
def calibration_library():
    from ...imaging.calibrate import CalibrationLibrary
    return CalibrationLibrary().list()


# =================================================================== solve ===
@router.get("/solve/status")
def solve_status():
    from ...solve.solver import status
    return status()


@router.post("/solve")
def solve(body: dict, request: Request):
    from ...solve.solver import solve_file
    path = str(safe_path(request, body["path"]))
    hint = None
    if body.get("hint"):
        from ...astro.catalog import Catalog
        o = Catalog.default().get(body["hint"])
        hint = {"ra": o.ra, "dec": o.dec} if o else None
    elif body.get("ra") is not None:
        hint = {"ra": float(body["ra"]), "dec": float(body["dec"])}
    pid = body.get("project_id")

    def run(job):
        res = solve_file(path, hint, body.get("engine"), progress=job.update, check=job.check)
        if res["solved"]:
            rec = library().get_by_path(path)
            if rec:
                sol = res["solution"]
                upd = {"solved": 1, "ra": sol["ra"], "dec": sol["dec"]}
                if not rec.get("target"):
                    from ...solve import annotate
                    from ...solve.wcs import Solution
                    objs = [o for o in annotate.objects_in_image(Solution.from_dict(sol), include_stars=False)]
                    if objs:
                        upd["target"] = objs[0]["label"].split(" (")[0]
                library().update(rec["id"], **upd)
            if pid:
                p = projects.load(pid)
                projects.update(p, solution=res["solution"])
        return res

    j = jobs.submit("solve", f"Plate solving {Path(path).name}", run)
    return {"job": j.id}


@router.get("/solve/annotations")
def annotations(request: Request, path: str, stars: bool = True):
    from ...solve import annotate
    from ...solve.wcs import Solution
    p = safe_path(request, path)
    sol = Solution.load_sidecar(p)
    if not sol:
        return {"solved": False}
    when = None
    meta = read_header(p)
    if meta.get("DATE-OBS"):
        try:
            when = dt.datetime.fromisoformat(str(meta["DATE-OBS"]).replace("Z", "+00:00"))
            when = when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            when = None
    items = annotate.objects_in_image(sol, include_stars=stars, when=when)
    return {"solved": True, "solution": sol.to_dict(), "objects": items, "compass": annotate.compass(sol),
            "scale_bar": annotate.scale_bar(sol.scale_arcsec, sol.width)}


# ================================================================= process ===
@router.get("/process/ops")
def ops():
    return {"ops": [o.to_dict() for o in REGISTRY.values()], "categories": CATEGORIES,
            "presets": pipeline.list_presets(), "mask_types": __import__("astroflow.process.masks", fromlist=["MASK_TYPES"]).MASK_TYPES}


def _load_other(request: Request):
    def load(path: str):
        try:
            return images.get(str(safe_path(request, path)))[0]
        except Exception:
            return None
    return load


@router.post("/process/render")
def render(body: dict, request: Request):
    path = str(safe_path(request, body["path"]))
    img, meta, key = images.get(path)
    H, W = img.shape[:2]
    max_size = int(body.get("max_size", 1400))
    scale = 1.0 if body.get("full") else min(1.0, max_size / max(H, W))
    t0 = time.time()
    steps, masks = body.get("steps", []), body.get("masks", {})
    mp = body.get("mask_preview")      # {"mask": id, "step": uid or None}: show where a mask applies (red)
    upto = body.get("upto")
    if mp:
        uids = [st.get("uid") for st in steps]
        upto = uids.index(mp["step"]) if mp.get("step") in uids else len(steps)
    try:
        out, info = pipeline.renderer.render(img, key, steps, masks, scale,
                                            {**meta, "solution": body.get("solution")}, _load_other(request),
                                            upto=upto)
    except Exception as e:
        log.exception("render failed")
        raise HTTPException(400, f"Processing failed: {e}")
    if mp and masks.get(mp.get("mask")):
        from ...process.masks import build_mask
        m = build_mask(masks[mp["mask"]], out, scale)[..., None]
        base3 = out if out.ndim == 3 else np.repeat(out[..., None], 3, axis=2)
        out = np.clip(base3 * (1 - 0.55 * m) + np.array([1.0, 0.15, 0.15], np.float32) * 0.55 * m, 0, 1)
    disp = out
    if body.get("crop"):
        x0, y0, x1, y1 = body["crop"]
        h, w = disp.shape[:2]
        disp = disp[int(y0 * h): int(y1 * h), int(x0 * w): int(x1 * w)]
    if max(disp.shape[:2]) > max_size and body.get("full"):
        import cv2
        f = max_size / max(disp.shape[:2])
        disp = cv2.resize(disp, (int(disp.shape[1] * f), int(disp.shape[0] * f)), interpolation=cv2.INTER_AREA)
    data = jpeg(to8(disp), 90)
    return {"image": "data:image/jpeg;base64," + base64.b64encode(data).decode(), "width": int(disp.shape[1]),
            "height": int(disp.shape[0]), "full_width": W, "full_height": H, "scale": scale,
            "histogram": histogram(out), "notes": info["notes"], "timings": info["timings"],
            "ms": round((time.time() - t0) * 1000), "target": pipeline.guess_target(out if out.ndim else img, meta)
            if body.get("guess") else None}


@router.post("/process/auto")
def auto(body: dict, request: Request):
    path = str(safe_path(request, body["path"]))
    img, meta, _ = images.get(path)
    target = body.get("target") or pipeline.guess_target(img, meta)
    return {"target": target, "steps": pipeline.auto_pipeline(target, img)}


@router.get("/process/presets")
def presets():
    return pipeline.list_presets()


@router.get("/process/preset")
def preset(name: str):
    try:
        return pipeline.load_preset(name)
    except KeyError:
        raise HTTPException(404, "Preset not found")


@router.post("/process/presets")
def save_preset(body: dict):
    try:
        return pipeline.save_preset(body["name"], body.get("steps", []), body.get("target", ""), body.get("masks"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/process/presets")
def delete_preset(name: str):
    try:
        pipeline.delete_preset(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/process/save")
def process_save(body: dict, request: Request):
    """Render at full resolution and save a processed master (FITS 32-bit + TIFF 16-bit) into the project."""
    path = str(safe_path(request, body["path"]))
    pid = body.get("project_id")
    steps, masks = body.get("steps", []), body.get("masks", {})

    def run(job):
        img, meta, key = images.get(path)
        out, info = pipeline.renderer.render(img, key, steps, masks, 1.0, {**meta, "solution": body.get("solution")},
                                            _load_other(request), progress=job.update, check=job.check)
        proj = projects.load(pid) if pid else None
        hist = pipeline.describe_steps(steps)
        name = str(meta.get("OBJECT") or Path(path).stem).replace(" ", "")
        res = _save_stack(np.clip(out, 0, 1), proj, f"{name}_processed", {k: v for k, v in meta.items() if not str(k).startswith("_")},
                          hist, status="processed")
        # carry the plate solution over to the processed files when the geometry is unchanged
        from ...solve.wcs import Solution
        sol = Solution.load_sidecar(path)
        if sol and out.shape[:2] == img.shape[:2]:
            sol.save_sidecar(res["fits"])
            sol.save_sidecar(res["tiff"])
        if proj:
            proj["process"]["output"] = res["fits"]
            projects.save(proj)
        return {**res, "notes": info["notes"]}

    j = jobs.submit("process", "Processing at full resolution", run)
    return {"job": j.id}


@router.post("/process/psf")
def psf(body: dict, request: Request):
    from ...process.core import estimate_psf_sigma
    img, _, _ = images.get(str(safe_path(request, body["path"])))
    sigma, how = estimate_psf_sigma(img)
    return {"sigma": sigma, "fwhm": sigma * 2.3548, "how": how}


# ================================================================== export ===
@router.get("/export/options")
def export_options():
    return {"formats": exporter.FORMATS, "sizes": exporter.SIZE_PRESETS, "profiles": exporter.PROFILES,
            "watermarks": exporter.list_watermarks(), "folder": str(settings().folder("exports"))}


@router.get("/export/watermarks")
def watermarks():
    return exporter.list_watermarks()


@router.post("/export/watermarks")
def save_watermark(body: dict):
    return exporter.save_watermark(body["name"], body["watermark"])


@router.delete("/export/watermarks")
def delete_watermark(name: str):
    return exporter.delete_watermark(name)


def _prepare_export_image(request: Request, item: dict, job=None) -> tuple[np.ndarray, dict, dict | None]:
    path = str(safe_path(request, item["path"]))
    img, meta, key = images.get(path)
    steps = item.get("steps")
    if steps is None and item.get("project_id"):
        pr = projects.load(item["project_id"])["process"]
        steps, masks = pr.get("steps", []), pr.get("masks", {})
    else:
        masks = item.get("masks", {})
    if steps:
        img, _ = pipeline.renderer.render(img, key, steps, masks, 1.0, meta, _load_other(request),
                                          progress=job.update if job else None)
    elif float(np.percentile(img, 50)) < 0.05 and str(meta.get("AFSTATUS", "")) == "stacked":
        img = img / max(float(np.percentile(img, 99.99)), 1e-6)
    from ...solve.wcs import Solution
    sol = Solution.load_sidecar(path)
    return np.clip(img, 0, 1), {k: v for k, v in meta.items() if not str(k).startswith("_")}, sol.to_dict() if sol else None


@router.post("/export")
def export(body: dict, request: Request):
    items = body.get("items", [])
    opts = body.get("options", {})
    folder = Path(body.get("folder") or settings().folder("exports"))
    if not is_local(request):
        folder = settings().folder("exports")
    if not items:
        raise HTTPException(400, "Nothing to export")

    def run(job):
        results = []
        for i, it in enumerate(items):
            job.update(i / len(items), f"Exporting {i + 1} of {len(items)}")
            img, meta, sol = _prepare_export_image(request, it, None)
            name = it.get("filename") or exporter.default_filename(meta, opts.get("format", "jpg"), it.get("suffix", ""))
            hist = pipeline.describe_steps(it.get("steps") or [])
            r = exporter.export_image(img, folder / name, opts, meta, sol, hist)
            library().add_paths([r["path"]], status="exported")
            if it.get("project_id"):
                p = projects.load(it["project_id"])
                p["exports"].append({"path": r["path"], "options": opts, "time": dt.datetime.now().isoformat(timespec="seconds")})
                projects.save(p)
            results.append(r)
        return {"files": results, "folder": str(folder)}

    j = jobs.submit("export", f"Exporting {len(items)} image(s)", run)
    return {"job": j.id}


@router.post("/export/preview")
def export_preview(body: dict, request: Request):
    import tempfile
    img, meta, sol = _prepare_export_image(request, body["item"])
    opts = dict(body.get("options", {}))
    w = opts.get("preview_width", 1000)
    tw, th, fit, _ = exporter.target_size(img.shape[1], img.shape[0], opts)
    f = min(1.0, w / tw)
    opts.update({"format": "jpg", "quality": 85, "size": "custom", "width": max(1, int(tw * f)), "height": max(1, int(th * f)),
                 "fit": fit})
    with tempfile.TemporaryDirectory() as td:
        r = exporter.export_image(img, Path(td) / "p.jpg", opts, meta, sol)
        data = Path(r["path"]).read_bytes()
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ============================================================ quick workflow ===
@router.post("/workflow/quick")
def quick(body: dict, request: Request):
    """One click (Beginner mode): analyse -> keep the best frames -> stack -> add Auto steps -> open in Post-process."""
    srcs = _paths(request, body.get("sources", []))
    if not srcs:
        raise HTTPException(400, "Choose a capture first")

    def run(job):
        src = open_source(srcs if len(srcs) > 1 else srcs[0])
        try:
            job.update(0.01, "Step 1 of 4: scoring every frame")
            kind = quality.guess_kind(src)
            rep = quality.analyze(src, kind, lambda f, m=None: job.update(0.02 + f * 0.3, f"Step 1 of 4: {m}"), threads=settings().threads())
            key = _report_key(srcs, kind)
            rep["sources"], rep["key"] = srcs, key
            if len(getattr(src, "paths", [])) > 1:
                rep["files"] = [str(x) for x in src.paths]
            atomic_write_text(_report_file(key), json.dumps(rep))
            rec = rep["recommendation"]
            frames = quality.select_frames(rep, "percent", rec["keep_percent"])
            meta = dict(src.meta)
            name = body.get("name") or (meta.get("OBJECT") or Path(srcs[0]).stem)
            proj = projects.new_project(f"{name} {dt.date.today()}", srcs, {"name": meta.get("OBJECT") or name})
            projects.update(proj, analysis={"key": key, "kind": kind, "summary": rep["summary"]},
                            selection={"mode": "percent", "value": rec["keep_percent"]})
            job.update(0.33, f"Step 2 of 4: stacking the best {len(frames):,} frames")
            prog = lambda f, m=None: job.update(0.33 + f * 0.55, f"Step 2 of 4: {m}")  # noqa: E731
            if kind == "planetary":
                res = lucky.stack(src, frames, {"memory_budget": settings().memory_budget_bytes(), "threads": settings().threads()}, prog)
                stacked, info = res.image, res.info
            else:
                lights = [str(p) for i, p in enumerate(src.paths) if i in set(frames)]
                r = dso.stack([{"lights": lights, "dark": "auto", "flat": "auto", "bias": "auto"}], {}, prog, check_cancel=job.check)
                stacked, info = r["image"], {k: v for k, v in r.items() if k not in ("image", "coverage", "frames")}
        finally:
            src.close()
        meta.update({"NCOMBINE": len(frames), "IMAGETYP": "Stack"})
        out = _save_stack(stacked, proj, f"{str(name).replace(' ', '')}_stack", {k: v for k, v in meta.items() if not str(k).startswith("_")},
                          [f"AstroFlow quick workflow: {len(frames)} frames"])
        job.update(0.9, "Step 3 of 4: preparing an Auto processing starting point")
        img = np.clip(stacked / max(float(np.percentile(stacked, 99.99)), 1e-6), 0, None) if kind == "planetary" else stacked
        ttype = pipeline.guess_target(img, meta)
        steps = pipeline.auto_pipeline(ttype, img)
        proj["stack"] = {"kind": "lucky" if kind == "planetary" else "dso", "output": out["fits"], "info": info, "frames": len(frames)}
        proj["process"]["base"] = out["fits"]
        projects.save(proj)
        projects.commit_process(proj, steps, {}, f"Auto ({ttype})")
        job.update(1.0, "Step 4 of 4: ready - review the result in Post-process")
        return {"project_id": proj["id"], "stack": out, "target_type": ttype, "frames": len(frames),
                "recommendation": rec, "analysis_key": key}

    j = jobs.submit("workflow", f"Quick process: {Path(srcs[0]).name}", run)
    return {"job": j.id}
