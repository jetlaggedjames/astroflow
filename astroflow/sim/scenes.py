"""Synthetic sky scenes for the simulated camera and the automated tests.

Planets are rendered as real rotating spheres (so Jupiter's Red Spot moves
over minutes, which tests de-rotation), then blurred and warped by fake
atmospheric turbulence that changes every frame (so some frames are sharp
and some are not - exactly what lucky imaging needs).
Deep-sky frames are star fields that rotate over time (alt-az field
rotation) with sky gradient, vignetting, hot pixels, noise and the odd
satellite trail.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

ROTATION_PERIOD_S = {"jupiter": 35740.6, "saturn": 38018.0, "mars": 88642.7}


def _value_noise(shape: tuple[int, int], scale: float, rng: np.random.Generator, octaves: int = 4) -> np.ndarray:
    """Smooth fractal noise in [-1, 1]."""
    h, w = shape
    out = np.zeros(shape, np.float32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        s = max(2, int(scale / (2 ** o)))
        g = rng.standard_normal((h // s + 3, w // s + 3)).astype(np.float32)
        up = cv2.resize(g, ((w // s + 3) * s, (h // s + 3) * s), interpolation=cv2.INTER_CUBIC)[:h, :w]
        out += amp * up
        total += amp
        amp *= 0.5
    out /= total
    return np.clip(out / (np.std(out) * 3 + 1e-9), -1, 1)


@dataclass
class PlanetScene:
    """Renders a planet/Moon 'truth' image (RGB float 0..1) at time t (seconds)."""
    target: str = "jupiter"
    size: tuple[int, int] = (480, 640)     # H, W of the output frame
    radius: float = 120.0                  # equatorial radius in pixels
    seed: int = 7
    pole_angle_deg: float = 0.0
    _tex: dict = field(default_factory=dict)

    def __post_init__(self):
        rng = np.random.default_rng(self.seed)
        # Surface texture on a (lat, lon) grid: 512 x 1024
        self._tex["noise"] = _value_noise((512, 1024), 64, rng, 5)
        self._tex["fine"] = _value_noise((512, 1024), 12, rng, 3)
        if self.target == "moon":
            self._tex["moon"] = self._moon_texture(rng)

    # -- spheres ----------------------------------------------------------
    def _sphere(self, t: float, flattening: float):
        H, W = self.size
        cy, cx = H / 2.0, W / 2.0
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        phi = math.radians(self.pole_angle_deg)
        e = np.array([math.cos(phi), math.sin(phi)])
        p = np.array([math.sin(phi), -math.cos(phi)])
        u = ((xx - cx) * e[0] + (yy - cy) * e[1]) / self.radius
        v = ((xx - cx) * p[0] + (yy - cy) * p[1]) / (self.radius * (1 - flattening))
        r2 = u * u + v * v
        inside = r2 < 1.0
        z = np.sqrt(np.clip(1 - r2, 0, 1))
        lat = np.arcsin(np.clip(v, -1, 1))
        period = ROTATION_PERIOD_S.get(self.target, 1e12)
        lon = np.arctan2(u, z) - 2 * math.pi * t / period
        return inside, z, lat, lon, u, v

    def _sample(self, tex: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        th, tw = tex.shape[:2]
        mx = ((lon / (2 * math.pi)) % 1.0) * tw
        my = (0.5 - lat / math.pi) * th
        return cv2.remap(tex, mx.astype(np.float32), my.astype(np.float32), cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)

    def render(self, t: float = 0.0) -> np.ndarray:
        if self.target == "jupiter":
            return self._jupiter(t)
        if self.target == "saturn":
            return self._saturn(t)
        if self.target == "mars":
            return self._mars(t)
        if self.target == "moon":
            return self._moon(t)
        raise ValueError(self.target)

    def _jupiter(self, t: float) -> np.ndarray:
        inside, z, lat, lon, u, v = self._sphere(t, 0.0649)
        n = self._sample(self._tex["noise"], lat, lon)
        f = self._sample(self._tex["fine"], lat, lon)
        latd = np.degrees(lat) + 3.0 * n  # turbulent band edges
        band = 0.5 + 0.5 * np.cos(np.radians(latd) * 9.0)
        base = np.stack([0.85 - 0.25 * band, 0.78 - 0.30 * band, 0.66 - 0.32 * band], -1)
        base += 0.05 * f[..., None]
        # Great Red Spot near latitude -22 deg
        dlon = ((lon - 1.0 + math.pi) % (2 * math.pi)) - math.pi
        grs = np.exp(-((dlon / 0.22) ** 2 + ((lat + math.radians(22)) / 0.07) ** 2))
        base[..., 0] += 0.10 * grs
        base[..., 1] -= 0.18 * grs
        base[..., 2] -= 0.25 * grs
        limb = (z ** 0.45)[..., None]
        img = np.clip(base * limb, 0, 1) * inside[..., None]
        return img.astype(np.float32) * 0.85

    def _saturn(self, t: float) -> np.ndarray:
        inside, z, lat, lon, u, v = self._sphere(t, 0.098)
        n = self._sample(self._tex["noise"], lat, lon)
        latd = np.degrees(lat) + 1.5 * n
        band = 0.5 + 0.5 * np.cos(np.radians(latd) * 7.0)
        base = np.stack([0.88 - 0.12 * band, 0.80 - 0.15 * band, 0.58 - 0.15 * band], -1)
        img = base * (z ** 0.4)[..., None] * inside[..., None]
        # Rings: ellipse annuli tilted 20 deg toward us
        H, W = self.size
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        tilt = math.radians(20)
        rx = (xx - W / 2) / self.radius
        ry = (yy - H / 2) / (self.radius * math.sin(tilt))
        rr = np.sqrt(rx * rx + ry * ry)
        ring = ((rr > 1.24) & (rr < 2.27)).astype(np.float32)
        ring *= np.where((rr > 1.95) & (rr < 2.02), 0.05, 1.0)          # Cassini division
        ring *= np.where(rr < 1.53, 0.35, 1.0)                          # faint C ring
        ring *= 0.85 + 0.15 * np.cos(rr * 40)
        front = ry > 0
        ring_col = np.stack([ring * 0.85, ring * 0.78, ring * 0.6], -1)
        # The ring is hidden where it passes behind the globe.
        visible = (ring > 0) & ~(inside & ~front)
        img = np.where(visible[..., None], ring_col, img)
        return np.clip(img, 0, 1).astype(np.float32) * 0.8

    def _mars(self, t: float) -> np.ndarray:
        inside, z, lat, lon, u, v = self._sphere(t, 0.005)
        n = self._sample(self._tex["noise"], lat, lon)
        dark = np.clip(n * 1.5, 0, 1)
        base = np.stack([0.85 - 0.35 * dark, 0.45 - 0.2 * dark, 0.25 - 0.1 * dark], -1)
        cap = np.clip((np.degrees(lat) - 70) / 5, 0, 1)[..., None]
        base = base * (1 - cap) + 0.95 * cap
        return (np.clip(base * (z ** 0.3)[..., None], 0, 1) * inside[..., None]).astype(np.float32) * 0.85

    def _moon_texture(self, rng: np.random.Generator) -> np.ndarray:
        S = 1024
        hgt = 0.15 * _value_noise((S, S), 128, rng, 6)
        yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
        for _ in range(900):
            r = float(np.clip(rng.pareto(1.8) * 4 + 2, 2, 120))
            cx, cy = rng.uniform(0, S, 2)
            x0, x1 = int(max(cx - 2 * r, 0)), int(min(cx + 2 * r, S))
            y0, y1 = int(max(cy - 2 * r, 0)), int(min(cy + 2 * r, S))
            if x1 <= x0 or y1 <= y0:
                continue
            d = np.hypot(xx[y0:y1, x0:x1] - cx, yy[y0:y1, x0:x1] - cy) / r
            bowl = np.where(d < 1, -(1 - d * d) * 0.6, 0) + np.exp(-((d - 1) / 0.15) ** 2) * 0.35
            hgt[y0:y1, x0:x1] += bowl * (r / 60)
        maria = np.clip(_value_noise((S, S), 300, rng, 3), 0, 1)
        return np.dstack([hgt, maria]).astype(np.float32)

    def _moon(self, t: float) -> np.ndarray:
        """A close-up of the lunar surface (fills the frame), lit from the left."""
        H, W = self.size
        tex = self._tex["moon"]
        hgt = cv2.resize(tex[..., 0], (W, H), interpolation=cv2.INTER_CUBIC) if (H, W) != tex.shape[:2] else tex[..., 0]
        maria = cv2.resize(tex[..., 1], (W, H), interpolation=cv2.INTER_CUBIC) if (H, W) != tex.shape[:2] else tex[..., 1]
        gx = cv2.Sobel(hgt, cv2.CV_32F, 1, 0, ksize=3) * 6
        gy = cv2.Sobel(hgt, cv2.CV_32F, 0, 1, ksize=3) * 6
        sun = np.array([-0.8, -0.2, 0.55])
        nrm = np.dstack([-gx, -gy, np.ones_like(gx)])
        nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
        shade = np.clip(nrm @ sun / np.linalg.norm(sun), 0, 1)
        albedo = 0.75 - 0.35 * maria
        g = shade * albedo
        rgb = np.dstack([g * 1.0, g * 0.97, g * 0.92 + 0.02 * maria])
        return np.clip(rgb, 0, 1).astype(np.float32) * 0.9


class Turbulence:
    """Per-frame atmospheric distortion: tip-tilt jitter, local warping and blur."""

    def __init__(self, shape: tuple[int, int], seeing: float = 1.0, seed: int = 0):
        self.shape = shape
        self.seeing = seeing
        self.rng = np.random.default_rng(seed)
        H, W = shape
        self.yy, self.xx = np.mgrid[0:H, 0:W].astype(np.float32)
        self.drift = np.zeros(2)

    def apply(self, img: np.ndarray) -> tuple[np.ndarray, float]:
        H, W = self.shape
        rng = self.rng
        # Seeing changes moment to moment: log-normal blur.
        blur = float(np.clip(rng.lognormal(math.log(1.2 * self.seeing), 0.45), 0.3, 6.0))
        amp = 1.8 * self.seeing * float(rng.lognormal(0, 0.4))
        self.drift += rng.normal(0, 0.15, 2)  # slow wander of the image in the frame
        self.drift *= 0.995
        jitter = rng.normal(0, 1.2 * self.seeing, 2) + self.drift
        g = rng.standard_normal((6, 8)).astype(np.float32)
        dx = cv2.resize(g, (W, H), interpolation=cv2.INTER_CUBIC) * amp
        g = rng.standard_normal((6, 8)).astype(np.float32)
        dy = cv2.resize(g, (W, H), interpolation=cv2.INTER_CUBIC) * amp
        mx = (self.xx + dx + np.float32(jitter[0])).astype(np.float32)
        my = (self.yy + dy + np.float32(jitter[1])).astype(np.float32)
        self.last_field = (mx - self.xx, my - self.yy)
        warped = cv2.remap(img, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        out = cv2.GaussianBlur(warped, (0, 0), blur)
        return out, blur


def sensor_readout(rgb: np.ndarray, bit_depth: int = 12, gain: float = 1.0, bayer: str | None = "RGGB",
                   read_noise: float = 0.003, rng: np.random.Generator | None = None,
                   out_dtype=np.uint16, photon_scale: float = 4000.0) -> np.ndarray:
    """Turn a float RGB scene into raw camera data (Bayer mosaic, noise, quantisation)."""
    rng = rng or np.random.default_rng()
    img = np.clip(rgb * gain, 0, None)
    if bayer:
        mono = np.zeros(img.shape[:2], np.float32)
        idx = {"R": 0, "G": 1, "B": 2}
        for i, ch in enumerate(bayer):
            mono[i // 2::2, i % 2::2] = img[i // 2::2, i % 2::2, idx[ch]]
        img = mono
    elif img.ndim == 3:
        img = img.mean(axis=2)
    # Shot noise (Poisson approximated by Gaussian) + read noise
    img = img + rng.standard_normal(img.shape).astype(np.float32) * np.sqrt(np.clip(img, 0, None) / photon_scale)
    img = img + rng.standard_normal(img.shape).astype(np.float32) * read_noise
    img = np.clip(img, 0, 1)
    levels = 2 ** bit_depth - 1
    q = np.round(img * levels) / levels
    if out_dtype == np.uint8:
        return (q * 255).astype(np.uint8)
    return (q * 65535).astype(np.uint16)


# ---------------------------------------------------------------- deep sky ---

@dataclass
class StarFieldScene:
    """A rotating star field as seen through an alt-az mounted telescope."""
    size: tuple[int, int] = (1080, 1920)
    n_stars: int = 400
    seed: int = 11
    fwhm: float = 3.0
    rotation_deg_per_s: float = 0.004       # field rotation rate
    drift_px_per_s: tuple[float, float] = (0.05, -0.03)
    galaxy: bool = True
    hot_pixels: int = 60
    stars_xy: np.ndarray | None = None      # optional fixed star positions (sky pixels)
    stars_flux: np.ndarray | None = None

    def __post_init__(self):
        rng = np.random.default_rng(self.seed)
        H, W = self.size
        diag = math.hypot(H, W)
        if self.stars_xy is None:
            # Stars spread over a circle bigger than the frame, so rotation brings new ones in.
            r = diag / 2 * np.sqrt(rng.uniform(0, 1, self.n_stars))
            a = rng.uniform(0, 2 * math.pi, self.n_stars)
            self.stars_xy = np.column_stack([r * np.cos(a), r * np.sin(a)])
            self.stars_flux = (rng.pareto(1.3, self.n_stars) + 0.05) * 0.05
        self.hot = np.column_stack([rng.integers(0, W, self.hot_pixels), rng.integers(0, H, self.hot_pixels)])
        self.hot_val = rng.uniform(0.3, 1.0, self.hot_pixels)
        self.rng = rng

    def render(self, t: float = 0.0, exposure_s: float = 10.0, clouds: float = 0.0, trail: bool = False,
               seeing_scale: float = 1.0) -> np.ndarray:
        """Float mono/RGB-agnostic sky (H, W, 3) before sensor effects."""
        H, W = self.size
        ang = math.radians(self.rotation_deg_per_s * t)
        c, s = math.cos(ang), math.sin(ang)
        dx, dy = self.drift_px_per_s[0] * t, self.drift_px_per_s[1] * t
        xy = self.stars_xy @ np.array([[c, s], [-s, c]]) + np.array([W / 2 + dx, H / 2 + dy])
        img = np.zeros((H, W, 3), np.float32)
        sig = self.fwhm * seeing_scale / 2.3548
        rad = int(math.ceil(sig * 5))
        k = np.arange(-rad, rad + 1, dtype=np.float32)
        flux = self.stars_flux * (exposure_s / 10.0) * (1 - clouds)
        colors = np.random.default_rng(self.seed + 1).uniform(0.75, 1.0, (len(flux), 3)).astype(np.float32)
        for (x, y), f, col in zip(xy, flux, colors):
            if not (-rad <= x < W + rad and -rad <= y < H + rad):
                continue
            x0, y0 = int(math.floor(x)), int(math.floor(y))
            gx = np.exp(-((k + x0 - x) ** 2) / (2 * sig * sig))
            gy = np.exp(-((k + y0 - y) ** 2) / (2 * sig * sig))
            stamp = np.outer(gy, gx) * (f / (2 * math.pi * sig * sig))
            ys0, xs0 = y0 - rad, x0 - rad
            sy0, sx0 = max(0, -ys0), max(0, -xs0)
            ey, ex = min(stamp.shape[0], H - ys0), min(stamp.shape[1], W - xs0)
            if ey <= sy0 or ex <= sx0:
                continue
            img[ys0 + sy0: ys0 + ey, xs0 + sx0: xs0 + ex] += stamp[sy0:ey, sx0:ex, None] * col
        if getattr(self, "_grid", None) is None or self._grid[0].shape != (H, W):
            self._grid = np.mgrid[0:H, 0:W].astype(np.float32)
        yy, xx = self._grid
        if self.galaxy:
            gxc, gyc = W / 2 + dx, H / 2 + dy
            rx = (xx - gxc) * math.cos(ang + 0.6) + (yy - gyc) * math.sin(ang + 0.6)
            ry = -(xx - gxc) * math.sin(ang + 0.6) + (yy - gyc) * math.cos(ang + 0.6)
            rr = np.sqrt((rx / (W * 0.12)) ** 2 + (ry / (W * 0.045)) ** 2)
            gal = 0.06 * np.exp(-rr * 3.0) * (exposure_s / 10.0) * (1 - clouds)
            img += gal[..., None] * np.array([1.0, 0.92, 0.8], np.float32)
        # Sky glow with a gradient (light pollution)
        grad = 0.04 + 0.02 * (xx / W) + 0.01 * (yy / H)
        img += (grad * (exposure_s / 10.0) + 0.15 * clouds)[..., None] * np.array([0.9, 1.0, 1.1], np.float32)
        if trail:
            p0 = (int(self.rng.uniform(0, W * 0.3)), int(self.rng.uniform(0, H)))
            p1 = (int(self.rng.uniform(W * 0.7, W)), int(self.rng.uniform(0, H)))
            tr = np.zeros((H, W), np.float32)
            cv2.line(tr, p0, p1, 0.25, 2, cv2.LINE_AA)
            img += tr[..., None]
        # Vignetting and dust are applied by optics(): lights and flats share the same light path.
        return img

    def add_hot_pixels(self, raw: np.ndarray) -> np.ndarray:
        H, W = raw.shape[:2]
        mx = 255 if raw.dtype == np.uint8 else 65535
        for (x, y), v in zip(self.hot, self.hot_val):
            raw[y % H, x % W] = max(int(raw[y % H, x % W]), int(v * mx))
        return raw

    def optics(self) -> np.ndarray:
        """Throughput of the telescope + camera (vignetting and a dust donut), max 1."""
        if getattr(self, "_optics", None) is None:
            f = self.flat_field()
            self._optics = (f / f.max()).astype(np.float32)
        return self._optics

    def flat_field(self) -> np.ndarray:
        H, W = self.size
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        r2 = ((xx - W / 2) ** 2 + (yy - H / 2) ** 2) / ((W / 2) ** 2 + (H / 2) ** 2)
        img = (1 - 0.35 * r2) * 0.5
        # a dust donut
        d = np.hypot(xx - W * 0.3, yy - H * 0.6)
        img *= 1 - 0.12 * np.exp(-((d - 25) / 6) ** 2)
        return np.repeat(img[..., None], 3, axis=2)


def jupiter_ser(path, n_frames: int = 300, size=(240, 320), radius: float = 70, seeing: float = 1.0,
                fps: float = 60.0, bayer: str | None = "RGGB", seed: int = 1, bit_depth: int = 12,
                target: str = "jupiter", gain: float = 1.0) -> dict:
    """Write a synthetic planetary SER video to ``path`` (used by tests and demos)."""
    import datetime as dt
    from ..io.ser import SerWriter
    scene = PlanetScene(target, size, radius, seed=seed)
    turb = Turbulence(size, seeing, seed)
    rng = np.random.default_rng(seed)
    t0 = dt.datetime(2026, 10, 20, 23, 0, 0, tzinfo=dt.timezone.utc)
    blurs = []
    truth = scene.render(0.0)
    with SerWriter(path, size[1], size[0], bit_depth, bayer or "MONO", instrument="AstroFlow Simulator",
                   telescope="Sim 300P", start_time=t0) as w:
        for i in range(n_frames):
            t = i / fps
            if target in ROTATION_PERIOD_S and i % max(1, int(fps)) == 0:
                truth = scene.render(t)
            frame, blur = turb.apply(truth)
            blurs.append(blur)
            raw = sensor_readout(frame, bit_depth, gain, bayer, rng=rng)
            w.write(raw, t0 + dt.timedelta(seconds=t))
    return {"truth": scene.render(0.0), "blur": np.array(blurs), "scene": scene}


def write_dso_session(folder, n_lights: int = 12, size=(540, 720), exposure: float = 10.0, seed: int = 3,
                      bayer: str = "RGGB", n_darks: int = 8, n_flats: int = 8, n_bias: int = 8,
                      trail_frame: int | None = 4, cloud_frame: int | None = None, interval_s: float = 30.0,
                      rotation_deg_per_s: float = 0.004, target: str = "Sim Galaxy") -> dict:
    """Write a complete simulated deep-sky session as FITS files (lights, darks, flats, bias)."""
    import datetime as dt
    from pathlib import Path
    from ..io.images import write_fits
    folder = Path(folder)
    rng = np.random.default_rng(seed)
    scene = StarFieldScene(size, n_stars=350, seed=seed, rotation_deg_per_s=rotation_deg_per_s)
    H, W = size
    dark_current = rng.gamma(1.0, 0.002, (H, W)).astype(np.float32)   # per 10 s, fixed pattern
    bias_level = 0.02
    t0 = dt.datetime(2026, 10, 20, 22, 0, 0, tzinfo=dt.timezone.utc)
    base = {"INSTRUME": "AstroFlow Simulator", "TELESCOP": "Sim 300P", "GAIN": 120, "OFFSET": 20,
            "XBINNING": 1, "CCD-TEMP": 12.0, "BAYERPAT": bayer, "FOCALLEN": 1500.0, "XPIXSZ": 2.9}

    def readout(rgb, exp, add_hot=True):
        img = rgb + 0.0
        raw = sensor_readout(img, 12, 1.0, bayer, read_noise=0.002, rng=rng, photon_scale=20000.0).astype(np.float32) / 65535
        raw = raw + bias_level + dark_current * (exp / 10.0)
        if add_hot:
            raw = scene.add_hot_pixels((np.clip(raw, 0, 1) * 65535).astype(np.uint16)).astype(np.float32) / 65535
        return (np.clip(raw, 0, 1) * 65535).astype(np.uint16)

    paths: dict[str, list] = {"lights": [], "darks": [], "flats": [], "bias": []}
    for i in range(n_lights):
        t = i * interval_s
        sky = scene.render(t, exposure, clouds=0.7 if i == cloud_frame else 0.0, trail=(i == trail_frame))
        raw = readout(sky * scene.optics(), exposure)
        p = folder / "lights" / f"light_{i:03d}.fits"
        write_fits(p, raw, {**base, "IMAGETYP": "Light", "EXPTIME": exposure, "OBJECT": target,
                            "DATE-OBS": (t0 + dt.timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S")}, bits=16)
        paths["lights"].append(p)
    zero = np.zeros((H, W, 3), np.float32)
    for i in range(n_darks):
        p = folder / "darks" / f"dark_{i:03d}.fits"
        write_fits(p, readout(zero, exposure), {**base, "IMAGETYP": "Dark", "EXPTIME": exposure}, bits=16)
        paths["darks"].append(p)
    flat_sky = scene.optics() * 0.5   # an evenly lit panel seen through the same optics
    for i in range(n_flats):
        p = folder / "flats" / f"flat_{i:03d}.fits"
        write_fits(p, readout(flat_sky, 0.01, add_hot=False), {**base, "IMAGETYP": "Flat", "EXPTIME": 0.01}, bits=16)
        paths["flats"].append(p)
    for i in range(n_bias):
        p = folder / "bias" / f"bias_{i:03d}.fits"
        write_fits(p, readout(zero, 0.0, add_hot=False), {**base, "IMAGETYP": "Bias", "EXPTIME": 0.0}, bits=16)
        paths["bias"].append(p)
    return {"paths": paths, "scene": scene}


def render_sky(ra: float, dec: float, scale_arcsec: float, rotation_deg: float, width: int, height: int,
               fwhm: float = 2.5, flipped: bool = False, sky: float = 0.03, noise: float = 0.002,
               seed: int = 0, color: bool = False, mag9_peak: float = 0.04) -> tuple[np.ndarray, dict]:
    """Render the real sky (bundled star catalogue, to mag 9) as the camera would see it.

    Returns (image, WCS header). Used to test plate solving and by the simulated camera.
    """
    from ..astro.catalog import star_table
    from ..astro.coords import angular_separation
    from ..solve.wcs import Solution, tan_header
    hdr = tan_header(ra, dec, scale_arcsec, rotation_deg, width, height, flipped)
    sol = Solution.from_header(hdr, width, height)
    st = star_table()
    radius = 0.75 * math.hypot(width, height) * scale_arcsec / 3600
    sel = np.nonzero(angular_separation(ra, dec, st["ra"], st["dec"]) < radius)[0]
    x, y = sol.world2pix(st["ra"][sel], st["dec"][sel])
    img = np.full((height, width, 3) if color else (height, width), sky, np.float32)
    sig = fwhm / 2.3548
    rad = int(math.ceil(sig * 5))
    k = np.arange(-rad, rad + 1, dtype=np.float32)
    # A magnitude-9 star peaks at ``mag9_peak``; brighter stars saturate as in real frames.
    flux_all = 10 ** (-0.4 * (st["mag"][sel] - 9.0)) * mag9_peak * 2 * math.pi * (fwhm / 2.3548) ** 2
    bv = st["bv"][sel]
    for xi, yi, f, b in zip(x, y, flux_all, bv):
        if not (-rad <= xi < width + rad and -rad <= yi < height + rad):
            continue
        x0, y0 = int(math.floor(xi)), int(math.floor(yi))
        gx = np.exp(-((k + x0 - xi) ** 2) / (2 * sig * sig))
        gy = np.exp(-((k + y0 - yi) ** 2) / (2 * sig * sig))
        stamp = np.outer(gy, gx) * (f / (2 * math.pi * sig * sig))
        ys0, xs0 = y0 - rad, x0 - rad
        sy0, sx0 = max(0, -ys0), max(0, -xs0)
        ey, ex = min(stamp.shape[0], height - ys0), min(stamp.shape[1], width - xs0)
        if ey <= sy0 or ex <= sx0:
            continue
        patch = stamp[sy0:ey, sx0:ex]
        if color:
            t = float(np.clip(b, -0.3, 2.0))
            col = np.array([0.8 + 0.25 * t, 0.95, 1.15 - 0.3 * t], np.float32)
            img[ys0 + sy0: ys0 + ey, xs0 + sx0: xs0 + ex] += patch[..., None] * col
        else:
            img[ys0 + sy0: ys0 + ey, xs0 + sx0: xs0 + ex] += patch
    if noise > 0:
        rng = np.random.default_rng(seed)
        img = img + rng.normal(0, noise, img.shape).astype(np.float32)
    return np.clip(img, 0, 1), hdr
