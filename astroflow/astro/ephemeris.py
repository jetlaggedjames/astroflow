"""Positions of the Sun, Moon and planets - fully offline.

Uses astropy's built-in ephemeris (no download). Astropy's automatic
download of Earth-rotation tables is switched off; the small loss of
accuracy (well under an arc-second for our purposes) doesn't matter.
"""
from __future__ import annotations

import datetime as dt
import functools
import logging
import math
import warnings

log = logging.getLogger("astroflow.ephemeris")

BODIES = ["sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune"]
PLANET_INFO = {
    "mercury": {"name": "Mercury", "radius_km": 2439.7},
    "venus": {"name": "Venus", "radius_km": 6051.8},
    "mars": {"name": "Mars", "radius_km": 3389.5},
    "jupiter": {"name": "Jupiter", "radius_km": 71492.0},
    "saturn": {"name": "Saturn", "radius_km": 60268.0},
    "uranus": {"name": "Uranus", "radius_km": 25559.0},
    "neptune": {"name": "Neptune", "radius_km": 24764.0},
    "moon": {"name": "Moon", "radius_km": 1737.4},
    "sun": {"name": "Sun", "radius_km": 695700.0},
}
AU_KM = 149597870.7


@functools.lru_cache(maxsize=1)
def _astropy():
    from astropy.utils import iers
    iers.conf.auto_download = False
    iers.conf.auto_max_age = None
    from astropy import units as u
    from astropy.coordinates import get_body, solar_system_ephemeris
    from astropy.time import Time
    return u, get_body, solar_system_ephemeris, Time


@functools.lru_cache(maxsize=4096)
def _body_cached(body: str, t_key: int) -> tuple[float, float, float]:
    u, get_body, sse, Time = _astropy()
    tm = Time(dt.datetime.fromtimestamp(t_key, dt.timezone.utc))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with sse.set("builtin"):
            c = get_body(body, tm)  # geocentric, GCRS ~ J2000 orientation
            return float(c.ra.deg), float(c.dec.deg), float(c.distance.to(u.km).value)


def body_position(body: str, t: dt.datetime) -> dict:
    """RA/Dec (J2000-oriented, geocentric), distance and apparent size of a solar-system body.

    Cached to the minute for speed (the Moon moves ~0.5 arcmin per minute).
    """
    body = body.lower()
    if body not in BODIES:
        raise ValueError(f"Unknown body {body}")
    ts = int(t.timestamp()) // 60 * 60
    ra, dec, dist_km = _body_cached(body, ts)
    radius = PLANET_INFO[body]["radius_km"]
    diam_arcsec = 2 * math.degrees(math.atan(radius / dist_km)) * 3600
    out = {"body": body, "name": PLANET_INFO[body]["name"], "ra": ra, "dec": dec, "distance_km": dist_km,
           "distance_au": dist_km / AU_KM, "diameter_arcsec": diam_arcsec}
    if body == "moon":
        out.update(moon_phase(t))
    return out


def moon_phase(t: dt.datetime) -> dict:
    """Illuminated fraction (0 = new, 1 = full) and whether waxing."""
    from .coords import angular_separation
    s_ra, s_dec, s_d = _body_cached("sun", int(t.timestamp()) // 60 * 60)
    m_ra, m_dec, m_d = _body_cached("moon", int(t.timestamp()) // 60 * 60)
    elong = math.radians(float(angular_separation(s_ra, s_dec, m_ra, m_dec)))
    phase_angle = math.atan2(s_d * math.sin(elong), m_d - s_d * math.cos(elong))
    illum = (1 + math.cos(phase_angle)) / 2
    later = _body_cached("moon", int(t.timestamp() + 6 * 3600) // 60 * 60)
    later_sun = _body_cached("sun", int(t.timestamp() + 6 * 3600) // 60 * 60)
    elong2 = float(angular_separation(later_sun[0], later_sun[1], later[0], later[1]))
    return {"illumination": illum, "waxing": elong2 > math.degrees(elong)}
