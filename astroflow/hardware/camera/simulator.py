"""Simulated camera behaving like a Player One Uranus-C (3856 x 2180, 2.9 um, 12-bit, RGGB).

Scenes: "jupiter", "saturn", "mars", "moon" (lucky-imaging video with live
turbulence), "deepsky" (real sky rendered where the simulated mount points,
with alt-az field rotation; otherwise a synthetic rotating star field) and
"star" (one star for focusing/collimation: use the sim focuser and the
collimation-error setting).
"""
from __future__ import annotations

import datetime as dt
import math
import threading
import time

import cv2
import numpy as np

from ...sim.scenes import PlanetScene, StarFieldScene, Turbulence, render_sky, sensor_readout
from .base import CONTROL_HELP, Camera, CameraDisconnected, CameraError, CameraInfo, Control

SCENES = ["jupiter", "saturn", "mars", "moon", "deepsky", "star"]


class SimCamera(Camera):
    def __init__(self, scene: str = "jupiter", mount_link=None, lat: float = 40.0, lon: float = -75.0,
                 sky_scale_arcsec: float = 0.0):
        self.info = CameraInfo(id="sim", name="Simulated Uranus-C", sensor="IMX585 (simulated)", width=3856, height=2180,
                               pixel_um=2.9, bit_depth=12, color=True, bayer="RGGB", has_cooler=False, usb3=True,
                               bins=[1, 2, 3, 4], driver="AstroFlow simulator")
        self.scene_name = scene
        self.mount_link = mount_link          # callable -> (true_az, true_alt) or None
        self.lat, self.lon = lat, lon
        self.sky_scale = sky_scale_arcsec     # 0 = wide simulated field (solvable with the built-in solver)
        self._ctl = {"exposure_ms": 8.0, "gain": 300.0, "offset": 20.0, "usb_bandwidth": 80.0, "frame_limit": 0.0,
                     "sim_focus": 0.0, "sim_seeing": 1.0, "sim_collimation": 0.15}
        self._roi = {"x": 0, "y": 0, "w": 3856, "h": 2180, "bin": 1, "out_w": 3856, "out_h": 2180}
        self._bits = 16
        self._connected = False
        self._streaming = False
        self._abort = threading.Event()
        self._last_frame_t = 0.0
        self.unplugged = False              # tests: simulate pulling the USB cable
        self._planet: PlanetScene | None = None
        self._planet_key = None
        self._turb: Turbulence | None = None
        self._stars: StarFieldScene | None = None
        self._t0 = time.time()
        self._rng = np.random.default_rng()
        self._dropped = 0

    # -- lifecycle --------------------------------------------------------
    def connect(self) -> None:
        self._connected = True
        if self.scene_name in ("jupiter", "saturn", "mars", "moon"):
            self.set_roi(3856 // 2 - 320, 2180 // 2 - 240, 640, 480, 1)

    def disconnect(self) -> None:
        self._connected = False
        self._streaming = False

    @property
    def connected(self) -> bool:
        return self._connected

    def set_scene(self, scene: str) -> None:
        if scene not in SCENES:
            raise CameraError(f"Unknown scene {scene}")
        self.scene_name = scene
        self._planet = None
        self._stars = None

    # -- controls -----------------------------------------------------------
    def controls(self) -> dict[str, Control]:
        c = self._ctl
        out = {
            "exposure_ms": Control("exposure_ms", "Exposure (ms)", c["exposure_ms"], 0.032, 2_000_000, 0.01, help=CONTROL_HELP["exposure_ms"]),
            "gain": Control("gain", "Gain", c["gain"], 0, 600, 1, help=CONTROL_HELP["gain"]),
            "offset": Control("offset", "Offset", c["offset"], 0, 100, 1, help=CONTROL_HELP["offset"]),
            "usb_bandwidth": Control("usb_bandwidth", "USB bandwidth (%)", c["usb_bandwidth"], 35, 100, 1, help=CONTROL_HELP["usb_bandwidth"]),
            "frame_limit": Control("frame_limit", "Frame rate limit (fps)", c["frame_limit"], 0, 2000, 1, help=CONTROL_HELP["frame_limit"]),
            "sim_focus": Control("sim_focus", "Simulator: focuser position", c["sim_focus"], -50, 50, 0.5,
                                 help="Simulator only: move the virtual focuser. 0 = perfect focus."),
            "sim_seeing": Control("sim_seeing", "Simulator: seeing (turbulence)", c["sim_seeing"], 0.2, 4, 0.1,
                                  help="Simulator only: how unsteady the virtual air is."),
            "sim_collimation": Control("sim_collimation", "Simulator: collimation error", c["sim_collimation"], 0, 0.6, 0.01,
                                       help="Simulator only: offset of the secondary shadow in a defocused star."),
        }
        return out

    def set_control(self, name: str, value: float, auto: bool = False) -> None:
        if name not in self._ctl:
            raise CameraError(f"Unknown setting {name}")
        self._ctl[name] = float(value)
        if name == "sim_seeing" and self._turb:
            self._turb.seeing = float(value)

    def set_roi(self, x: int, y: int, w: int, h: int, binning: int = 1) -> dict:
        b = binning if binning in self.info.bins else 1
        W, H = self.info.width, self.info.height
        w = max(64, min(int(w), W)) // (4 * b) * (4 * b)
        h = max(64, min(int(h), H)) // (2 * b) * (2 * b)
        x = min(max(0, int(x)), W - w)
        y = min(max(0, int(y)), H - h)
        self._roi = {"x": x, "y": y, "w": w, "h": h, "bin": b, "out_w": w // b, "out_h": h // b}
        self._planet = None
        self._stars = None
        return dict(self._roi)

    def get_roi(self) -> dict:
        return dict(self._roi)

    def set_bit_depth(self, bits: int) -> None:
        self._bits = 16 if bits > 8 else 8

    @property
    def bit_depth_out(self) -> int:
        return self._bits

    # -- capture --------------------------------------------------------------
    def start_stream(self) -> None:
        self._check()
        self._streaming = True

    def stop_stream(self) -> None:
        self._streaming = False

    def _check(self) -> None:
        if self.unplugged:
            self._connected = False
            raise CameraDisconnected("Simulated camera unplugged")
        if not self._connected:
            raise CameraError("Camera not connected")

    def get_frame(self, timeout_s: float = 2.0) -> np.ndarray | None:
        self._check()
        exp = self._ctl["exposure_ms"] / 1000.0
        px = self._roi["out_w"] * self._roi["out_h"]
        # USB 3 at ~350 MB/s * bandwidth share; 2 bytes per pixel in 16-bit mode.
        bw_fps = 350e6 * self._ctl["usb_bandwidth"] / 100 / (px * (2 if self._bits == 16 else 1))
        fps = min(1.0 / max(exp, 1e-4), bw_fps)
        if self._ctl["frame_limit"] > 0:
            fps = min(fps, self._ctl["frame_limit"])
        wait = self._last_frame_t + 1.0 / fps - time.time()
        if wait > timeout_s:
            time.sleep(timeout_s)
            return None
        if wait > 0:
            time.sleep(wait)
        self._last_frame_t = time.time()
        return self._render(exp)

    def expose(self, seconds: float) -> np.ndarray:
        self._check()
        self._abort.clear()
        end = time.time() + seconds
        while time.time() < end:
            if self._abort.wait(min(0.1, max(0.0, end - time.time()))):
                raise CameraError("Exposure aborted")
        self._check()
        return self._render(seconds)

    def abort(self) -> None:
        self._abort.set()

    def temperature(self) -> float | None:
        return 18.0 + 0.002 * (time.time() - self._t0) % 3

    def dropped_frames(self) -> int:
        return self._dropped

    # -- rendering ------------------------------------------------------------
    def _brightness(self, exp_s: float) -> float:
        # Signal grows with exposure and gain (gain 0..600 ~ 0..60 dB scaled down).
        return exp_s / 0.008 * 10 ** (self._ctl["gain"] / 600.0) / 10 ** 0.5

    def _render(self, exp_s: float) -> np.ndarray:
        r = self._roi
        H, W = r["out_h"], r["out_w"]
        s = self.scene_name
        if s in ("jupiter", "saturn", "mars", "moon"):
            img = self._render_planet(H, W, exp_s)
        elif s == "star":
            img = self._render_star(H, W, exp_s)
        else:
            img = self._render_deepsky(H, W, exp_s)
        bayer = self.info.bayer if self.info.color else None
        if r["bin"] > 1:
            bayer = None  # binned colour data loses the mosaic in this simulation
        raw = sensor_readout(img, 12, 1.0, bayer, read_noise=0.002 + 0.001 * self._ctl["gain"] / 300, rng=self._rng,
                             photon_scale=4000.0 / max(10 ** (self._ctl["gain"] / 600.0), 1))
        if self._ctl["offset"]:
            raw = np.clip(raw.astype(np.int32) + int(self._ctl["offset"] * 16), 0, 65535).astype(np.uint16)
        if self._bits == 8:
            raw = (raw >> 8).astype(np.uint8)
        return raw

    def _render_planet(self, H: int, W: int, exp_s: float) -> np.ndarray:
        key = (self.scene_name, H, W)
        if self._planet is None or self._planet_key != key:
            radius = {"jupiter": 0.3, "saturn": 0.16, "mars": 0.12, "moon": 0}[self.scene_name] * min(H, W)
            self._planet = PlanetScene(self.scene_name, (H, W), radius)
            self._planet_key = key
            self._turb = Turbulence((H, W), self._ctl["sim_seeing"], int(time.time()))
            self._truth_t = None
        t = time.time() - self._t0
        if self._truth_t is None or abs(t - self._truth_t) > 2.0:
            self._truth = self._planet.render(t)
            self._truth_t = t
        frame, _ = self._turb.apply(self._truth)
        defocus = abs(self._ctl["sim_focus"]) * 0.15
        if defocus > 0.05:
            frame = cv2.GaussianBlur(frame, (0, 0), defocus)
        return frame * self._brightness(exp_s)

    def _render_star(self, H: int, W: int, exp_s: float) -> np.ndarray:
        img = np.zeros((H, W, 3), np.float32)
        f = self._ctl["sim_focus"]
        cy, cx = H / 2 + 3 * math.sin(time.time() * 1.3), W / 2 + 3 * math.cos(time.time() * 0.9)
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        r_out = abs(f) * 2.0 + 1.5
        if abs(f) < 1.0:
            sig = 1.3 + abs(f)
            g = np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sig * sig))
            img += (g * 0.9)[..., None]
        else:
            col = self._ctl["sim_collimation"]
            d_out = np.hypot(xx - cx, yy - cy)
            d_in = np.hypot(xx - (cx + col * r_out), yy - (cy - col * r_out * 0.5))
            donut = ((d_out < r_out) & (d_in > 0.33 * r_out)).astype(np.float32)
            donut = cv2.GaussianBlur(donut, (0, 0), 1.2)
            img += (donut * 0.6 * 25 / max(r_out, 5) ** 0.9)[..., None]
        img = cv2.GaussianBlur(img, (0, 0), 0.6 * self._ctl["sim_seeing"])
        return img * self._brightness(exp_s) * 0.2

    def _render_deepsky(self, H: int, W: int, exp_s: float) -> np.ndarray:
        if self.mount_link is not None:
            pos = self.mount_link()
            if pos is not None:
                from ...astro.coords import altaz_to_radec, parallactic_angle
                az, alt = pos
                now = dt.datetime.now(dt.timezone.utc)
                ra, dec = altaz_to_radec(alt, az, now, self.lat, self.lon)
                # Alt-az: the field turns with the parallactic angle.
                rot = parallactic_angle(float(ra), float(dec), now, self.lat, self.lon)
                scale = self.sky_scale or 10.5 * self._roi["bin"] * 3856 / max(W * self._roi["bin"], 1)
                gainf = 10 ** (self._ctl["gain"] / 600.0) / 10 ** 0.5
                img, _ = render_sky(float(ra), float(dec), scale, rot, W, H, fwhm=2.8, color=True, noise=0.0,
                                    sky=0.02, mag9_peak=min(0.3 * exp_s * gainf, 2.0))  # 12-inch aperture: mag 9 is bright
                return img
        if self._stars is None or self._stars.size != (H, W):
            self._stars = StarFieldScene((H, W), n_stars=max(150, int(H * W / 4000)), rotation_deg_per_s=0.004)
        # The simulated sky is bright enough that short alt-az subs (0.5-10 s) show plenty of stars.
        eff = exp_s * 20 * 10 ** (self._ctl["gain"] / 600.0) / 10 ** 0.5
        img = self._stars.render(time.time() - self._t0, min(eff, 60.0))
        return img * self._stars.optics()
