"""Tonight's best targets, rise/set times, and alt-az exposure limits.

For your location and date we find tonight's dark hours (Sun more than 12
degrees below the horizon), then score every candidate - planets, the Moon,
Messier and Caldwell objects and bright NGC/IC objects - by how high it
gets, how long it stays up, how well it fits your camera's field of view
and how much the Moon will interfere.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Iterable

import numpy as np

from .catalog import KIND_GROUPS, Catalog
from .coords import angular_separation, field_rotation_rate_deg_s, max_exposure_for_rotation, radec_to_altaz
from .ephemeris import BODIES, PLANET_INFO, body_position, moon_phase


def _times(start: dt.datetime, end: dt.datetime, step_min: float = 10) -> list[dt.datetime]:
    n = max(2, int((end - start).total_seconds() / 60 / step_min) + 1)
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def night_window(date_local: dt.date, lat: float, lon: float, tz: dt.tzinfo, sun_limit: float = -12.0) -> dict:
    """Dark window for the night starting on ``date_local`` (evening) until next morning."""
    start = dt.datetime.combine(date_local, dt.time(12, 0), tz)
    times = _times(start, start + dt.timedelta(hours=24), 5)
    alts = []
    for t in times:
        s = body_position("sun", t)
        a, _ = radec_to_altaz(s["ra"], s["dec"], t, lat, lon, j2000=True, refraction=False)
        alts.append(float(a))
    alts = np.array(alts)
    dark = alts < sun_limit
    if not dark.any():
        return {"dark_start": None, "dark_end": None, "sunset": None, "sunrise": None,
                "note": "The Sun never gets low enough for full darkness tonight (summer at high latitude)."}
    i0 = int(np.argmax(dark))
    i1 = len(dark) - 1 - int(np.argmax(dark[::-1]))
    below = alts < -0.833
    s0 = int(np.argmax(below)) if below.any() else None
    s1 = len(below) - 1 - int(np.argmax(below[::-1])) if below.any() else None
    return {"dark_start": times[i0], "dark_end": times[i1],
            "sunset": times[s0] if s0 is not None else None, "sunrise": times[s1] if s1 is not None else None, "note": ""}


def altitude_curve(ra: float, dec: float, times: list[dt.datetime], lat: float, lon: float):
    alt, az = [], []
    for t in times:
        a, z = radec_to_altaz(ra, dec, t, lat, lon)
        alt.append(float(a))
        az.append(float(z))
    return np.array(alt), np.array(az)


def object_track(obj: dict, times: list[dt.datetime], lat: float, lon: float) -> tuple[np.ndarray, np.ndarray]:
    if obj.get("solar_system"):
        alt, az = [], []
        for t in times:
            p = body_position(obj["solar_system"], t)
            a, z = radec_to_altaz(p["ra"], p["dec"], t, lat, lon)
            alt.append(float(a))
            az.append(float(z))
        return np.array(alt), np.array(az)
    return altitude_curve(obj["ra"], obj["dec"], times, lat, lon)


def tonight(lat: float, lon: float, tz: dt.tzinfo, date_local: dt.date | None = None, fov_deg: tuple[float, float] = (0.43, 0.24),
            interests: Iterable[str] = ("planet", "moon", "galaxy", "nebula", "cluster"), min_alt: float = 25.0,
            limit: int = 30, include_ngc: bool = True) -> dict:
    """Ranked list of good targets for tonight."""
    now = dt.datetime.now(tz)
    if date_local is None:
        date_local = (now - dt.timedelta(hours=12)).date() if now.hour < 12 else now.date()
    win = night_window(date_local, lat, lon, tz)
    start = win["dark_start"] or dt.datetime.combine(date_local, dt.time(21, 0), tz)
    end = win["dark_end"] or start + dt.timedelta(hours=6)
    times = _times(start, end, 15)
    mid = start + (end - start) / 2
    moon = body_position("moon", mid)
    moon_illum = moon["illumination"]
    interests = set(interests)
    cands: list[dict] = []
    for b in BODIES:
        if b == "sun":
            continue
        kind = "moon" if b == "moon" else "planet"
        if kind not in interests:
            continue
        p = body_position(b, mid)
        cands.append({"id": PLANET_INFO[b]["name"], "name": PLANET_INFO[b]["name"], "kind": "Moon" if b == "moon" else "Planet",
                      "group": kind, "solar_system": b, "ra": p["ra"], "dec": p["dec"], "mag": None,
                      "size_arcmin": p["diameter_arcsec"] / 60, "label": PLANET_INFO[b]["name"]})
    cat = Catalog.default()
    for o in cat.objects:
        grp = KIND_GROUPS.get(o.kind, "other")
        if grp not in interests or o.catalog == "stars":
            continue
        if o.catalog not in ("messier", "caldwell"):
            if not include_ngc or o.mag is None or o.mag > 11.0 or (o.size_arcmin or 0) < 1.5:
                continue
        d = o.to_dict()
        d["group"] = grp
        cands.append(d)
    # Vectorised altitude for all deep-sky candidates at once.
    ras = np.array([c["ra"] for c in cands])
    decs = np.array([c["dec"] for c in cands])
    alts = np.zeros((len(times), len(cands)))
    azs = np.zeros_like(alts)
    for k, t in enumerate(times):
        a, z = radec_to_altaz(ras, decs, t, lat, lon)
        alts[k], azs[k] = a, z
    for j, c in enumerate(cands):
        if c.get("solar_system"):
            a, z = object_track(c, times[::2], lat, lon)
            alts[:, j] = np.interp(np.arange(len(times)), np.arange(0, len(times), 2)[: len(a)], a)
            azs[:, j] = np.interp(np.arange(len(times)), np.arange(0, len(times), 2)[: len(z)], z)
    step_h = (times[1] - times[0]).total_seconds() / 3600
    fov_min = min(fov_deg) * 60
    results = []
    for j, c in enumerate(cands):
        a = alts[:, j]
        best_k = int(np.argmax(a))
        max_alt = float(a[best_k])
        if max_alt < min_alt:
            continue
        hours_up = float(np.sum(a >= min_alt) * step_h)
        msep = float(angular_separation(c["ra"], c["dec"], moon["ra"], moon["dec"])) if c.get("solar_system") != "moon" else 0.0
        score = max_alt / 90 * 40 + min(hours_up, 6) / 6 * 25
        if c["group"] in ("galaxy", "nebula", "cluster"):
            mag = c.get("mag") or 10.0
            score += max(0, 12 - mag) * 2.5
            size = c.get("size_arcmin") or 1
            fit = size / fov_min
            score += 10 if 0.15 <= fit <= 1.2 else 4 if fit < 0.15 else 2
            score -= moon_illum * max(0, 60 - msep) / 60 * 20 * (1.5 if c["group"] != "cluster" else 0.5)
            if c.get("catalog") == "messier":
                score += 6
        elif c["group"] == "planet":
            score += 25 + min(c.get("size_arcmin", 0) * 60, 45) / 45 * 15
        elif c["group"] == "moon":
            score += 20 if 0.15 < moon_illum < 0.95 else 8
        t_best = times[best_k]
        rot_rate = field_rotation_rate_deg_s(max_alt, float(azs[best_k, j]), lat)
        results.append({**{k: c.get(k) for k in ("id", "name", "kind", "group", "ra", "dec", "mag", "size_arcmin", "label", "solar_system", "catalog", "const")},
                        "max_alt": round(max_alt, 1), "best_time": t_best.isoformat(), "hours_up": round(hours_up, 1),
                        "moon_sep": round(msep, 1), "score": round(score, 1),
                        "field_rotation_deg_per_min": round(rot_rate * 60, 3),
                        "altitudes": [round(float(x), 1) for x in a]})
    results.sort(key=lambda r: -r["score"])
    return {"window": {k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in win.items()},
            "times": [t.isoformat() for t in times], "moon": {"illumination": round(moon_illum, 3), "ra": moon["ra"], "dec": moon["dec"]},
            "targets": results[:limit]}


def rotation_advice(ra: float, dec: float, t: dt.datetime, lat: float, lon: float, sensor_w: int, sensor_h: int,
                    tolerance_px: float = 1.0) -> dict:
    """Field-rotation warning and recommended max sub-exposure for an alt-az mount."""
    alt, az = radec_to_altaz(ra, dec, t, lat, lon)
    alt, az = float(alt), float(az)
    rate = field_rotation_rate_deg_s(alt, az, lat)
    tmax = max_exposure_for_rotation(alt, az, lat, sensor_w, sensor_h, tolerance_px)
    tmax_c = min(tmax, 600.0)
    if tmax_c >= 60:
        level, text = "ok", f"Field rotation is slow here: sub-exposures up to {tmax_c:.0f}s stay sharp to the corners."
    elif tmax_c >= 10:
        level, text = "warn", f"Keep sub-exposures under about {tmax_c:.0f}s to avoid star trails in the corners (alt-az field rotation)."
    else:
        level, text = "bad", (f"Field rotation is fast here (target is high near the meridian): use {max(tmax_c, 0.5):.1f}s or shorter "
                              "subs, or wait until the target moves east/west.")
    return {"alt": alt, "az": az, "rate_deg_per_min": rate * 60, "max_exposure_s": tmax_c, "level": level, "text": text}
