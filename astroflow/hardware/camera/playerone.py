"""Player One Astronomy cameras (Uranus-C and others) through Player One's official SDK.

Why the SDK and not ASCOM: the native SDK delivers the full frame rate
needed for planetary video (hundreds of fps with a small region of
interest), raw 16-bit data and region-of-interest control. Player One's
ASCOM driver is fine for deep-sky stills but slower for video.

The SDK library (PlayerOneCamera.dll on Windows) is Player One's software and
is not bundled with AstroFlow. The setup wizard finds it (SharpCap, N.I.N.A.
and Player One's own software install it) or takes the SDK zip you download
from player-one-astronomy.com.

Function signatures follow PlayerOneCamera.h (SDK 3.x). The library was
verified to load and enumerate here; capture itself is UNTESTED ON REAL
HARDWARE until you run the hardware checklist.
"""
from __future__ import annotations

import ctypes
import logging
import os
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

from ...config import data_dir, settings
from .base import CONTROL_HELP, Camera, CameraDisconnected, CameraError, CameraInfo, Control

log = logging.getLogger("astroflow.camera.playerone")

c_int, c_double, c_char = ctypes.c_int, ctypes.c_double, ctypes.c_char
# C "long" is 32-bit on Windows and 64-bit on Linux/macOS.
c_long = ctypes.c_int32 if sys.platform.startswith("win") else ctypes.c_int64

POA_OK = 0
ERRORS = ["OK", "invalid index", "invalid camera ID", "invalid config", "invalid argument", "camera not opened",
          "device not found (unplugged?)", "value out of limit", "exposure failed", "timeout", "buffer too small",
          "camera is exposing", "invalid pointer", "config not writable", "config not readable", "access denied",
          "operation failed (camera disconnected?)", "memory allocation failed"]
BAYER = {0: "RGGB", 1: "BGGR", 2: "GRBG", 3: "GBRG", -1: None}
RAW8, RAW16 = 0, 1

# POAConfig ids
CFG = {"exposure_us": 0, "gain": 1, "hardware_bin": 2, "temperature": 3, "wb_r": 4, "wb_g": 5, "wb_b": 6, "offset": 7,
       "guide_n": 11, "egain": 15, "cooler_power": 16, "target_temp": 17, "cooler": 18, "heater_power": 20,
       "fan_power": 21, "frame_limit": 26, "usb_bandwidth": 28, "pixel_bin_sum": 29, "mono_bin": 30}
VAL_INT, VAL_FLOAT, VAL_BOOL = 0, 1, 2


class POACameraProperties(ctypes.Structure):
    _fields_ = [("cameraModelName", c_char * 256), ("userCustomID", c_char * 16), ("cameraID", c_int),
                ("maxWidth", c_int), ("maxHeight", c_int), ("bitDepth", c_int), ("isColorCamera", c_int),
                ("isHasST4Port", c_int), ("isHasCooler", c_int), ("isUSB3Speed", c_int), ("bayerPattern", c_int),
                ("pixelSize", c_double), ("SN", c_char * 64), ("sensorModelName", c_char * 32),
                ("localPath", c_char * 256), ("bins", c_int * 8), ("imgFormats", c_int * 8),
                ("isSupportHardBin", c_int), ("pID", c_int), ("reserved", c_char * 248)]


class POAConfigValue(ctypes.Union):
    _fields_ = [("intValue", c_long), ("floatValue", c_double), ("boolValue", c_int)]


class POAConfigAttributes(ctypes.Structure):
    _fields_ = [("isSupportAuto", c_int), ("isWritable", c_int), ("isReadable", c_int), ("configID", c_int),
                ("valueType", c_int), ("maxValue", POAConfigValue), ("minValue", POAConfigValue),
                ("defaultValue", POAConfigValue), ("szConfName", c_char * 64), ("szDescription", c_char * 128),
                ("reserved", c_char * 64)]


def candidate_paths() -> list[Path]:
    name = "PlayerOneCamera.dll" if sys.platform.startswith("win") else (
        "libPlayerOneCamera.dylib" if sys.platform == "darwin" else "libPlayerOneCamera.so")
    out = []
    configured = settings().get("camera.playerone_dll", "")
    if configured:
        out.append(Path(configured))
    out.append(data_dir() / "drivers" / name)
    if sys.platform.startswith("win"):
        pf = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")), Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
        for base in pf:
            if base.exists():
                out += sorted(base.glob("SharpCap*/PlayerOneCamera.dll"))
                out += sorted(base.glob("SharpCap*/*/PlayerOneCamera.dll"))
                out += sorted(base.glob("N.I.N.A*/External/x64/PlayerOne/PlayerOneCamera.dll"))
                out += sorted(base.glob("Player One*/**/x64/PlayerOneCamera.dll"))
                out += sorted(base.glob("FireCapture*/**/PlayerOneCamera.dll"))
    return [p for p in out if p.exists()]


def install_from_sdk_zip(zip_path: str | Path) -> Path:
    """Extract the right library from Player One's SDK zip into AstroFlow's drivers folder."""
    import zipfile
    dest = data_dir() / "drivers"
    dest.mkdir(parents=True, exist_ok=True)
    want = "PlayerOneCamera.dll" if sys.platform.startswith("win") else "libPlayerOneCamera"
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if Path(n).name.startswith(want)]
        if sys.platform.startswith("win"):
            names = [n for n in names if "x64" in n.replace("\\", "/").lower()] or names
        if not names:
            raise CameraError("No PlayerOneCamera library found in that zip")
        for n in names:
            target = dest / Path(n).name
            with z.open(n) as src, open(target, "wb") as dst:
                dst.write(src.read())
    found = candidate_paths()
    if not found:
        raise CameraError("Extracted, but the library could not be found afterwards")
    return found[0]


class _SDK:
    _inst: "_SDK | None" = None
    _lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = path
        self.dll = ctypes.cdll.LoadLibrary(str(path))
        d = self.dll
        d.POAGetCameraCount.restype = c_int
        d.POAGetCameraProperties.argtypes = [c_int, ctypes.POINTER(POACameraProperties)]
        d.POAGetErrorString.restype = ctypes.c_char_p
        d.POAGetSDKVersion.restype = ctypes.c_char_p
        # POASetConfig takes the 8-byte value union BY VALUE. Python's ctypes can't pass unions
        # by value, but an 8-byte union travels in an integer register on both Win64 and
        # x86-64 SysV, so we pass its bytes as a 64-bit integer (floats: their bit pattern).
        d.POASetConfig.argtypes = [c_int, c_int, ctypes.c_int64, c_int]
        d.POAGetConfig.argtypes = [c_int, c_int, ctypes.POINTER(POAConfigValue), ctypes.POINTER(c_int)]
        d.POAGetConfigsCount.argtypes = [c_int, ctypes.POINTER(c_int)]
        d.POAGetConfigAttributes.argtypes = [c_int, c_int, ctypes.POINTER(POAConfigAttributes)]
        d.POAGetConfigAttributesByConfigID.argtypes = [c_int, c_int, ctypes.POINTER(POAConfigAttributes)]
        d.POASetImageFormat.argtypes = [c_int, c_int]
        d.POASetImageBin.argtypes = [c_int, c_int]
        d.POASetImageStartPos.argtypes = [c_int, c_int, c_int]
        d.POASetImageSize.argtypes = [c_int, c_int, c_int]
        d.POAGetImageSize.argtypes = [c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_int)]
        d.POAGetImageStartPos.argtypes = [c_int, ctypes.POINTER(c_int), ctypes.POINTER(c_int)]
        d.POAStartExposure.argtypes = [c_int, c_int]
        d.POAStopExposure.argtypes = [c_int]
        d.POAImageReady.argtypes = [c_int, ctypes.POINTER(c_int)]
        d.POAGetImageData.argtypes = [c_int, ctypes.c_void_p, c_long, c_int]
        d.POAGetDroppedImagesCount.argtypes = [c_int, ctypes.POINTER(c_int)]
        d.POAGetCameraState.argtypes = [c_int, ctypes.POINTER(c_int)]
        for f in ("POAOpenCamera", "POAInitCamera", "POACloseCamera"):
            getattr(d, f).argtypes = [c_int]

    @classmethod
    def get(cls) -> "_SDK":
        with cls._lock:
            if cls._inst is None:
                paths = candidate_paths()
                if not paths:
                    raise CameraError("Player One camera library (PlayerOneCamera.dll) not found. Run the setup wizard's "
                                      "camera step to locate it or install it from Player One's SDK zip.")
                last = None
                for p in paths:
                    try:
                        cls._inst = cls(p)
                        log.info("Loaded Player One SDK %s from %s", cls._inst.version(), p)
                        break
                    except OSError as e:
                        last = e
                if cls._inst is None:
                    raise CameraError(f"Could not load the Player One library: {last}")
            return cls._inst

    def version(self) -> str:
        return self.dll.POAGetSDKVersion().decode()

    def check(self, err: int, what: str) -> None:
        if err != POA_OK:
            msg = ERRORS[err] if 0 <= err < len(ERRORS) else str(err)
            if err in (6, 16):
                raise CameraDisconnected(f"{what}: {msg}")
            raise CameraError(f"{what}: {msg}")


def list_cameras() -> list[dict]:
    sdk = _SDK.get()
    out = []
    for i in range(sdk.dll.POAGetCameraCount()):
        p = POACameraProperties()
        if sdk.dll.POAGetCameraProperties(i, ctypes.byref(p)) == POA_OK:
            out.append({"index": i, "id": p.cameraID, "name": p.cameraModelName.decode(errors="replace"),
                        "serial": p.SN.decode(errors="replace"), "sensor": p.sensorModelName.decode(errors="replace")})
    return out


class PlayerOneCamera(Camera):
    def __init__(self, camera_id: int | None = None):
        self.sdk = _SDK.get()
        self.cid = camera_id
        self._open = False
        self._streaming = False
        self._bits = 16
        self._roi = {"x": 0, "y": 0, "w": 0, "h": 0, "bin": 1}
        self._buf: np.ndarray | None = None
        self._lock = threading.Lock()
        self._attrs: dict[int, POAConfigAttributes] = {}
        self._abort = threading.Event()

    def connect(self) -> None:
        d = self.sdk.dll
        n = d.POAGetCameraCount()
        if n == 0:
            raise CameraError("No Player One camera found. Check the USB 3 cable and that the camera shows in Windows Device Manager.")
        props = None
        for i in range(n):
            p = POACameraProperties()
            self.sdk.check(d.POAGetCameraProperties(i, ctypes.byref(p)), "read camera properties")
            if self.cid is None or p.cameraID == self.cid:
                props = p
                break
        if props is None:
            raise CameraError("That camera is not connected")
        self.cid = props.cameraID
        self.sdk.check(d.POAOpenCamera(self.cid), "open camera")
        self.sdk.check(d.POAInitCamera(self.cid), "initialise camera")
        self._open = True
        self.info = CameraInfo(
            id=str(self.cid), name=props.cameraModelName.decode(errors="replace"),
            sensor=props.sensorModelName.decode(errors="replace"), width=props.maxWidth, height=props.maxHeight,
            pixel_um=props.pixelSize, bit_depth=props.bitDepth, color=bool(props.isColorCamera),
            bayer=BAYER.get(props.bayerPattern) if props.isColorCamera else None, has_cooler=bool(props.isHasCooler),
            usb3=bool(props.isUSB3Speed), bins=[b for b in props.bins if b > 0] or [1], driver="Player One SDK " + self.sdk.version(),
            serial=props.SN.decode(errors="replace"))
        cnt = c_int(0)
        if d.POAGetConfigsCount(self.cid, ctypes.byref(cnt)) == POA_OK:
            for i in range(cnt.value):
                a = POAConfigAttributes()
                if d.POAGetConfigAttributes(self.cid, i, ctypes.byref(a)) == POA_OK:
                    self._attrs[a.configID] = a
        self.set_bit_depth(16)
        self.set_roi(0, 0, self.info.width, self.info.height, 1)

    def disconnect(self) -> None:
        if self._open:
            try:
                self.stop_stream()
            except CameraError:
                pass
            self.sdk.dll.POACloseCamera(self.cid)
        self._open = False

    @property
    def connected(self) -> bool:
        return self._open

    # -- controls ------------------------------------------------------------
    def _get_cfg(self, cfg: int) -> tuple[float, bool]:
        v = POAConfigValue()
        auto = c_int(0)
        self.sdk.check(self.sdk.dll.POAGetConfig(self.cid, cfg, ctypes.byref(v), ctypes.byref(auto)), "read setting")
        a = self._attrs.get(cfg)
        if a is not None and a.valueType == VAL_FLOAT:
            return float(v.floatValue), bool(auto.value)
        if a is not None and a.valueType == VAL_BOOL:
            return float(v.boolValue), bool(auto.value)
        return float(v.intValue), bool(auto.value)

    def _set_cfg(self, cfg: int, value: float, auto: bool = False) -> None:
        a = self._attrs.get(cfg)
        if a is not None and a.valueType == VAL_FLOAT:
            raw = struct.unpack("<q", struct.pack("<d", float(value)))[0]
        else:
            raw = int(round(value))
        self.sdk.check(self.sdk.dll.POASetConfig(self.cid, cfg, raw, 1 if auto else 0), "change setting")

    def _range(self, cfg: int) -> tuple[float, float]:
        a = self._attrs.get(cfg)
        if a is None:
            return 0.0, 0.0
        if a.valueType == VAL_FLOAT:
            return a.minValue.floatValue, a.maxValue.floatValue
        return float(a.minValue.intValue), float(a.maxValue.intValue)

    def controls(self) -> dict[str, Control]:
        out: dict[str, Control] = {}
        def add(name, cfg, label, scale=1.0, step=1.0):
            if cfg not in self._attrs:
                return
            v, auto = self._get_cfg(cfg)
            lo, hi = self._range(cfg)
            a = self._attrs[cfg]
            out[name] = Control(name, label, v * scale, lo * scale, hi * scale, step, auto, bool(a.isSupportAuto),
                                bool(a.isWritable), CONTROL_HELP.get(name, ""))
        add("exposure_ms", CFG["exposure_us"], "Exposure (ms)", 0.001, 0.001)
        add("gain", CFG["gain"], "Gain")
        add("offset", CFG["offset"], "Offset")
        add("usb_bandwidth", CFG["usb_bandwidth"], "USB bandwidth (%)")
        add("frame_limit", CFG["frame_limit"], "Frame rate limit (fps)")
        add("wb_r", CFG["wb_r"], "White balance red")
        add("wb_b", CFG["wb_b"], "White balance blue")
        if self.info.has_cooler:
            add("cooler", CFG["cooler"], "Cooler on")
            add("target_temp", CFG["target_temp"], "Target temperature (C)")
        return out

    def set_control(self, name: str, value: float, auto: bool = False) -> None:
        if name == "exposure_ms":
            self._set_cfg(CFG["exposure_us"], value * 1000.0, auto)
        elif name in CFG:
            self._set_cfg(CFG[name], value, auto)
        else:
            raise CameraError(f"Unknown setting {name}")

    def temperature(self) -> float | None:
        try:
            return self._get_cfg(CFG["temperature"])[0]
        except CameraError:
            return None

    # -- format / ROI --------------------------------------------------------
    def set_bit_depth(self, bits: int) -> None:
        was = self._streaming
        if was:
            self.stop_stream()
        self.sdk.check(self.sdk.dll.POASetImageFormat(self.cid, RAW16 if bits > 8 else RAW8), "set bit depth")
        self._bits = 16 if bits > 8 else 8
        if was:
            self.start_stream()

    def set_roi(self, x: int, y: int, w: int, h: int, binning: int = 1) -> dict:
        was = self._streaming
        if was:
            self.stop_stream()
        d = self.sdk.dll
        b = binning if binning in self.info.bins else 1
        self.sdk.check(d.POASetImageBin(self.cid, b), "set binning")
        W, H = self.info.width // b, self.info.height // b
        w, h = max(16, min(int(w) // b, W)) // 4 * 4, max(16, min(int(h) // b, H)) // 2 * 2
        x, y = min(max(0, int(x) // b), W - w), min(max(0, int(y) // b), H - h)
        self.sdk.check(d.POASetImageSize(self.cid, w, h), "set image size")
        self.sdk.check(d.POASetImageStartPos(self.cid, x, y), "set image position")
        cw, ch = c_int(0), c_int(0)
        d.POAGetImageSize(self.cid, ctypes.byref(cw), ctypes.byref(ch))
        self._roi = {"x": x * b, "y": y * b, "w": cw.value * b, "h": ch.value * b, "bin": b, "out_w": cw.value, "out_h": ch.value}
        self._buf = None
        if was:
            self.start_stream()
        return dict(self._roi)

    def get_roi(self) -> dict:
        return dict(self._roi)

    @property
    def bit_depth_out(self) -> int:
        return self._bits

    def _frame_buffer(self) -> np.ndarray:
        w, h = self._roi["out_w"], self._roi["out_h"]
        dt = np.uint16 if self._bits == 16 else np.uint8
        if self._buf is None or self._buf.shape != (h, w) or self._buf.dtype != dt:
            self._buf = np.empty((h, w), dt)
        return self._buf

    # -- capture -------------------------------------------------------------
    def start_stream(self) -> None:
        self.sdk.check(self.sdk.dll.POAStartExposure(self.cid, 0), "start video")
        self._streaming = True

    def stop_stream(self) -> None:
        if self._streaming:
            self.sdk.dll.POAStopExposure(self.cid)
        self._streaming = False

    def get_frame(self, timeout_s: float = 2.0) -> np.ndarray | None:
        buf = self._frame_buffer()
        err = self.sdk.dll.POAGetImageData(self.cid, buf.ctypes.data, buf.nbytes, int(timeout_s * 1000))
        if err == 9:   # timeout
            return None
        self.sdk.check(err, "read frame")
        return buf.copy()

    def expose(self, seconds: float) -> np.ndarray:
        self._abort.clear()
        if self._streaming:
            self.stop_stream()
        self.set_control("exposure_ms", seconds * 1000.0)
        self.sdk.check(self.sdk.dll.POAStartExposure(self.cid, 1), "start exposure")
        ready = c_int(0)
        t_end = time.time() + seconds + 30
        while time.time() < t_end:
            if self._abort.is_set():
                self.sdk.dll.POAStopExposure(self.cid)
                raise CameraError("Exposure aborted")
            self.sdk.check(self.sdk.dll.POAImageReady(self.cid, ctypes.byref(ready)), "wait for image")
            if ready.value:
                buf = self._frame_buffer()
                self.sdk.check(self.sdk.dll.POAGetImageData(self.cid, buf.ctypes.data, buf.nbytes, 1000), "read image")
                return buf.copy()
            time.sleep(0.02 if seconds < 1 else 0.1)
        raise CameraError("Exposure timed out")

    def abort(self) -> None:
        self._abort.set()

    def dropped_frames(self) -> int:
        n = c_int(0)
        self.sdk.dll.POAGetDroppedImagesCount(self.cid, ctypes.byref(n))
        return n.value
