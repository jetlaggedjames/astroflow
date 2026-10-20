"""The capture engine behind the Capture tab.

* One background thread talks to the camera (live view, recording, exposures).
* Recording goes through a separate writer thread and a memory buffer, so a
  slow disk never makes the camera drop frames; if the buffer fills, the
  number of dropped frames is shown.
* Files are written incrementally and closed properly on any problem
  (camera unplugged, disk nearly full, app closing) - nothing recorded is lost.
* Free disk space is checked continuously: warning below 5 GB, recording is
  stopped (and the file finalised) below 1 GB.
"""
from __future__ import annotations

import collections
import datetime as dt
import io
import json
import logging
import math
import queue
import re
import shutil
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .. import SOFTWARE_ID
from ..config import atomic_write_text, calibration_dir, settings
from ..imaging.livestack import LiveStacker
from ..imaging.stars import detect_stars, measure_summary
from ..io.debayer import bayer_luminance, debayer, superpixel
from ..io.images import write_fits, write_standard, write_tiff
from ..io.ser import SerWriter
from .camera.base import Camera, CameraDisconnected, CameraError

log = logging.getLogger("astroflow.capture")

WARN_FREE = 5 * 1024**3
STOP_FREE = 1 * 1024**3


def night_date(t: dt.datetime | None = None) -> str:
    """Observing-night date: times before noon belong to the previous evening."""
    t = (t or dt.datetime.now()).astimezone()
    if t.hour < 12:
        t -= dt.timedelta(days=1)
    return t.strftime("%Y-%m-%d")


def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 _\-+.]", "_", s or "").strip()
    return re.sub(r"\s+", "_", s)[:60] or "Untitled"


# ------------------------------------------------------------- recorders ---

class SerRecorder:
    kind = "ser"

    def __init__(self, path: Path, width: int, height: int, bits: int, bayer: str | None, meta: dict):
        self.path = path
        self.meta = meta
        self.w = SerWriter(path, width, height, bits, bayer or "MONO", observer=meta.get("OBSERVER", ""),
                           instrument=meta.get("INSTRUME", ""), telescope=meta.get("TELESCOP", ""))
        self._write_sidecar()

    def _write_sidecar(self) -> None:
        atomic_write_text(Path(str(self.path) + ".json"), json.dumps({**self.meta, "FRAMES": self.w.frames}, indent=1, default=str))

    def write(self, frame: np.ndarray, t: dt.datetime) -> None:
        self.w.write(frame, t)

    @property
    def frames(self) -> int:
        return self.w.frames

    @property
    def bytes(self) -> int:
        return self.w.bytes_written

    def close(self) -> None:
        self.w.close()
        self.meta["DATE-END"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self._write_sidecar()


class FitsSequenceRecorder:
    kind = "fits"

    def __init__(self, folder: Path, prefix: str, meta: dict):
        self.folder = folder
        self.prefix = prefix
        self.meta = meta
        self.folder.mkdir(parents=True, exist_ok=True)
        self.frames = 0
        self.bytes = 0
        self.files: list[str] = []

    def write(self, frame: np.ndarray, t: dt.datetime, extra: dict | None = None) -> Path:
        self.frames += 1
        p = self.folder / f"{self.prefix}_{self.frames:04d}.fits"
        hdr = {**self.meta, **(extra or {}), "DATE-OBS": t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]}
        write_fits(p, frame, hdr, bits=16 if frame.dtype == np.uint16 else 32)
        self.bytes += p.stat().st_size
        self.files.append(str(p))
        return p

    def close(self) -> None:
        pass


# ------------------------------------------------------------ manager ---

class CaptureManager:
    def __init__(self):
        self.camera: Camera | None = None
        self.camera_kind = ""
        self.lock = threading.RLock()
        self.state = "disconnected"        # disconnected | idle | live | recording | exposing
        self.mode = "stream"              # stream (video) | exposures (long single frames)
        self.exposure_s = 2.0              # for exposure mode
        self.latest: np.ndarray | None = None
        self.latest_t: dt.datetime | None = None
        self.seq = 0
        self.fps = 0.0
        self._fps_times: collections.deque = collections.deque(maxlen=60)
        self.message = ""
        self.warnings: list[str] = []
        self.recorder = None
        self.rec_info: dict = {}
        self._queue: queue.Queue | None = None
        self._writer: threading.Thread | None = None
        self._acq: threading.Thread | None = None
        self._run = threading.Event()
        self.dropped_by_app = 0
        self.focus_history: collections.deque = collections.deque(maxlen=300)
        self.focus_box: tuple[float, float, float, float] | None = None
        self.stats: dict = {}
        self.livestack: LiveStacker | None = None
        self.livestack_info: dict = {}
        self.target = ""
        self.calib: dict | None = None
        self._preview_cache: tuple[int, tuple, bytes] | None = None
        self._last_disk_check = 0.0
        self.disk_free = None
        self.reconnect_wanted = False
        self.on_frame_hooks: list = []

    # ------------------------------------------------------------- connect
    def connect(self, kind: str | None = None, **kw) -> dict:
        s = settings()
        kind = kind or s.get("camera.driver", "simulator")
        self.disconnect()
        if kind == "simulator":
            from .camera.simulator import SimCamera
            from .mount.controller import controller
            # Looked up on every frame, so the simulated sky follows the simulated mount even if it connects later.
            def link():
                srv = controller.sim_server
                return srv.true_altaz() if srv is not None else None
            lat = s.get("location.lat") or 40.0
            lon = s.get("location.lon") or -75.0
            cam = SimCamera(kw.get("scene", s.get("camera.sim_scene", "jupiter")), link, float(lat), float(lon))
        elif kind == "playerone":
            from .camera.playerone import PlayerOneCamera
            cam = PlayerOneCamera(kw.get("camera_id"))
        elif kind == "ascom":
            from .camera.ascom_camera import AscomCamera
            cam = AscomCamera(s.get("camera.ascom_progid", ""))
        else:
            raise CameraError(f"Unknown camera type {kind}")
        cam.connect()
        with self.lock:
            self.camera, self.camera_kind = cam, kind
            self.state = "idle"
            self.message = f"Connected: {cam.info.name}"
        self.start_live()
        return self.status()

    def disconnect(self) -> None:
        self.stop_recording()
        self.stop_live()
        with self.lock:
            if self.camera:
                try:
                    self.camera.disconnect()
                except Exception:
                    pass
            self.camera = None
            self.state = "disconnected"
            self.reconnect_wanted = False

    # ------------------------------------------------------------- live view
    def start_live(self) -> None:
        if not self.camera:
            raise CameraError("No camera connected")
        if self._acq and self._acq.is_alive():
            return
        self._run.set()
        self._acq = threading.Thread(target=self._acquire, name="capture", daemon=True)
        self._acq.start()

    def stop_live(self) -> None:
        self._run.clear()
        if self.camera:
            try:
                self.camera.abort()
            except Exception:
                pass
        if self._acq and self._acq.is_alive() and threading.current_thread() is not self._acq:
            self._acq.join(timeout=10)
        self._acq = None
        if self.camera:
            try:
                self.camera.stop_stream()
            except Exception:
                pass
        if self.state not in ("disconnected",):
            self.state = "idle"

    def set_mode(self, mode: str, exposure_s: float | None = None) -> None:
        if self.recorder:
            raise CameraError("Stop recording before changing the capture mode")
        restart = self._acq is not None and self._acq.is_alive()
        if restart:
            self.stop_live()
        self.mode = "exposures" if mode == "exposures" else "stream"
        if exposure_s:
            self.exposure_s = float(exposure_s)
        if restart and self.camera:
            self.start_live()

    def _acquire(self) -> None:
        cam = self.camera
        streaming = False
        try:
            while self._run.is_set() and cam is self.camera and cam is not None:
                if self.mode == "stream":
                    if not streaming:
                        cam.start_stream()
                        streaming = True
                        self.state = "recording" if self.recorder else "live"
                    frame = cam.get_frame(timeout_s=1.0)
                    if frame is None:
                        continue
                else:
                    if streaming:
                        cam.stop_stream()
                        streaming = False
                    self.state = "exposing"
                    frame = cam.expose(self.exposure_s)
                self._on_frame(frame)
        except CameraDisconnected as e:
            self._camera_lost(str(e))
        except CameraError as e:
            if self._run.is_set():
                self.message = f"Camera error: {e}"
                log.error("capture: %s", e)
        except Exception as e:
            log.exception("capture loop crashed: %s", e)
            self.message = f"Capture error: {e}"
            self.stop_recording()
        finally:
            if streaming:
                try:
                    cam.stop_stream()
                except Exception:
                    pass

    def _camera_lost(self, why: str) -> None:
        log.error("Camera disconnected: %s", why)
        saved = self.stop_recording()
        self.state = "disconnected"
        self.reconnect_wanted = True
        msg = "The camera stopped responding (cable unplugged?)."
        if saved:
            msg += f" The recording was closed safely: {saved.get('frames', 0)} frames kept in {Path(saved['path']).name}."
        self.message = msg
        self.warnings.append(msg)
        threading.Thread(target=self._reconnect_loop, name="camera-reconnect", daemon=True).start()

    def _reconnect_loop(self) -> None:
        kind = self.camera_kind
        while self.reconnect_wanted:
            time.sleep(3)
            try:
                if self.camera is not None and hasattr(self.camera, "unplugged") and self.camera.unplugged:
                    continue
                self.camera.disconnect() if self.camera else None
                self.connect(kind)
                self.message = "Camera reconnected. Recording was NOT restarted automatically - press Record when ready."
                return
            except Exception:
                continue

    # ----------------------------------------------------------- per frame
    def _on_frame(self, frame: np.ndarray) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        with self.lock:
            self.latest = frame
            self.latest_t = now
            self.seq += 1
            self._fps_times.append(time.monotonic())
            if len(self._fps_times) > 2:
                span = self._fps_times[-1] - self._fps_times[0]
                self.fps = (len(self._fps_times) - 1) / span if span > 0 else 0.0
        if self.recorder is not None:
            if self.mode == "stream":
                try:
                    self._queue.put_nowait((frame, now))
                except queue.Full:
                    self.dropped_by_app += 1
            else:
                self._write_exposure(frame, now)
            self._check_limits()
        if self.livestack is not None and self.mode == "exposures":
            try:
                self.livestack_info = self.livestack.add(frame, self.exposure_s)
            except Exception as e:
                self.livestack_info = {"stacked": False, "message": str(e)}
        if self.calib is not None:
            self._calib_frame(frame, now)
        # Cheap statistics a few times per second (not on every frame at 300 fps).
        if self.mode == "exposures" or self.seq % max(1, int(self.fps // 4) or 1) == 0:
            self._update_stats(frame)
        if time.time() - self._last_disk_check > 2:
            self._check_disk()
        for hook in list(self.on_frame_hooks):
            try:
                hook(frame, now)
            except Exception:
                pass

    def _update_stats(self, frame: np.ndarray) -> None:
        cam = self.camera
        if cam is None:
            return
        maxv = 255 if frame.dtype == np.uint8 else 65535
        step = max(1, int(math.sqrt(frame.size / 200_000)))
        sub = frame[::step * 2, ::step * 2] if frame.ndim == 2 else frame[::step, ::step]
        bayer = cam.info.bayer if frame.ndim == 2 and cam.get_roi().get("bin", 1) == 1 else None
        hist = {}
        if bayer:
            planes = {}
            for i, ch in enumerate(bayer):
                planes.setdefault(ch, []).append(frame[i // 2::2 * step, i % 2::2 * step].ravel())
            for ch in "RGB":
                v = np.concatenate(planes[ch])
                hist[ch] = np.histogram(v, bins=256, range=(0, maxv + 1))[0].tolist()
        else:
            hist["L"] = np.histogram(sub.ravel(), bins=256, range=(0, maxv + 1))[0].tolist()
        clipped = float(np.count_nonzero(sub >= maxv * 0.98)) / sub.size
        focus = self._focus_metric(frame, bayer)
        self.focus_history.append({"t": time.time(), **focus})
        self.stats = {"hist": hist, "max": int(sub.max()), "mean": float(sub.mean()), "full_scale": maxv,
                      "clipped_percent": round(clipped * 100, 3), "saturated": clipped > 0.0005, "focus": focus}

    def _focus_metric(self, frame: np.ndarray, bayer: str | None) -> dict:
        img = bayer_luminance(frame) if bayer else frame.astype(np.float32)
        if img.ndim == 3:
            img = img.mean(axis=2)
        scale = 2 if bayer else 1
        H, W = img.shape
        if self.focus_box:
            x, y, w, h = self.focus_box
            img = img[int(y * H): int((y + h) * H), int(x * W): int((x + w) * W)]
        if img.size < 64:
            return {"value": None}
        img = img / max(float(img.max()), 1e-6)
        st = detect_stars(img, sigma=6.0, max_stars=40)
        s = measure_summary(st)
        if s["stars"] >= 1 and s["fwhm"] and img.mean() < 0.3:
            return {"kind": "fwhm", "value": round(float(s["fwhm"]) * scale, 2), "stars": s["stars"],
                    "label": "Star size FWHM (px) - smaller is better"}
        sm = cv2.GaussianBlur(img.astype(np.float32), (0, 0), 1.0)
        lap = cv2.Laplacian(sm, cv2.CV_32F)
        mask = sm > 0.15
        v = float(lap[mask].var() / max(sm[mask].mean() ** 2, 1e-6) * 1e4) if mask.any() else 0.0
        return {"kind": "sharpness", "value": round(v, 3), "label": "Sharpness - higher is better"}

    # ----------------------------------------------------------- preview
    def preview_jpeg(self, max_w: int = 1280, stretch: str = "auto", quality: int = 80) -> bytes | None:
        with self.lock:
            frame, seq = self.latest, self.seq
            cam = self.camera
        if frame is None or cam is None:
            return None
        key = (max_w, stretch, quality)
        if self._preview_cache and self._preview_cache[0] == seq and self._preview_cache[1] == key:
            return self._preview_cache[2]
        rgb = self._display_rgb(frame, cam, max_w, stretch)
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
        data = buf.tobytes() if ok else None
        self._preview_cache = (seq, key, data)
        return data

    def _display_rgb(self, frame: np.ndarray, cam: Camera, max_w: int, stretch: str) -> np.ndarray:
        maxv = 255.0 if frame.dtype == np.uint8 else 65535.0
        bayer = cam.info.bayer if frame.ndim == 2 and cam.get_roi().get("bin", 1) == 1 else None
        if bayer and frame.shape[1] > max_w * 1.5:
            img = superpixel(frame, bayer) / maxv
        elif bayer:
            img = debayer(frame, bayer).astype(np.float32) / maxv
        else:
            img = frame.astype(np.float32) / maxv
            if img.ndim == 2:
                img = np.repeat(img[..., None], 3, axis=2)
        H, W = img.shape[:2]
        if W > max_w:
            img = cv2.resize(img, (max_w, int(H * max_w / W)), interpolation=cv2.INTER_AREA)
        if stretch in ("auto", "strong"):
            lo = float(np.percentile(img, 1))
            hi = float(np.percentile(img, 99.95))
            img = np.clip((img - lo) / max(hi - lo, 1e-6), 0, 1)
            med = float(np.median(img))
            goal = 0.3 if stretch == "strong" else 0.2
            # Lift the midtones only for faint deep-sky exposures; planet videos are shown linearly
            # (lifting their dark background just magnifies noise and burns out the planet).
            if (stretch == "strong" and med < goal) or (stretch == "auto" and self.mode == "exposures" and med < 0.15):
                m = med / goal
                img = ((m - 1) * img) / ((2 * m - 1) * img - m + 1e-9)
                img = np.clip(np.nan_to_num(img), 0, 1)
        elif stretch == "gamma":
            img = np.clip(img, 0, 1) ** (1 / 2.2)
        return (np.clip(img, 0, 1) * 255).astype(np.uint8)

    def livestack_jpeg(self, max_w: int = 1280) -> bytes | None:
        if not self.livestack:
            return None
        img = self.livestack.image()
        if img is None:
            return None
        if img.ndim == 2:
            img = np.repeat(img[..., None], 3, axis=2)
        from ..process.ops import Ctx, run_op
        disp = run_op("auto_stretch", np.nan_to_num(img), {"target": 0.2}, Ctx())
        H, W = disp.shape[:2]
        if W > max_w:
            disp = cv2.resize(disp, (max_w, int(H * max_w / W)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor((np.clip(disp, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR),
                               [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes() if ok else None

    # ----------------------------------------------------------- metadata
    def metadata(self, kind: str = "Light") -> dict:
        s = settings()
        cam = self.camera
        ctl = {k: c.value for k, c in cam.controls().items()} if cam else {}
        roi = cam.get_roi() if cam else {}
        eq = s.get("equipment")
        exp_s = self.exposure_s if self.mode == "exposures" else ctl.get("exposure_ms", 0) / 1000.0
        meta = {
            "OBJECT": self.target or "Unknown", "IMAGETYP": kind, "EXPTIME": round(exp_s, 6),
            "GAIN": ctl.get("gain"), "OFFSET": ctl.get("offset"), "CCD-TEMP": cam.temperature() if cam else None,
            "XBINNING": roi.get("bin", 1), "YBINNING": roi.get("bin", 1), "XORGSUBF": roi.get("x"), "YORGSUBF": roi.get("y"),
            "INSTRUME": cam.info.name if cam else "", "BAYERPAT": cam.info.bayer if cam and cam.info.color else None,
            "XPIXSZ": cam.info.pixel_um if cam else None, "TELESCOP": eq.get("telescope"),
            "FOCALLEN": settings().effective_focal_length(), "APTDIA": eq.get("aperture_mm"), "FILTER": eq.get("filter"),
            "BARLOW": (eq.get("barlows") or [{}])[int(eq.get("active_barlow", 0))].get("name") if eq.get("barlows") else None,
            "SITELAT": s.get("location.lat"), "SITELONG": s.get("location.lon"), "SITENAME": s.get("location.name"),
            "OBSERVER": s.get("observer", ""), "SWCREATE": SOFTWARE_ID,
            "DATE-OBS": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if cam is not None and hasattr(cam, "sky_scale") and getattr(cam, "scene_name", "") == "deepsky":
            # The simulator renders the sky as a wide field (like a camera lens); describe the optics it really
            # simulates, so plate solving and pixel-scale based tools work on recorded practice files.
            sky = cam.sky_scale or 10.5 * 3856 / max(roi.get("out_w") or 3856, 1) / max(roi.get("bin", 1), 1)
            meta.update({"FOCALLEN": round(206.265 * cam.info.pixel_um / sky, 2), "TELESCOP": "Simulated wide-field lens",
                         "NOTES": "AstroFlow simulator"})
        try:
            from .mount.controller import controller
            ms = controller.mount.status() if controller.mount and controller.mount.connected else None
            if ms and ms.ra is not None:
                meta.update({"RA": round(ms.ra, 5), "DEC": round(ms.dec, 5), "CENTALT": round(ms.alt or 0, 2),
                             "CENTAZ": round(ms.az or 0, 2), "MOUNT": ms.model or ms.driver})
        except Exception:
            pass
        return {k: v for k, v in meta.items() if v is not None}

    # ----------------------------------------------------------- recording
    def start_recording(self, target: str = "", fmt: str | None = None, max_frames: int = 0, max_seconds: float = 0,
                        folder: str | None = None) -> dict:
        cam = self.camera
        if cam is None:
            raise CameraError("No camera connected")
        if self.recorder is not None:
            raise CameraError("Already recording")
        self.target = target or self.target or "Untitled"
        base = Path(folder) if folder else settings().folder("captures") / night_date() / safe_name(self.target)
        base.mkdir(parents=True, exist_ok=True)
        self._check_disk(force=True)
        if self.disk_free is not None and self.disk_free < STOP_FREE * 1.5:
            raise CameraError(f"Only {self.disk_free / 1e9:.1f} GB free on the capture drive - free some space first")
        stamp = dt.datetime.now().strftime("%H%M%S")
        meta = self.metadata("Light")
        fmt = fmt or ("ser" if self.mode == "stream" else "fits")
        roi = cam.get_roi()
        if fmt == "ser":
            if self.mode != "stream":
                raise CameraError("SER video needs streaming mode (short exposures)")
            path = base / f"{safe_name(self.target)}_{stamp}.ser"
            bits = cam.info.bit_depth if cam.bit_depth_out == 16 else 8
            bayer = cam.info.bayer if cam.info.color and roi.get("bin", 1) == 1 else None
            rec = SerRecorder(path, roi["out_w"], roi["out_h"], bits, bayer, meta)
            frame_bytes = roi["out_w"] * roi["out_h"] * (2 if cam.bit_depth_out == 16 else 1)
            qsize = int(max(16, min(4000, settings().memory_budget_bytes() * 0.25 // max(frame_bytes, 1))))
            self._queue = queue.Queue(maxsize=qsize)
            self._writer = threading.Thread(target=self._write_loop, args=(rec,), name="ser-writer", daemon=True)
            self._writer.start()
        else:
            folder_p = base / f"lights_{stamp}"
            rec = FitsSequenceRecorder(folder_p, safe_name(self.target), meta)
            path = folder_p
        self.dropped_by_app = 0
        self.rec_info = {"path": str(path), "format": fmt, "started": time.time(), "max_frames": int(max_frames or 0),
                         "max_seconds": float(max_seconds or 0), "target": self.target}
        self.recorder = rec
        self.state = "recording" if self.mode == "stream" else "exposing"
        self.message = f"Recording to {path.name}"
        log.info("Recording started: %s", path)
        return dict(self.rec_info)

    def _write_loop(self, rec: SerRecorder) -> None:
        q = self._queue
        while True:
            item = q.get()
            if item is None:
                break
            frame, t = item
            try:
                rec.write(frame, t)
            except Exception as e:
                log.error("Writing frame failed: %s", e)
                self.message = f"Disk write error: {e} - recording stopped"
                threading.Thread(target=self.stop_recording, daemon=True).start()
                break

    def _write_exposure(self, frame: np.ndarray, now: dt.datetime) -> None:
        rec = self.recorder
        try:
            p = rec.write(frame, now, {"EXPTIME": self.exposure_s, "CCD-TEMP": self.camera.temperature() if self.camera else None})
            self.rec_info["last_file"] = str(p)
        except Exception as e:
            self.message = f"Disk write error: {e}"
            self.stop_recording()

    def _check_limits(self) -> None:
        info = self.rec_info
        rec = self.recorder
        if rec is None:
            return
        n = rec.frames + (self._queue.qsize() if self._queue else 0)
        if info.get("max_frames") and n >= info["max_frames"]:
            threading.Thread(target=self.stop_recording, kwargs={"reason": "frame limit reached"}, daemon=True).start()
        elif info.get("max_seconds") and time.time() - info["started"] >= info["max_seconds"]:
            threading.Thread(target=self.stop_recording, kwargs={"reason": "time limit reached"}, daemon=True).start()

    def stop_recording(self, reason: str = "stopped") -> dict | None:
        with self.lock:
            rec = self.recorder
            if rec is None:
                return None
            self.recorder = None
        if self._queue is not None:
            self._queue.put(None)
            if self._writer is not None:
                self._writer.join(timeout=120)
        try:
            rec.close()
        except Exception as e:
            log.error("Closing recording failed: %s", e)
        self._queue, self._writer = None, None
        out = {**self.rec_info, "frames": rec.frames, "bytes": rec.bytes, "dropped": self.dropped_by_app,
               "duration_s": round(time.time() - self.rec_info.get("started", time.time()), 1), "reason": reason}
        self.rec_info = {}
        self.message = f"Recording {reason}: {rec.frames} frames saved"
        if self.state in ("recording", "exposing"):
            self.state = "live" if self._acq and self._acq.is_alive() else "idle"
        log.info("Recording %s: %s", reason, out)
        try:
            from ..library.index import library
            library().add_paths([out["path"]])
        except Exception:
            pass
        self.last_recording = out
        return out

    def _check_disk(self, force: bool = False) -> None:
        self._last_disk_check = time.time()
        try:
            folder = settings().folder("captures")
            self.disk_free = shutil.disk_usage(folder).free
        except OSError:
            return
        if self.disk_free < STOP_FREE and self.recorder is not None:
            self.warnings.append("Disk almost full - recording stopped and saved.")
            threading.Thread(target=self.stop_recording, kwargs={"reason": "disk almost full"}, daemon=True).start()
        elif self.disk_free < WARN_FREE:
            msg = f"Low disk space: {self.disk_free / 1e9:.1f} GB free on the capture drive."
            if not self.warnings or self.warnings[-1] != msg:
                self.warnings.append(msg)

    # ------------------------------------------------------------ snapshot
    def snapshot(self, fmt: str = "fits", target: str = "") -> str:
        with self.lock:
            frame, t = self.latest, self.latest_t
        if frame is None:
            raise CameraError("No frame yet")
        base = settings().folder("captures") / night_date() / safe_name(target or self.target or "Snapshot")
        stamp = dt.datetime.now().strftime("%H%M%S")
        meta = self.metadata("Light")
        if fmt == "fits":
            p = base / f"snapshot_{stamp}.fits"
            write_fits(p, frame, meta, bits=16 if frame.dtype == np.uint16 else 32)
        else:
            rgb = self._display_rgb(frame, self.camera, 10**6, "none" if fmt == "tiff" else "auto")
            p = base / f"snapshot_{stamp}.{fmt}"
            if fmt == "tiff":
                bayer = self.camera.info.bayer if frame.ndim == 2 else None
                img = debayer(frame, bayer) if bayer else frame
                maxv = 255.0 if img.dtype == np.uint8 else 65535.0
                write_tiff(p, img.astype(np.float32) / maxv, 16, meta)
            else:
                write_standard(p, rgb.astype(np.float32) / 255, fmt, 8, 95, meta=meta)
        try:
            from ..library.index import library
            library().add_paths([str(p)])
        except Exception:
            pass
        return str(p)

    # ------------------------------------------------------------- live stack
    def start_livestack(self, use_calibration: bool = True) -> None:
        cam = self.camera
        if cam is None:
            raise CameraError("No camera connected")
        cal = None
        if use_calibration:
            from ..imaging.calibrate import CalibrationLibrary, CalibrationSet
            lib = CalibrationLibrary()
            meta = self.metadata()
            meta.update({"_width": cam.get_roi()["out_w"], "_height": cam.get_roi()["out_h"]})
            d, _ = lib.find("dark", meta)
            f, _ = lib.find("flat", meta)
            if d or f:
                cal = CalibrationSet(None, lib.load(d) if d else None, lib.load(f) if f else None,
                                     (d or {}).get("exposure"))
        bayer = cam.info.bayer if cam.info.color and cam.get_roi().get("bin", 1) == 1 else None
        self.livestack = LiveStacker(bayer, cal)
        self.livestack_info = {"stacked": False, "message": "Waiting for the first frame", "frames": 0,
                               "calibrated": cal is not None}

    def stop_livestack(self, save: bool = True) -> str | None:
        ls = self.livestack
        self.livestack = None
        if ls is None or not save or ls.frames == 0:
            return None
        img = ls.image()
        base = settings().folder("captures") / night_date() / safe_name(self.target or "LiveStack")
        p = base / f"livestack_{dt.datetime.now().strftime('%H%M%S')}.fits"
        write_fits(p, np.nan_to_num(img), {**self.metadata(), "NCOMBINE": ls.frames, "IMAGETYP": "Live stack"}, bits=32)
        return str(p)

    # ---------------------------------------------- calibration assistant
    def start_calibration(self, kind: str, count: int, exposure_s: float | None = None) -> dict:
        """Capture ``count`` calibration frames of ``kind`` (dark/flat/flatdark/bias), then build a master."""
        if kind not in ("dark", "flat", "flatdark", "bias"):
            raise CameraError("Unknown calibration frame type")
        if self.recorder:
            raise CameraError("Stop recording first")
        cam = self.camera
        if cam is None:
            raise CameraError("No camera connected")
        if kind == "bias":
            exposure_s = 0.0001
        exposure_s = float(exposure_s if exposure_s is not None else self.exposure_s)
        folder = calibration_dir() / "raw" / f"{kind}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.calib = {"kind": kind, "count": int(count), "done": 0, "folder": str(folder), "exposure_s": exposure_s,
                      "files": [], "status": "capturing"}
        self.set_mode("exposures", exposure_s)
        if not (self._acq and self._acq.is_alive()):
            self.start_live()
        return dict(self.calib)

    def _calib_frame(self, frame: np.ndarray, now: dt.datetime) -> None:
        c = self.calib
        if c is None or c["status"] != "capturing":
            return
        imagetyp = {"dark": "Dark", "flat": "Flat", "flatdark": "Dark Flat", "bias": "Bias"}[c["kind"]]
        meta = self.metadata(imagetyp)
        meta["EXPTIME"] = c["exposure_s"]
        p = Path(c["folder"]) / f"{c['kind']}_{c['done'] + 1:03d}.fits"
        write_fits(p, frame, meta, bits=16)
        c["files"].append(str(p))
        c["done"] += 1
        if c["kind"] == "flat":
            maxv = 255 if frame.dtype == np.uint8 else 65535
            level = float(np.median(frame)) / maxv
            c["flat_level"] = round(level, 3)
            c["flat_advice"] = ("Flat is too dark - increase exposure" if level < 0.25 else
                                "Flat is too bright - decrease exposure" if level > 0.75 else "Flat brightness is good")
        if c["done"] >= c["count"]:
            c["status"] = "building master"
            threading.Thread(target=self._build_master, name="build-master", daemon=True).start()

    def _build_master(self) -> None:
        from ..imaging.calibrate import CalibrationLibrary, make_master
        c = self.calib
        try:
            master, meta = make_master(c["files"], c["kind"])
            entry = CalibrationLibrary().add(c["kind"], master, meta, len(c["files"]), note="calibration assistant")
            c.update({"status": "done", "master": entry["path"], "entry": entry})
        except Exception as e:
            c.update({"status": "error", "error": str(e)})
        self.stop_live()

    def cancel_calibration(self) -> None:
        if self.calib:
            self.calib["status"] = "cancelled"
        self.calib = None

    # ----------------------------------------------------- collimation aid
    def collimation(self) -> dict:
        with self.lock:
            frame = self.latest
            cam = self.camera
        if frame is None or cam is None:
            return {"ok": False, "message": "No frame yet"}
        bayer = cam.info.bayer if frame.ndim == 2 else None
        img = bayer_luminance(frame) if bayer else frame.astype(np.float32)
        if img.ndim == 3:
            img = img.mean(axis=2)
        scale = 2 if bayer else 1
        img = cv2.GaussianBlur(img, (0, 0), 1.5)
        lo, hi = float(np.percentile(img, 50)), float(img.max())
        if hi - lo < 1e-6:
            return {"ok": False, "message": "No star visible"}
        mask = (img > lo + 0.3 * (hi - lo)).astype(np.uint8)
        cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        if not cnts:
            return {"ok": False, "message": "No star visible"}
        big = max(range(len(cnts)), key=lambda i: cv2.contourArea(cnts[i]))
        (ox, oy), r_out = cv2.minEnclosingCircle(cnts[big])
        holes = [i for i in range(len(cnts)) if hier[0][i][3] == big]
        if not holes or r_out < 6:
            return {"ok": True, "outer": [ox * scale, oy * scale, r_out * scale], "inner": None,
                    "message": "Defocus the star until it becomes a ring (donut) with a dark centre."}
        hole = max(holes, key=lambda i: cv2.contourArea(cnts[i]))
        m = cv2.moments(cnts[hole])
        ix, iy = m["m10"] / max(m["m00"], 1e-9), m["m01"] / max(m["m00"], 1e-9)
        r_in = math.sqrt(cv2.contourArea(cnts[hole]) / math.pi)
        off = math.hypot(ix - ox, iy - oy) / r_out
        verdict = ("Excellent - well collimated" if off < 0.03 else "Good" if off < 0.07 else
                   "Collimation needs adjusting: the dark shadow is off-centre")
        return {"ok": True, "outer": [ox * scale, oy * scale, r_out * scale], "inner": [ix * scale, iy * scale, r_in * scale],
                "offset_percent": round(off * 100, 1), "direction_deg": round(math.degrees(math.atan2(iy - oy, ix - ox)), 0),
                "message": verdict}

    # ------------------------------------------------------------- status
    def status(self) -> dict:
        cam = self.camera
        rec = self.recorder
        out = {"state": self.state, "mode": self.mode, "exposure_s": self.exposure_s, "fps": round(self.fps, 1),
               "seq": self.seq, "message": self.message, "warnings": self.warnings[-5:], "target": self.target,
               "camera": cam.info.to_dict() if cam else None, "camera_kind": self.camera_kind,
               "disk_free": self.disk_free, "stats": self.stats, "livestack": self.livestack_info if self.livestack else None,
               "calibration": {k: v for k, v in (self.calib or {}).items() if k != "files"} if self.calib else None,
               "focus_history": list(self.focus_history)[-150:], "last_recording": getattr(self, "last_recording", None)}
        if cam:
            try:
                out["controls"] = {k: c.to_dict() for k, c in cam.controls().items()}
                out["roi"] = cam.get_roi()
                out["temperature"] = cam.temperature()
                out["bit_depth"] = cam.bit_depth_out
            except Exception as e:
                out["controls"] = {}
                out["message"] = str(e)
        if rec is not None:
            el = time.time() - self.rec_info.get("started", time.time())
            qlen = self._queue.qsize() if self._queue else 0
            out["recording"] = {**self.rec_info, "frames": rec.frames + qlen, "written": rec.frames, "bytes": rec.bytes,
                                "elapsed_s": round(el, 1), "buffer": qlen, "dropped": self.dropped_by_app}
        return out


capture = CaptureManager()
