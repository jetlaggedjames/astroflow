"""The Library: a fast, searchable index of every capture, stack and export.

* Folders (including external drives) are scanned into a local SQLite database.
* Re-scans are quick: a file is only re-read when its size or modification
  time changed, and then only its header (never the whole file).
* Targets come from file headers, then plate-solve results, then the file or
  folder name ("Jupiter_2215.ser", ".../M31/lights/...").
* Files shot on the same night are grouped into sessions automatically.
* Records are never deleted just because a drive is unplugged: the files are
  marked "missing" and come back when the drive does.
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

from ..config import app_home, cache_dir, settings
from ..io.images import ALL_EXT, kind_of, read_header

log = logging.getLogger("astroflow.library")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  root TEXT, name TEXT, ext TEXT, kind TEXT,
  size INTEGER, mtime REAL,
  width INTEGER, height INTEGER, channels INTEGER, frames INTEGER, bitdepth INTEGER, bayer TEXT,
  target TEXT, target_source TEXT,
  date_obs TEXT, night TEXT, session TEXT,
  camera TEXT, telescope TEXT, exposure REAL, gain REAL, temperature REAL,
  imagetype TEXT, status TEXT DEFAULT 'raw',
  quality REAL, solved INTEGER DEFAULT 0, ra REAL, dec REAL,
  rating INTEGER DEFAULT 0, tags TEXT DEFAULT '', notes TEXT DEFAULT '',
  quickhash TEXT, offloaded_to TEXT, missing INTEGER DEFAULT 0, error TEXT,
  indexed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_target ON files(target);
CREATE INDEX IF NOT EXISTS ix_night ON files(night);
CREATE INDEX IF NOT EXISTS ix_kind ON files(kind);
CREATE INDEX IF NOT EXISTS ix_size ON files(size);
CREATE TABLE IF NOT EXISTS roots (path TEXT PRIMARY KEY, last_scan REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, action TEXT, detail TEXT);
"""

SKIP_SUFFIXES = (".json", ".ts", ".part", ".tmp", ".bak", ".header-backup")
SORTABLE = {"name", "date_obs", "size", "target", "kind", "exposure", "gain", "quality", "rating", "mtime", "camera", "night"}


def _now() -> float:
    return time.time()


class Library:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else app_home() / "library.db"
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def log_event(self, action: str, detail: dict | str) -> None:
        with self._write_lock:
            self._conn().execute("INSERT INTO events(time, action, detail) VALUES (?,?,?)",
                                 (_now(), action, json.dumps(detail, default=str) if not isinstance(detail, str) else detail))

    def events(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._conn().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------- scanning
    def roots(self) -> list[str]:
        roots = list(settings().get("folders.library_roots") or [])
        for k in ("captures", "projects", "exports"):
            p = settings().get(f"folders.{k}")
            if p and not any(str(Path(p)).startswith(str(Path(r))) for r in roots):
                roots.append(p)
        return roots

    def scan(self, roots: Iterable[str] | None = None, progress: Callable[[float, str], None] | None = None,
             check: Callable[[], None] | None = None, threads: int = 6) -> dict:
        roots = [str(Path(r)) for r in (roots or self.roots())]
        prog = progress or (lambda f, m=None: None)
        t0 = time.time()
        found: list[tuple[str, str, os.stat_result]] = []
        for root in roots:
            if not Path(root).exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("__pycache__", "$RECYCLE.BIN", "System Volume Information")]
                for fn in filenames:
                    if fn.endswith(SKIP_SUFFIXES):
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in ALL_EXT:
                        continue
                    p = os.path.join(dirpath, fn)
                    try:
                        found.append((p, root, os.stat(p)))
                    except OSError:
                        continue
                if check:
                    check()
            prog(0.1, f"Found {len(found):,} image and video files")
        known = {r["path"]: (r["size"], r["mtime"]) for r in self._conn().execute("SELECT path, size, mtime FROM files")}
        todo = [(p, root, st) for p, root, st in found if known.get(p) != (st.st_size, st.st_mtime)]
        prog(0.15, f"{len(todo):,} new or changed files to read ({len(found) - len(todo):,} unchanged)")
        done = 0
        rows = []

        def work(item):
            p, root, st = item
            return self._record(p, root, st)

        with ThreadPoolExecutor(max_workers=threads) as ex:
            for rec in ex.map(work, todo):
                rows.append(rec)
                done += 1
                if done % 50 == 0 or done == len(todo):
                    if check:
                        check()
                    prog(0.15 + 0.8 * done / max(1, len(todo)), f"Indexed {done:,}/{len(todo):,} files")
                if len(rows) >= 500:
                    self._upsert(rows)
                    rows = []
        self._upsert(rows)
        # Mark files that disappeared under scanned roots (never delete records).
        present = {p for p, _, _ in found}
        missing = 0
        with self._write_lock:
            c = self._conn()
            for r in c.execute("SELECT id, path, root, missing FROM files").fetchall():
                if any(r["path"].startswith(root) for root in roots):
                    is_missing = 0 if r["path"] in present else 1
                    if is_missing != r["missing"]:
                        c.execute("UPDATE files SET missing=? WHERE id=?", (is_missing, r["id"]))
                    missing += is_missing
            for root in roots:
                c.execute("INSERT OR REPLACE INTO roots(path, last_scan) VALUES (?,?)", (root, _now()))
        out = {"files": len(found), "updated": len(todo), "missing": missing, "seconds": round(time.time() - t0, 2)}
        prog(1.0, f"Library up to date: {len(found):,} files ({len(todo):,} read) in {out['seconds']}s")
        return out

    def add_paths(self, paths: Iterable[str], status: str | None = None, extra: dict | None = None) -> list[int]:
        """Index specific files immediately (new recordings, stacks, exports, uploads)."""
        rows = []
        for p in paths:
            pp = Path(p)
            if pp.is_dir():
                for q in sorted(pp.rglob("*")):
                    if q.is_file() and q.suffix.lower() in ALL_EXT and not q.name.endswith(SKIP_SUFFIXES):
                        rows.append(self._record(str(q), str(pp), q.stat(), status, extra))
            elif pp.exists():
                rows.append(self._record(str(pp), str(pp.parent), pp.stat(), status, extra))
        self._upsert(rows)
        ids = []
        for r in rows:
            got = self._conn().execute("SELECT id FROM files WHERE path=?", (r["path"],)).fetchone()
            if got:
                ids.append(got["id"])
        return ids

    def _record(self, path: str, root: str, st: os.stat_result, status: str | None = None, extra: dict | None = None) -> dict:
        meta = read_header(path)
        p = Path(path)
        k = kind_of(p)
        date_obs = _parse_date(meta.get("DATE-OBS") or meta.get("DATE_OBS") or meta.get("DATE"))
        if not date_obs:
            date_obs = dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc)
        target, source = _target(meta, p)
        imagetype = _imagetype(meta, p)
        night = _night(date_obs)
        rec = {
            "path": path, "root": root, "name": p.name, "ext": p.suffix.lower(), "kind": k, "size": st.st_size,
            "mtime": st.st_mtime, "width": _int(meta.get("_width")), "height": _int(meta.get("_height")),
            "channels": _int(meta.get("_channels")), "frames": _int(meta.get("_frames")) or 1,
            "bitdepth": _int(meta.get("BITPIX")), "bayer": meta.get("BAYERPAT") or None,
            "target": target, "target_source": source, "date_obs": date_obs.isoformat(), "night": night,
            "session": f"{night} {target or 'Unknown'}", "camera": str(meta.get("INSTRUME") or "")[:80] or None,
            "telescope": str(meta.get("TELESCOP") or "")[:80] or None, "exposure": _float(meta.get("EXPTIME") or meta.get("EXPOSURE")),
            "gain": _float(meta.get("GAIN")), "temperature": _float(meta.get("CCD-TEMP")),
            "imagetype": imagetype, "status": status or _status(meta, p, imagetype),
            "error": meta.get("_error"), "indexed_at": _now(), "missing": 0,
        }
        sol = Path(path + ".wcs.json")
        if sol.exists():
            try:
                s = json.loads(sol.read_text(encoding="utf-8"))
                rec.update({"solved": 1, "ra": s.get("ra"), "dec": s.get("dec")})
            except Exception:
                pass
        if meta.get("_truncated"):
            rec["error"] = "Recording was interrupted (repairable)"
        if extra:
            rec.update(extra)
        return rec

    def _upsert(self, rows: list[dict]) -> None:
        if not rows:
            return
        cols = list(rows[0].keys())
        with self._write_lock:
            c = self._conn()
            c.execute("BEGIN")
            for r in rows:
                keys = list(r.keys())
                ph = ",".join("?" for _ in keys)
                upd = ",".join(f"{k}=excluded.{k}" for k in keys if k not in ("path",))
                c.execute(f"INSERT INTO files({','.join(keys)}) VALUES ({ph}) ON CONFLICT(path) DO UPDATE SET {upd}",
                          [r[k] for k in keys])
            c.execute("COMMIT")

    # ------------------------------------------------------------- queries
    def query(self, text: str = "", kind: str = "", target: str = "", night: str = "", session: str = "",
              status: str = "", imagetype: str = "", camera: str = "", min_rating: int = 0, tag: str = "",
              date_from: str = "", date_to: str = "", min_exposure: float | None = None, max_exposure: float | None = None,
              gain: float | None = None, include_missing: bool = True, sort: str = "date_obs", desc: bool = True,
              limit: int = 500, offset: int = 0) -> dict:
        where, args = [], []
        if text:
            for word in text.split():
                where.append("(name LIKE ? OR target LIKE ? OR tags LIKE ? OR notes LIKE ? OR camera LIKE ? OR path LIKE ?)")
                args += [f"%{word}%"] * 6
        for col, val in (("kind", kind), ("target", target), ("night", night), ("session", session), ("status", status),
                         ("imagetype", imagetype), ("camera", camera)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if min_rating:
            where.append("rating >= ?")
            args.append(int(min_rating))
        if tag:
            where.append("(',' || tags || ',') LIKE ?")
            args.append(f"%,{tag},%")
        if date_from:
            where.append("night >= ?")
            args.append(date_from)
        if date_to:
            where.append("night <= ?")
            args.append(date_to)
        if min_exposure is not None:
            where.append("exposure >= ?")
            args.append(min_exposure)
        if max_exposure is not None:
            where.append("exposure <= ?")
            args.append(max_exposure)
        if gain is not None:
            where.append("gain = ?")
            args.append(gain)
        if not include_missing:
            where.append("missing = 0")
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        sort = sort if sort in SORTABLE else "date_obs"
        c = self._conn()
        total = c.execute(f"SELECT COUNT(*), COALESCE(SUM(size),0) FROM files {sql_where}", args).fetchone()
        rows = c.execute(f"SELECT * FROM files {sql_where} ORDER BY {sort} {'DESC' if desc else 'ASC'}, id DESC LIMIT ? OFFSET ?",
                         args + [int(limit), int(offset)]).fetchall()
        return {"total": total[0], "total_size": total[1], "items": [dict(r) for r in rows]}

    def facets(self) -> dict:
        c = self._conn()
        def col(name):
            return [{"value": r[0], "count": r[1]} for r in c.execute(
                f"SELECT {name}, COUNT(*) FROM files WHERE {name} IS NOT NULL AND {name} != '' GROUP BY {name} ORDER BY COUNT(*) DESC LIMIT 200")]
        sessions = [dict(r) for r in c.execute(
            "SELECT session, night, target, COUNT(*) AS files, SUM(size) AS size, MIN(date_obs) AS start, MAX(date_obs) AS end "
            "FROM files GROUP BY session ORDER BY night DESC, target LIMIT 300")]
        tags = {}
        for r in c.execute("SELECT tags FROM files WHERE tags != ''"):
            for t in r[0].split(","):
                if t:
                    tags[t] = tags.get(t, 0) + 1
        totals = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0), SUM(missing) FROM files").fetchone()
        return {"kind": col("kind"), "target": col("target"), "night": col("night"), "status": col("status"),
                "imagetype": col("imagetype"), "camera": col("camera"), "sessions": sessions,
                "tags": [{"value": k, "count": v} for k, v in sorted(tags.items(), key=lambda kv: -kv[1])],
                "totals": {"files": totals[0], "size": totals[1], "missing": totals[2] or 0}}

    def get(self, file_id: int) -> dict | None:
        r = self._conn().execute("SELECT * FROM files WHERE id=?", (int(file_id),)).fetchone()
        return dict(r) if r else None

    def get_by_path(self, path: str) -> dict | None:
        r = self._conn().execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
        return dict(r) if r else None

    def update(self, file_id: int, **fields) -> dict:
        allowed = {"rating", "tags", "notes", "target", "status", "quality", "imagetype", "solved", "ra", "dec", "offloaded_to"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if "tags" in fields and isinstance(fields["tags"], (list, tuple)):
            fields["tags"] = ",".join(sorted({t.strip() for t in fields["tags"] if t.strip()}))
        if "target" in fields:
            fields["target_source"] = "user"
        if fields:
            sets = ",".join(f"{k}=?" for k in fields)
            with self._write_lock:
                self._conn().execute(f"UPDATE files SET {sets} WHERE id=?", list(fields.values()) + [int(file_id)])
                if "target" in fields:
                    r = self.get(file_id)
                    if r:
                        self._conn().execute("UPDATE files SET session=? WHERE id=?", (f"{r['night']} {r['target'] or 'Unknown'}", file_id))
        return self.get(file_id) or {}

    def forget(self, ids: Iterable[int]) -> None:
        with self._write_lock:
            self._conn().executemany("DELETE FROM files WHERE id=?", [(int(i),) for i in ids])


def _int(v) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _parse_date(v) -> dt.datetime | None:
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            t = dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s[:26], fmt)
            return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _night(t: dt.datetime) -> str:
    tzname = settings().get("location.tz") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        local = t.astimezone(ZoneInfo(tzname))
    except Exception:
        local = t.astimezone()
    if local.hour < 12:
        local -= dt.timedelta(days=1)
    return local.strftime("%Y-%m-%d")


_PLANETS = ["Mercury", "Venus", "Moon", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Sun"]


@functools.lru_cache(maxsize=4096)
def _target_from_text(text: str) -> str | None:
    """Recognise a target in a file or folder name."""
    low = text.lower()
    for p in _PLANETS:
        if re.search(rf"(^|[^a-z]){p.lower()}([^a-z]|$)", low):
            return p
    if re.search(r"(^|[^a-z])(luna|lunar)([^a-z]|$)", low):
        return "Moon"
    from ..astro.catalog import Catalog
    cat = Catalog.default()
    for m in re.finditer(r"(?<![a-z])(m|ngc|ic|c|caldwell|messier)[ _\-]?0*(\d{1,4})(?!\d)", low):
        o = cat.get(f"{m.group(1)} {m.group(2)}")
        if o:
            return o.aliases[0] if o.aliases and o.aliases[0].startswith("M ") else o.id
    words = re.split(r"[^a-z0-9]+", low)
    for n in (3, 2):
        for i in range(len(words) - n + 1):
            o = cat.get(" ".join(words[i:i + n]))
            if o and o.catalog != "stars":
                return o.aliases[0] if o.aliases and o.aliases[0].startswith("M ") else o.id
    return None


def _target(meta: dict, p: Path) -> tuple[str | None, str | None]:
    obj = str(meta.get("OBJECT") or "").strip()
    if obj and obj.lower() not in ("unknown", "untitled", "none", ""):
        t = _target_from_text(obj)
        return (t or obj), "header"
    t = _target_from_text(p.stem)
    if t:
        return t, "filename"
    for parent in list(p.parents)[:3]:
        t = _target_from_text(parent.name)
        if t:
            return t, "folder"
    return None, None


def _imagetype(meta: dict, p: Path) -> str:
    it = str(meta.get("IMAGETYP") or meta.get("FRAME") or "").lower()
    if "master" in it:
        return "master " + ("flat" if "flat" in it else "dark" if "dark" in it else "bias")
    for key in ("flatdark", "dark flat", "darkflat"):
        if key in it:
            return "flatdark"
    for key in ("dark", "flat", "bias", "offset"):
        if key in it:
            return "bias" if key == "offset" else key
    if "stack" in it:
        return "stack"
    path = str(p).lower()
    for key, val in (("flatdark", "flatdark"), ("darkflat", "flatdark"), ("dark", "dark"), ("flat", "flat"), ("bias", "bias")):
        if re.search(rf"(^|[\\/_\-\s]){key}s?([\\/_\-\s.]|$)", path):
            return val
    return "light"


def _status(meta: dict, p: Path, imagetype: str) -> str:
    st = str(meta.get("AFSTATUS") or "").lower()
    if st:
        return st
    if imagetype == "stack" or _int(meta.get("NCOMBINE")) and _int(meta.get("NCOMBINE")) > 1 and "master" not in imagetype:
        return "stacked"
    exports = settings().get("folders.exports")
    if exports and str(p).startswith(str(Path(exports))):
        return "exported"
    return "raw"


_lib: Library | None = None
_lock = threading.Lock()


def library() -> Library:
    global _lib
    with _lock:
        if _lib is None or _lib.db_path != app_home() / "library.db":
            _lib = Library()
        return _lib


# ---------------------------------------------------------------- thumbnails

def thumbnail(file_id: int, size: int = 320) -> Path | None:
    """JPEG thumbnail (cached). Huge videos: only the middle frame is read."""
    import cv2
    import numpy as np
    rec = library().get(file_id)
    if not rec or rec["missing"]:
        return None
    out = cache_dir() / "thumbs" / f"{file_id}_{int(rec['mtime'])}_{size}.jpg"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        img = preview_image(rec["path"], max_size=size)
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 82])
        out.write_bytes(buf.tobytes())
        return out
    except Exception as e:
        log.info("Thumbnail failed for %s: %s", rec["path"], e)
        return None


def preview_image(path: str, max_size: int = 1600, frame: int | None = None, stretch: bool = True):
    """8-bit RGB preview of any supported file (auto-stretched for linear astro data)."""
    import cv2
    import numpy as np
    from ..io.debayer import debayer, superpixel
    from ..io.images import read_image, to_float
    from ..io.source import open_source
    k = kind_of(path)
    if k in ("ser", "video"):
        src = open_source(path)
        try:
            raw = src.get_raw(min(src.count - 1, frame if frame is not None else src.count // 2))
            bayer = src.bayer
        finally:
            src.close()
        meta = {}
    else:
        raw, meta = read_image(path)
        pat = str(meta.get("BAYERPAT") or "").upper()
        bayer = pat if pat in ("RGGB", "BGGR", "GRBG", "GBRG") and raw.ndim == 2 else None
    if bayer and raw.ndim == 2:
        img = superpixel(raw, bayer) if max(raw.shape) > max_size * 2 else debayer(raw, bayer)
        img = to_float(img) if img.dtype != np.float32 else img / (255.0 if raw.dtype == np.uint8 else 65535.0)
    else:
        img = to_float(raw)
    if img.ndim == 2:
        img = np.repeat(img[..., None], 3, axis=2)
    H, W = img.shape[:2]
    f = max_size / max(H, W)
    if f < 1:
        img = cv2.resize(img, (max(1, int(W * f)), max(1, int(H * f))), interpolation=cv2.INTER_AREA)
    if stretch and k not in ("png", "jpg", "webp", "bmp"):
        from ..process.ops import Ctx, run_op
        med = float(np.median(img))
        if med < 0.25:
            img = run_op("auto_stretch", img, {"target": 0.2}, Ctx())
        else:
            lo, hi = np.percentile(img, [0.5, 99.8])
            img = np.clip((img - lo) / max(hi - lo, 1e-6), 0, 1)
    return (np.clip(img, 0, 1) * 255).astype(np.uint8)
