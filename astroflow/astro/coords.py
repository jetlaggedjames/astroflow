"""Sky coordinate maths: where things are in your sky, and how fast the field rotates.

* RA/Dec   - a star's fixed address on the sky (like latitude/longitude on Earth).
* Alt/Az   - where to point from where you stand: altitude above the horizon
             and azimuth (compass direction, 0 = north, 90 = east).
* Field rotation - on an alt-az mount the camera stays level with the horizon
             while the sky slowly turns, so long exposures smear into arcs.

Accuracy: ~30 arc-seconds (precession included; nutation/aberration ignored),
far better than needed for GoTo with plate-solve centring.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

SIDEREAL_RATE_DEG_S = 360.0 / 86164.0905     # Earth's rotation relative to the stars
OMEGA_RAD_S = 2 * math.pi / 86164.0905


def julian_date(t: dt.datetime) -> float:
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    t = t.astimezone(dt.timezone.utc)
    return 2440587.5 + t.timestamp() / 86400.0


def gmst_deg(t: dt.datetime) -> float:
    """Greenwich mean sidereal time in degrees (IAU 1982)."""
    jd = julian_date(t)
    T = (jd - 2451545.0) / 36525.0
    g = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T * T - T ** 3 / 38710000.0
    return g % 360.0


def lst_deg(t: dt.datetime, lon_deg: float) -> float:
    return (gmst_deg(t) + lon_deg) % 360.0


def precession_matrix(t: dt.datetime) -> np.ndarray:
    """J2000 -> mean equator of date (IAU 1976, Meeus ch. 21)."""
    T = (julian_date(t) - 2451545.0) / 36525.0
    a = math.radians
    zeta = a((2306.2181 * T + 0.30188 * T * T + 0.017998 * T ** 3) / 3600)
    z = a((2306.2181 * T + 1.09468 * T * T + 0.018203 * T ** 3) / 3600)
    theta = a((2004.3109 * T - 0.42665 * T * T - 0.041833 * T ** 3) / 3600)
    cz, sz = math.cos(zeta), math.sin(zeta)
    cZ, sZ = math.cos(z), math.sin(z)
    ct, st = math.cos(theta), math.sin(theta)
    return np.array([
        [cz * ct * cZ - sz * sZ, -sz * ct * cZ - cz * sZ, -st * cZ],
        [cz * ct * sZ + sz * cZ, -sz * ct * sZ + cz * cZ, -st * sZ],
        [cz * st, -sz * st, ct]])


def _vec(ra_deg, dec_deg):
    ra, dec = np.radians(ra_deg), np.radians(dec_deg)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)])


def _radec(v):
    ra = np.degrees(np.arctan2(v[1], v[0])) % 360
    dec = np.degrees(np.arcsin(np.clip(v[2], -1, 1)))
    return ra, dec


def j2000_to_date(ra_deg, dec_deg, t: dt.datetime):
    return _radec(precession_matrix(t) @ _vec(ra_deg, dec_deg))


def date_to_j2000(ra_deg, dec_deg, t: dt.datetime):
    return _radec(precession_matrix(t).T @ _vec(ra_deg, dec_deg))


def refraction_deg(alt_deg):
    """Atmospheric refraction (Bennett): how much the air lifts objects near the horizon."""
    h = np.maximum(np.asarray(alt_deg, float), -1.0)
    return 1.02 / np.tan(np.radians(h + 10.3 / (h + 5.11))) / 60.0


def radec_to_altaz(ra_deg, dec_deg, t: dt.datetime, lat_deg: float, lon_deg: float,
                   j2000: bool = True, refraction: bool = True):
    """Returns (alt, az) in degrees. Azimuth: 0 = north, 90 = east."""
    ra, dec = (j2000_to_date(ra_deg, dec_deg, t) if j2000 else (ra_deg, dec_deg))
    ha = np.radians((lst_deg(t, lon_deg) - np.asarray(ra)) % 360)
    d, phi = np.radians(dec), math.radians(lat_deg)
    sin_alt = np.sin(d) * math.sin(phi) + np.cos(d) * math.cos(phi) * np.cos(ha)
    alt = np.degrees(np.arcsin(np.clip(sin_alt, -1, 1)))
    az = np.degrees(np.arctan2(-np.cos(d) * np.sin(ha),
                               np.sin(d) * math.cos(phi) - np.cos(d) * math.sin(phi) * np.cos(ha))) % 360
    if refraction:
        alt = alt + np.where(alt > -1, refraction_deg(alt), 0)
    return alt, az


def altaz_to_radec(alt_deg, az_deg, t: dt.datetime, lat_deg: float, lon_deg: float,
                   j2000: bool = True, refraction: bool = True):
    alt = np.asarray(alt_deg, float)
    if refraction:
        alt = alt - np.where(alt > -1, refraction_deg(alt - refraction_deg(alt)), 0)
    a, A, phi = np.radians(alt), np.radians(az_deg), math.radians(lat_deg)
    sin_dec = np.sin(a) * math.sin(phi) + np.cos(a) * math.cos(phi) * np.cos(A)
    dec = np.degrees(np.arcsin(np.clip(sin_dec, -1, 1)))
    ha = np.degrees(np.arctan2(-np.sin(A) * np.cos(a), np.sin(a) * math.cos(phi) - np.cos(a) * math.sin(phi) * np.cos(A)))
    ra = (lst_deg(t, lon_deg) - ha) % 360
    if j2000:
        return date_to_j2000(ra, dec, t)
    return ra, dec


def parallactic_angle(ra_deg, dec_deg, t: dt.datetime, lat_deg: float, lon_deg: float) -> float:
    ra, dec = j2000_to_date(ra_deg, dec_deg, t)
    ha = math.radians((lst_deg(t, lon_deg) - float(ra)) % 360)
    d, phi = math.radians(float(dec)), math.radians(lat_deg)
    return math.degrees(math.atan2(math.sin(ha), math.tan(phi) * math.cos(d) - math.sin(d) * math.cos(ha)))


def field_rotation_rate_deg_s(alt_deg: float, az_deg: float, lat_deg: float) -> float:
    """How fast the sky turns in the camera on an alt-az mount (deg/second).

    rate = Earth's rotation * cos(latitude) * cos(azimuth) / cos(altitude).
    Fastest near the meridian (north/south) and high up; zero due east/west.
    """
    alt = math.radians(min(alt_deg, 89.5))
    return math.degrees(OMEGA_RAD_S * math.cos(math.radians(lat_deg)) * math.cos(math.radians(az_deg)) / math.cos(alt))


def max_exposure_for_rotation(alt_deg: float, az_deg: float, lat_deg: float, sensor_w_px: int, sensor_h_px: int,
                              tolerance_px: float = 1.0) -> float:
    """Longest single exposure before stars in the frame corners smear by more than ``tolerance_px``.

    A star at radius r from the centre moves r * angle; the corners (half the diagonal) move most.
    """
    rate = abs(math.radians(field_rotation_rate_deg_s(alt_deg, az_deg, lat_deg)))
    if rate < 1e-12:
        return float("inf")
    r = 0.5 * math.hypot(sensor_w_px, sensor_h_px)
    return tolerance_px / (r * rate)


def angular_separation(ra1, dec1, ra2, dec2):
    """Degrees between two sky positions."""
    r1, d1, r2, d2 = map(np.radians, (ra1, dec1, ra2, dec2))
    s = np.sin((d2 - d1) / 2) ** 2 + np.cos(d1) * np.cos(d2) * np.sin((r2 - r1) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(np.clip(s, 0, 1))))


def format_ra(ra_deg: float) -> str:
    h = (ra_deg % 360) / 15
    hh = int(h)
    mm = int((h - hh) * 60)
    ss = ((h - hh) * 60 - mm) * 60
    return f"{hh:02d}h {mm:02d}m {ss:04.1f}s"


def format_dec(dec_deg: float) -> str:
    s = "-" if dec_deg < 0 else "+"
    d = abs(dec_deg)
    dd = int(d)
    mm = int((d - dd) * 60)
    ss = ((d - dd) * 60 - mm) * 60
    return f"{s}{dd:02d}° {mm:02d}' {ss:04.1f}\""


def parse_ra(s: str | float) -> float:
    """'10h 41m 2s', '10:41:02', '160.25' (degrees) -> degrees."""
    if isinstance(s, (int, float)):
        return float(s)
    t = s.strip().lower().replace("h", " ").replace("m", " ").replace("s", " ").replace(":", " ")
    parts = [float(x) for x in t.split()]
    if len(parts) == 1:
        return parts[0]
    while len(parts) < 3:
        parts.append(0.0)
    return (parts[0] + parts[1] / 60 + parts[2] / 3600) * 15


def parse_dec(s: str | float) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    t = s.strip().replace("°", " ").replace("d", " ").replace("'", " ").replace('"', " ").replace(":", " ")
    neg = t.strip().startswith("-")
    parts = [abs(float(x)) for x in t.replace("+", " ").replace("-", " ").split()]
    while len(parts) < 3:
        parts.append(0.0)
    v = parts[0] + parts[1] / 60 + parts[2] / 3600
    return -v if neg else v
