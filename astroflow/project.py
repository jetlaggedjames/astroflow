"""Projects: one small file that remembers everything done to a target.

A project (``project.afproj``, plain JSON) records the source captures (never
modified), the analysis and frame selection, the stacking settings and result,
the plate solution, every post-processing step with its settings (plus the
undo/redo history) and the exports. Reopen it any time, change one setting and
re-run. Saving is atomic, with the previous version kept as ``.bak``.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

from .config import atomic_write_text, settings

VERSION = 1
MAX_HISTORY = 200
_lock = threading.RLock()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _\-+.]", "_", s).strip().replace(" ", "_")[:50] or "Project"


def projects_root() -> Path:
    return settings().folder("projects")


def new_project(name: str, sources: list[str] | None = None, target: dict | None = None) -> dict:
    pid = uuid.uuid4().hex[:10]
    folder = projects_root() / f"{_safe(name)}_{pid[:6]}"
    folder.mkdir(parents=True, exist_ok=True)
    proj = {
        "version": VERSION, "id": pid, "name": name, "created": _now(), "modified": _now(),
        "folder": str(folder), "target": target or {}, "sources": list(sources or []),
        "analysis": {}, "selection": {"mode": "percent", "value": 25, "include": [], "exclude": []},
        "stack": {"kind": None, "params": {}, "groups": [], "output": None, "info": None},
        "process": {"base": None, "steps": [], "masks": {}, "history": [], "index": -1},
        "solution": None, "exports": [], "notes": "",
    }
    save(proj)
    return proj


def path_of(proj: dict) -> Path:
    return Path(proj["folder"]) / "project.afproj"


def save(proj: dict) -> dict:
    with _lock:
        proj["modified"] = _now()
        p = path_of(proj)
        if p.exists():
            try:
                shutil.copy2(p, p.with_suffix(".afproj.bak"))
            except OSError:
                pass
        atomic_write_text(p, json.dumps(proj, indent=1, default=str))
        settings().set("last_project", proj["id"])
        return proj


def load(path_or_id: str) -> dict:
    p = Path(path_or_id)
    if not p.exists():
        found = find(path_or_id)
        if not found:
            raise FileNotFoundError(f"Project {path_or_id} not found")
        p = Path(found)
    if p.is_dir():
        p = p / "project.afproj"
    try:
        proj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        bak = p.with_suffix(".afproj.bak")
        proj = json.loads(bak.read_text(encoding="utf-8"))  # recover from the last good save
    proj["folder"] = str(p.parent)
    return proj


def find(pid: str) -> str | None:
    for f in projects_root().glob("*/project.afproj"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("id") == pid:
                return str(f)
        except Exception:
            continue
    return None


def list_projects() -> list[dict]:
    out = []
    for f in sorted(projects_root().glob("*/project.afproj"), key=lambda q: q.stat().st_mtime, reverse=True):
        try:
            d = load(str(f))
            out.append({"id": d["id"], "name": d["name"], "modified": d.get("modified"), "path": str(f),
                        "target": d.get("target", {}).get("name"), "stacked": bool(d.get("stack", {}).get("output")),
                        "steps": len(d.get("process", {}).get("steps", [])), "sources": len(d.get("sources", []))})
        except Exception:
            continue
    return out


def update(proj: dict, **sections) -> dict:
    for k, v in sections.items():
        if isinstance(v, dict) and isinstance(proj.get(k), dict):
            proj[k].update(v)
        else:
            proj[k] = v
    return save(proj)


# ------------------------------------------------------- processing history

def commit_process(proj: dict, steps: list[dict], masks: dict, label: str) -> dict:
    """Record a new processing state (after any edit). Redo history after the current point is dropped."""
    pr = proj["process"]
    hist = pr.setdefault("history", [])
    idx = pr.get("index", len(hist) - 1)
    hist = hist[: idx + 1]
    if not hist:
        # The unedited image is the first history entry, so the very first edit can be undone too.
        hist.append({"label": "Original (no edits)", "time": _now(), "steps": [], "masks": {}})
    if hist and hist[-1]["steps"] == steps and hist[-1].get("masks") == masks:
        return proj
    hist.append({"label": label, "time": _now(), "steps": copy.deepcopy(steps), "masks": copy.deepcopy(masks)})
    if len(hist) > MAX_HISTORY:
        hist = hist[-MAX_HISTORY:]
    pr.update({"history": hist, "index": len(hist) - 1, "steps": copy.deepcopy(steps), "masks": copy.deepcopy(masks)})
    return save(proj)


def undo(proj: dict) -> dict:
    pr = proj["process"]
    if pr.get("index", -1) > 0:
        pr["index"] -= 1
        h = pr["history"][pr["index"]]
        pr["steps"], pr["masks"] = copy.deepcopy(h["steps"]), copy.deepcopy(h["masks"])
        save(proj)
    return proj


def redo(proj: dict) -> dict:
    pr = proj["process"]
    if pr.get("index", -1) < len(pr.get("history", [])) - 1:
        pr["index"] += 1
        h = pr["history"][pr["index"]]
        pr["steps"], pr["masks"] = copy.deepcopy(h["steps"]), copy.deepcopy(h["masks"])
        save(proj)
    return proj


def jump(proj: dict, index: int) -> dict:
    pr = proj["process"]
    if 0 <= index < len(pr.get("history", [])):
        pr["index"] = index
        h = pr["history"][index]
        pr["steps"], pr["masks"] = copy.deepcopy(h["steps"]), copy.deepcopy(h["masks"])
        save(proj)
    return proj


def output_path(proj: dict, name: str) -> Path:
    p = Path(proj["folder"]) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
