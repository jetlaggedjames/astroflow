"""Safe clean-up: find duplicates and failed captures, free space, offload to another drive.

Safety rules (enforced here, not just in the user interface):
  * Nothing is ever removed automatically.
  * Every removal or offload is a two-step "plan, then confirm": the plan lists
    every file and the total size and returns a one-time code; only that code
    can carry it out, within 10 minutes, and only if the files haven't changed.
  * Raw captures are protected: a plan touching them needs an explicit extra
    "yes, include raw data" choice and says so in red.
  * "Remove" means move to the Recycle Bin / Trash (recoverable), never a
    permanent delete.
  * Offload = copy, verify the copy with a SHA-256 checksum, record where it
    went - and only then *offer* to remove the original (another plan).
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from .index import library

log = logging.getLogger("astroflow.cleanup")

PLAN_TTL = 600
_plans: dict[str, dict] = {}
_lock = threading.Lock()
RAW_TYPES = {"light", "dark", "flat", "bias", "flatdark"}
RAW_KINDS = {"ser", "video", "fits", "raw", "xisf", "tiff"}   # camera data; PNG/JPG are rendered images


def is_raw(rec: dict) -> bool:
    return (rec.get("imagetype") or "light") in RAW_TYPES and rec.get("status") == "raw" and rec.get("kind") in RAW_KINDS


class SafetyError(RuntimeError):
    pass


def _quickhash(path: str, size: int) -> str:
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()


def sha256_file(path: str, progress: Callable[[int], None] | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(8 << 20)
            if not b:
                break
            h.update(b)
            if progress:
                progress(len(b))
    return h.hexdigest()


# ------------------------------------------------------------- findings ---

def find_duplicates(check: Callable[[], None] | None = None) -> list[dict]:
    """Groups of files with identical content (size, then quick hash, then full hash)."""
    lib = library()
    rows = lib._conn().execute("SELECT id, path, size, quickhash, mtime FROM files WHERE missing=0 AND size>0").fetchall()
    by_size: dict[int, list] = {}
    for r in rows:
        by_size.setdefault(r["size"], []).append(dict(r))
    groups = []
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        if check:
            check()
        by_q: dict[str, list] = {}
        for it in items:
            try:
                q = it["quickhash"] or _quickhash(it["path"], size)
            except OSError:
                continue
            if not it["quickhash"]:
                with lib._write_lock:
                    lib._conn().execute("UPDATE files SET quickhash=? WHERE id=?", (q, it["id"]))
            by_q.setdefault(q, []).append(it)
        for q, same in by_q.items():
            if len(same) < 2:
                continue
            full: dict[str, list] = {}
            for it in same:
                try:
                    full.setdefault(sha256_file(it["path"]) if size < 4 << 30 else q, []).append(it)
                except OSError:
                    continue
            for h, dup in full.items():
                if len(dup) > 1:
                    dup.sort(key=lambda d: d["mtime"])
                    groups.append({"size": size, "hash": h, "files": [{"id": d["id"], "path": d["path"]} for d in dup],
                                   "keep": dup[0]["id"], "reclaimable": size * (len(dup) - 1)})
    groups.sort(key=lambda g: -g["reclaimable"])
    return groups


def find_problems() -> list[dict]:
    """Captures that look failed or useless: interrupted, empty, unreadable, tiny, or rated very low."""
    lib = library()
    out = []
    for r in lib._conn().execute("SELECT * FROM files WHERE missing=0"):
        reasons = []
        if r["error"]:
            reasons.append(r["error"])
        if r["kind"] == "ser" and (r["frames"] or 0) == 0:
            reasons.append("video contains no frames")
        if r["size"] is not None and r["size"] < 2048:
            reasons.append("file is almost empty")
        if r["quality"] is not None and r["quality"] < 5:
            reasons.append(f"very low quality score ({r['quality']:.0f})")
        if r["rating"] == 1:
            reasons.append("you rated it 1 star")
        if reasons:
            out.append({"id": r["id"], "path": r["path"], "size": r["size"], "reasons": reasons,
                        "raw": is_raw(dict(r))})
    return out


def space_report() -> dict:
    lib = library()
    c = lib._conn()
    by_kind = [dict(r) for r in c.execute("SELECT kind, COUNT(*) AS files, SUM(size) AS size FROM files WHERE missing=0 GROUP BY kind ORDER BY size DESC")]
    by_status = [dict(r) for r in c.execute("SELECT status, COUNT(*) AS files, SUM(size) AS size FROM files WHERE missing=0 GROUP BY status")]
    drives = {}
    for root in lib.roots():
        try:
            u = shutil.disk_usage(root)
            drives[root] = {"total": u.total, "free": u.free}
        except OSError:
            pass
    return {"by_kind": by_kind, "by_status": by_status, "drives": drives}


# --------------------------------------------------------------- planning ---

def _files_for(ids: list[int]) -> list[dict]:
    lib = library()
    out = []
    for i in ids:
        r = lib.get(int(i))
        if r is None:
            raise SafetyError(f"Unknown file id {i}")
        if r["missing"] or not os.path.exists(r["path"]):
            raise SafetyError(f"{r['name']} is not available (drive unplugged or file moved)")
        st = os.stat(r["path"])
        out.append({"id": r["id"], "path": r["path"], "name": r["name"], "size": st.st_size, "mtime": st.st_mtime,
                    "raw": is_raw(r),
                    "offloaded_to": r["offloaded_to"], "root": r["root"]})
    return out


def _store(plan: dict) -> dict:
    token = secrets.token_urlsafe(9)
    plan.update({"token": token, "expires": time.time() + PLAN_TTL})
    with _lock:
        for k in [k for k, v in _plans.items() if v["expires"] < time.time()]:
            _plans.pop(k, None)
        _plans[token] = plan
    return {k: v for k, v in plan.items() if k != "files_internal"}


def plan_trash(ids: list[int], include_raw: bool = False) -> dict:
    files = _files_for(ids)
    if not files:
        raise SafetyError("Nothing selected")
    raw = [f for f in files if f["raw"]]
    if raw and not include_raw:
        raise SafetyError(f"{len(raw)} of these are RAW captures (original data). Tick 'include raw captures' if you "
                          "really want to remove them - consider offloading them to another drive instead.")
    total = sum(f["size"] for f in files)
    warn = []
    if raw:
        warn.append(f"WARNING: {len(raw)} raw capture(s) - original data that cannot be re-shot.")
    return _store({"action": "trash", "files": files, "count": len(files), "total_size": total, "warnings": warn,
                   "summary": f"Move {len(files)} file(s), {total / 1e9:.2f} GB, to the Recycle Bin. You can restore them from there."})


def plan_offload(ids: list[int], dest_root: str) -> dict:
    files = _files_for(ids)
    dest = Path(dest_root)
    if not dest.exists():
        raise SafetyError(f"Destination {dest} does not exist (is the drive connected?)")
    total = sum(f["size"] for f in files)
    free = shutil.disk_usage(dest).free
    if free < total * 1.02 + 200e6:
        raise SafetyError(f"Not enough space on {dest}: need {total / 1e9:.2f} GB, {free / 1e9:.2f} GB free")
    for f in files:
        try:
            rel = Path(f["path"]).relative_to(f["root"])
        except ValueError:
            rel = Path(Path(f["path"]).name)
        f["dest"] = str(dest / Path(f["root"]).name / rel)
        if os.path.exists(f["dest"]):
            raise SafetyError(f"{f['dest']} already exists on the destination - choose another folder")
    return _store({"action": "offload", "files": files, "count": len(files), "total_size": total, "dest_root": str(dest),
                   "warnings": [], "summary": f"Copy {len(files)} file(s), {total / 1e9:.2f} GB, to {dest}, verify every copy "
                   "with a SHA-256 checksum, and record where they went. The originals are NOT removed."})


def plan_remove_offloaded(ids: list[int]) -> dict:
    files = _files_for(ids)
    not_verified = [f["name"] for f in files if not f["offloaded_to"]]
    if not_verified:
        raise SafetyError("These files have not been offloaded and verified yet: " + ", ".join(not_verified[:5]))
    for f in files:
        if not os.path.exists(f["offloaded_to"]):
            raise SafetyError(f"The offloaded copy of {f['name']} is not reachable ({f['offloaded_to']}). Connect that drive first.")
        if os.path.getsize(f["offloaded_to"]) != f["size"]:
            raise SafetyError(f"The offloaded copy of {f['name']} has a different size - not removing the original.")
    total = sum(f["size"] for f in files)
    return _store({"action": "remove_offloaded", "files": files, "count": len(files), "total_size": total,
                   "warnings": [], "summary": f"Move {len(files)} original(s), {total / 1e9:.2f} GB, to the Recycle Bin. "
                   "Verified copies stay on the offload drive and the library remembers where they are."})


def get_plan(token: str) -> dict:
    with _lock:
        p = _plans.get(token)
    if not p or p["expires"] < time.time():
        raise SafetyError("This confirmation has expired - please review the list again.")
    return p


def cancel(token: str) -> None:
    with _lock:
        _plans.pop(token, None)


# -------------------------------------------------------------- execution ---

def _recheck(files: list[dict]) -> None:
    for f in files:
        if not os.path.exists(f["path"]):
            raise SafetyError(f"{f['name']} no longer exists")
        st = os.stat(f["path"])
        if st.st_size != f["size"] or abs(st.st_mtime - f["mtime"]) > 1e-3:
            raise SafetyError(f"{f['name']} changed since you reviewed the list - please review again")


def _to_trash(path: str) -> None:
    from send2trash import send2trash
    send2trash(path)
    for side in (".json", ".ts", ".wcs.json"):
        if os.path.exists(path + side):
            try:
                send2trash(path + side)
            except Exception:
                pass


def execute(token: str, job=None) -> dict:
    """Carry out a confirmed plan. Returns a per-file report."""
    plan = get_plan(token)
    with _lock:
        _plans.pop(token, None)   # one-time use
    files = plan["files"]
    _recheck(files)
    lib = library()
    done, failed = [], []
    if plan["action"] in ("trash", "remove_offloaded"):
        for i, f in enumerate(files):
            if job:
                job.update(i / len(files), f"Moving {f['name']} to the Recycle Bin")
            try:
                _to_trash(f["path"])
                done.append(f["path"])
                if plan["action"] == "trash":
                    lib.forget([f["id"]])
                else:
                    with lib._write_lock:
                        lib._conn().execute("UPDATE files SET missing=1, status='offloaded' WHERE id=?", (f["id"],))
            except Exception as e:
                failed.append({"path": f["path"], "error": str(e)})
        lib.log_event(plan["action"], {"files": done, "failed": failed, "bytes": plan["total_size"]})
        return {"action": plan["action"], "done": len(done), "failed": failed, "bytes": plan["total_size"]}
    if plan["action"] == "offload":
        total = plan["total_size"]
        copied = [0]
        for i, f in enumerate(files):
            if job:
                job.check()
            dest = Path(f["dest"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            src_hash = hashlib.sha256()
            try:
                with open(f["path"], "rb") as s, open(tmp, "wb") as d:
                    while True:
                        b = s.read(8 << 20)
                        if not b:
                            break
                        src_hash.update(b)
                        d.write(b)
                        copied[0] += len(b)
                        if job:
                            job.update(copied[0] / max(total, 1) * 0.9, f"Copying {f['name']} ({copied[0] / 1e9:.2f} / {total / 1e9:.2f} GB)")
                    d.flush()
                    os.fsync(d.fileno())
                # Verify by reading the copy back from the destination drive.
                if sha256_file(str(tmp)) != src_hash.hexdigest():
                    tmp.unlink(missing_ok=True)
                    raise SafetyError("checksum mismatch after copying - the copy was discarded")
                os.replace(tmp, dest)
                for side in (".json", ".wcs.json"):
                    if os.path.exists(f["path"] + side):
                        shutil.copy2(f["path"] + side, str(dest) + side)
                shutil.copystat(f["path"], dest)
                with lib._write_lock:
                    lib._conn().execute("UPDATE files SET offloaded_to=? WHERE id=?", (str(dest), f["id"]))
                done.append({"path": f["path"], "dest": str(dest), "sha256": src_hash.hexdigest()})
            except Exception as e:
                tmp.unlink(missing_ok=True) if tmp.exists() else None
                failed.append({"path": f["path"], "error": str(e)})
        lib.log_event("offload", {"done": done, "failed": failed})
        return {"action": "offload", "done": len(done), "verified": len(done), "failed": failed, "files": done,
                "next": "Originals were kept. You can now remove them safely (they will go to the Recycle Bin)."}
    raise SafetyError("Unknown plan")
