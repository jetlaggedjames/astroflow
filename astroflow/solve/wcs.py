"""A plate-solve result ("WCS": World Coordinate System) - which sky position each pixel shows."""
from __future__ import annotations

import json
import math
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from ..config import atomic_write_text


@dataclass
class Solution:
    ra: float                    # centre of the image, degrees (J2000)
    dec: float
    scale_arcsec: float          # arc-seconds per pixel
    rotation_deg: float          # angle of celestial north, measured from image "up" towards east
    flipped: bool
    width: int
    height: int
    header: dict = field(default_factory=dict)  # FITS WCS keywords (CRVAL, CRPIX, CD, optional SIP)
    solver: str = ""
    seconds: float = 0.0
    stars_matched: int | None = None

    @property
    def fov_deg(self) -> tuple[float, float]:
        return self.width * self.scale_arcsec / 3600, self.height * self.scale_arcsec / 3600

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fov_deg"] = self.fov_deg
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Solution":
        keys = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in keys})

    # ---------------------------------------------------------- astropy ---
    def wcs(self):
        from astropy.wcs import WCS
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hdr = {k: v for k, v in self.header.items()}
            hdr.setdefault("NAXIS", 2)
            hdr["NAXIS1"], hdr["NAXIS2"] = self.width, self.height
            return WCS(hdr, relax=True)

    def pix2world(self, x, y):
        """Pixel (0-based, top-left origin as shown in the app) -> (ra, dec) degrees."""
        w = self.wcs()
        ra, dec = w.all_pix2world(np.asarray(x, float), np.asarray(y, float), 0)
        return ra, dec

    def world2pix(self, ra, dec):
        w = self.wcs()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            x, y = w.all_world2pix(np.asarray(ra, float), np.asarray(dec, float), 0, quiet=True)
        return x, y

    @classmethod
    def from_header(cls, hdr: dict, width: int, height: int, solver: str = "") -> "Solution":
        keep = {k: hdr[k] for k in hdr if _is_wcs_key(k)}
        keep.setdefault("CTYPE1", "RA---TAN")
        keep.setdefault("CTYPE2", "DEC--TAN")
        if "CD1_1" not in keep and "CDELT1" in keep:
            # Convert CDELT/CROTA form to a CD matrix.
            cd1, cd2 = float(keep["CDELT1"]), float(keep["CDELT2"])
            rot = math.radians(float(keep.get("CROTA2", 0.0)))
            keep["CD1_1"], keep["CD1_2"] = cd1 * math.cos(rot), -cd2 * math.sin(rot)
            keep["CD2_1"], keep["CD2_2"] = cd1 * math.sin(rot), cd2 * math.cos(rot)
        cd = np.array([[float(keep["CD1_1"]), float(keep.get("CD1_2", 0))],
                       [float(keep.get("CD2_1", 0)), float(keep["CD2_2"])]])
        scale = math.sqrt(abs(np.linalg.det(cd))) * 3600
        tmp = cls(0, 0, scale, 0, False, width, height, keep, solver)
        cra, cdec = tmp.pix2world(width / 2 - 0.5, height / 2 - 0.5)
        tmp.ra, tmp.dec = float(cra) % 360, float(cdec)
        tmp.rotation_deg, tmp.flipped = _orientation(tmp)
        return tmp

    def to_header(self) -> dict:
        return dict(self.header)

    def save_sidecar(self, image_path: str | Path) -> Path:
        p = Path(str(image_path) + ".wcs.json")
        atomic_write_text(p, json.dumps(self.to_dict(), indent=1, default=float))
        return p

    @classmethod
    def load_sidecar(cls, image_path: str | Path) -> "Solution | None":
        p = Path(str(image_path) + ".wcs.json")
        if not p.exists():
            return None
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return None


_WCS_PREFIX = ("CRVAL", "CRPIX", "CD1_", "CD2_", "CDELT", "CROTA", "CTYPE", "CUNIT", "A_", "B_", "AP_", "BP_",
               "LONPOLE", "LATPOLE", "RADESYS", "EQUINOX", "PC1_", "PC2_")


def _is_wcs_key(k: str) -> bool:
    return any(k.startswith(p) for p in _WCS_PREFIX) or k in ("A_ORDER", "B_ORDER", "AP_ORDER", "BP_ORDER")


def _orientation(sol: Solution) -> tuple[float, bool]:
    """North angle (degrees from image-up toward east... measured clockwise) and mirror flag."""
    cx, cy = sol.width / 2, sol.height / 2
    ra0, dec0 = sol.pix2world(cx, cy)
    # A point a little to the north, and one a little to the east.
    nx, ny = sol.world2pix(ra0, float(dec0) + 0.01)
    ex, ey = sol.world2pix(float(ra0) + 0.01 / max(math.cos(math.radians(float(dec0))), 1e-3), dec0)
    vn = np.array([float(nx) - cx, float(ny) - cy])
    ve = np.array([float(ex) - cx, float(ey) - cy])
    # Image y grows downward: "up" is (0, -1). Angle of north from up, clockwise.
    ang = math.degrees(math.atan2(vn[0], -vn[1]))
    # Normal sky view (not mirrored): east is 90 degrees counter-clockwise from north.
    cross = vn[0] * ve[1] - vn[1] * ve[0]
    flipped = cross > 0
    return ang % 360, bool(flipped)


def tan_header(ra: float, dec: float, scale_arcsec: float, rotation_deg: float, width: int, height: int,
               flipped: bool = False) -> dict:
    """A simple tangent-plane WCS header (used by the simulator and the built-in solver).

    rotation_deg: angle of north, clockwise from image "up". Not flipped = the sky as seen
    (east to the left of north).
    """
    s = scale_arcsec / 3600
    sx = s if flipped else -s
    cd = _cd_from(sx, -s, math.radians(rotation_deg if flipped else -rotation_deg))
    return {"CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CRVAL1": ra, "CRVAL2": dec,
            "CRPIX1": width / 2 + 0.5, "CRPIX2": height / 2 + 0.5,
            "CD1_1": cd[0, 0], "CD1_2": cd[0, 1], "CD2_1": cd[1, 0], "CD2_2": cd[1, 1],
            "CUNIT1": "deg", "CUNIT2": "deg", "RADESYS": "ICRS", "EQUINOX": 2000.0}


def _cd_from(sx: float, sy: float, th: float) -> np.ndarray:
    """CD = R(th) @ diag(sx, sy): pixel offsets -> (xi, eta) degrees."""
    c, s = math.cos(th), math.sin(th)
    R = np.array([[c, -s], [s, c]])
    return R @ np.diag([sx, sy])
