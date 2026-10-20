"""Object catalogues: deep-sky objects, named stars, planets - searchable offline.

Search accepts what people type: "M31", "m 31", "messier 31", "NGC224",
"ngc 224", "C14", "Andromeda", "orion nebula", "Vega", "jupiter".
"""
from __future__ import annotations

import csv
import functools
import gzip
import re
import unicodedata
from dataclasses import dataclass, field

import numpy as np

from ..config import bundled_data_dir, data_dir
from .coords import angular_separation
from .ephemeris import BODIES, PLANET_INFO

KIND_GROUPS = {
    "Galaxy": "galaxy", "Galaxy pair": "galaxy", "Galaxy triplet": "galaxy", "Galaxy group": "galaxy",
    "Open cluster": "cluster", "Globular cluster": "cluster", "Stellar association": "cluster", "Cluster with nebula": "nebula",
    "Planetary nebula": "nebula", "Emission nebula": "nebula", "Reflection nebula": "nebula", "Dark nebula": "nebula",
    "Nebula": "nebula", "Supernova remnant": "nebula", "Star": "star", "Double star": "star", "Nova": "star", "Other": "other",
}


@dataclass
class SkyObject:
    id: str
    name: str
    kind: str
    ra: float
    dec: float
    mag: float | None = None
    size_arcmin: float | None = None
    size2_arcmin: float | None = None
    pa: float | None = None
    const: str = ""
    aliases: list[str] = field(default_factory=list)
    catalog: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "ra": self.ra, "dec": self.dec,
                "mag": self.mag, "size_arcmin": self.size_arcmin, "size2_arcmin": self.size2_arcmin, "pa": self.pa,
                "const": self.const, "aliases": self.aliases, "catalog": self.catalog,
                "label": self.label}

    @property
    def label(self) -> str:
        main = next((a for a in self.aliases if a.startswith("M ")), None) or self.id
        return f"{main} ({self.name})" if self.name and self.name != main else main


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^(messier|mess)\s*", "m ", s)
    s = re.sub(r"^caldwell\s*", "c ", s)
    s = re.sub(r"^(m|ngc|ic|c)\s*0*(\d+)", r"\1 \2", s)
    return s


def _f(v: str) -> float | None:
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


class Catalog:
    def __init__(self):
        self.objects: list[SkyObject] = []
        self.index: dict[str, int] = {}
        self._ra = self._dec = None

    @classmethod
    @functools.lru_cache(maxsize=1)
    def default(cls) -> "Catalog":
        c = cls()
        c._load_dso()
        c._load_named_stars()
        c._finish()
        return c

    def _load_dso(self) -> None:
        path = bundled_data_dir() / "dso.csv.gz"
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cal = r["caldwell"].strip()
                aliases = [a for a in [r["messier"]] if a] + ([c.strip() for c in re.findall(r"C \d+", cal)])
                names = [n.strip() for n in r["names"].split(",") if n.strip()]
                cat = "messier" if r["messier"] else ("caldwell" if cal else ("ic" if r["id"].startswith("IC") else "ngc"))
                self.objects.append(SkyObject(r["id"], names[0] if names else "", r["type"], float(r["ra"]), float(r["dec"]),
                                              _f(r["mag"]), _f(r["maj"]), _f(r["min"]), _f(r["pa"]), r["const"],
                                              aliases + names[1:], cat))

    def _load_named_stars(self) -> None:
        path = bundled_data_dir() / "stars.csv.gz"
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if not r["name"]:
                    continue
                self.objects.append(SkyObject(r["name"], r["name"], "Star", float(r["ra"]), float(r["dec"]),
                                              _f(r["mag"]), None, None, None, "", [r["desig"]] if r["desig"] else [], "stars"))

    def _finish(self) -> None:
        for i, o in enumerate(self.objects):
            for key in [o.id, o.name, *o.aliases]:
                k = _norm(key)
                if k and k not in self.index:
                    self.index[k] = i
        self._ra = np.array([o.ra for o in self.objects])
        self._dec = np.array([o.dec for o in self.objects])

    def get(self, query: str) -> SkyObject | None:
        k = _norm(query)
        i = self.index.get(k)
        return self.objects[i] if i is not None else None

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Name search; planets and the Moon are included."""
        q = _norm(query)
        if not q:
            return []
        out: list[dict] = []
        for b in BODIES:
            if b != "sun" and (b.startswith(q) or PLANET_INFO[b]["name"].lower().startswith(q)):
                out.append({"id": PLANET_INFO[b]["name"], "name": PLANET_INFO[b]["name"], "kind": "Planet" if b != "moon" else "Moon",
                            "solar_system": b, "label": PLANET_INFO[b]["name"], "catalog": "planets"})
        exact = self.index.get(q)
        seen = set()
        if exact is not None:
            out.append(self.objects[exact].to_dict())
            seen.add(exact)
        prefix = [i for k, i in self.index.items() if k.startswith(q) and i not in seen]
        designation = re.match(r"^(m|ngc|ic|c) \d+$", q) is not None
        contains = [] if designation else [i for k, i in self.index.items() if q in k and not k.startswith(q) and len(q) >= 3]
        def rank(i):
            o = self.objects[i]
            return (0 if o.catalog == "messier" else 1 if o.catalog == "caldwell" else 2 if o.catalog == "stars" else 3,
                    o.mag if o.mag is not None else 99)
        for i in sorted(set(prefix), key=rank) + sorted(set(contains) - set(prefix), key=rank):
            if i in seen:
                continue
            seen.add(i)
            out.append(self.objects[i].to_dict())
            if len(out) >= limit:
                break
        return out[:limit]

    def in_field(self, ra: float, dec: float, radius_deg: float, max_mag: float | None = None,
                 include_stars: bool = True, limit: int = 200) -> list[SkyObject]:
        """Objects within ``radius_deg`` of a position (brightest / biggest first)."""
        if self._ra is None:
            return []
        cosd = max(np.cos(np.radians(dec)), 1e-3)
        pre = (np.abs(self._dec - dec) <= radius_deg + 1) & (np.abs(((self._ra - ra + 180) % 360) - 180) * cosd <= radius_deg + 1)
        idx = np.nonzero(pre)[0]
        sep = angular_separation(ra, dec, self._ra[idx], self._dec[idx])
        sel = idx[sep <= radius_deg]
        objs = [self.objects[i] for i in sel]
        if not include_stars:
            objs = [o for o in objs if o.catalog != "stars"]
        if max_mag is not None:
            objs = [o for o in objs if o.mag is None or o.mag <= max_mag]
        objs.sort(key=lambda o: (o.catalog not in ("messier", "caldwell"), -(o.size_arcmin or 0), o.mag or 99))
        return objs[:limit]


@functools.lru_cache(maxsize=1)
def star_table() -> dict[str, np.ndarray]:
    """All bundled stars to magnitude 9 (positions, magnitudes, B-V colour)."""
    ra, dec, mag, bv = [], [], [], []
    with gzip.open(bundled_data_dir() / "stars.csv.gz", "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ra.append(float(r["ra"]))
            dec.append(float(r["dec"]))
            mag.append(float(r["mag"]))
            bv.append(float(r["bv"]) if r["bv"] else 0.65)
    return {"ra": np.array(ra), "dec": np.array(dec), "mag": np.array(mag), "bv": np.array(bv)}


# ------------------------------------------------------------------ cities ---

@functools.lru_cache(maxsize=1)
def _cities() -> list[tuple[str, str, str, float, float, str]]:
    rows = []
    with gzip.open(bundled_data_dir() / "cities.csv.gz", "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append((r["name"], r["region"], r["country"], float(r["lat"]), float(r["lon"]), _norm(r["name"])))
    return rows


def search_cities(query: str, limit: int = 15) -> list[dict]:
    """Offline place search ("type your city"). Accepts 'Springfield, IL' style input."""
    q = query.split(",")
    name_q = _norm(q[0])
    extra = _norm(" ".join(q[1:])) if len(q) > 1 else ""
    if len(name_q) < 2:
        return []
    exact, prefix = [], []
    for row in _cities():
        n = row[5]
        if n == name_q:
            exact.append(row)
        elif n.startswith(name_q):
            prefix.append(row)
    res = exact + sorted(prefix, key=lambda r: len(r[0]))
    if extra:
        res = [r for r in res if extra in _norm(r[1]) or extra == _norm(r[2]) or _norm(r[1]).startswith(extra)] or res
    out = []
    for name, region, country, lat, lon, _ in res[:limit]:
        out.append({"name": name, "region": region, "country": country, "lat": lat, "lon": lon,
                    "label": ", ".join(x for x in (name, region, country) if x), "tz": timezone_at(lat, lon)})
    return out


def timezone_at(lat: float, lon: float) -> str:
    """IANA time zone for a place, offline (timezonefinder), or a rough UTC offset."""
    try:
        from timezonefinder import TimezoneFinder
        tf = _tzf()
        tz = tf.timezone_at(lat=lat, lng=lon)
        if tz:
            return tz
    except Exception:
        pass
    off = round(lon / 15)
    return "UTC" if off == 0 else f"Etc/GMT{'-' if off > 0 else '+'}{abs(off)}"


@functools.lru_cache(maxsize=1)
def _tzf():
    from timezonefinder import TimezoneFinder
    return TimezoneFinder()
