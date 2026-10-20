"""Any camera with an ASCOM driver (Windows). Fine for deep-sky stills; slow for planetary video.

UNTESTED ON REAL HARDWARE (needs Windows + the camera's ASCOM driver).
"""
from __future__ import annotations

import sys
import threading
import time

import numpy as np

from .base import CONTROL_HELP, Camera, CameraDisconnected, CameraError, CameraInfo, Control

SENSOR_BAYER = {0: None, 1: None, 2: "RGGB"}   # ASCOM SensorType: 0 mono, 1 colour (rare), 2 RGGB...


class AscomCamera(Camera):
    def __init__(self, progid: str):
        self.progid = progid
        self.c = None
        self._abort = threading.Event()
        self._stream_exp = 0.01
        self._streaming = False

    @staticmethod
    def choose(current: str = "") -> str | None:
        import win32com.client  # type: ignore
        ch = win32com.client.Dispatch("ASCOM.Utilities.Chooser")
        ch.DeviceType = "Camera"
        return ch.Choose(current) or None

    def connect(self) -> None:
        if not sys.platform.startswith("win"):
            raise CameraError("ASCOM drivers only exist on Windows")
        import win32com.client  # type: ignore
        self.c = win32com.client.Dispatch(self.progid)
        self.c.Connected = True
        c = self.c
        try:
            bayer = SENSOR_BAYER.get(int(c.SensorType), None)
            if bayer and (int(c.BayerOffsetX), int(c.BayerOffsetY)) != (0, 0):
                bayer = {(1, 0): "GRBG", (0, 1): "GBRG", (1, 1): "BGGR"}.get((int(c.BayerOffsetX), int(c.BayerOffsetY)), "RGGB")
        except Exception:
            bayer = None
        self.info = CameraInfo(id=self.progid, name=str(c.Name), sensor=str(getattr(c, "SensorName", "")),
                               width=int(c.CameraXSize), height=int(c.CameraYSize), pixel_um=float(c.PixelSizeX),
                               bit_depth=16, color=bayer is not None, bayer=bayer, has_cooler=bool(c.CanSetCCDTemperature),
                               bins=list(range(1, int(c.MaxBinX) + 1)), driver="ASCOM")

    def disconnect(self) -> None:
        if self.c is not None:
            try:
                self.c.Connected = False
            except Exception:
                pass
        self.c = None

    @property
    def connected(self) -> bool:
        return self.c is not None

    def controls(self) -> dict[str, Control]:
        c = self.c
        out = {"exposure_ms": Control("exposure_ms", "Exposure (ms)", self._stream_exp * 1000, float(c.ExposureMin) * 1000,
                                      float(c.ExposureMax) * 1000, 0.1, help=CONTROL_HELP["exposure_ms"])}
        try:
            out["gain"] = Control("gain", "Gain", float(c.Gain), float(c.GainMin), float(c.GainMax), 1, help=CONTROL_HELP["gain"])
        except Exception:
            pass
        try:
            out["offset"] = Control("offset", "Offset", float(c.Offset), float(c.OffsetMin), float(c.OffsetMax), 1, help=CONTROL_HELP["offset"])
        except Exception:
            pass
        if self.info.has_cooler:
            out["target_temp"] = Control("target_temp", "Target temperature (C)", float(c.SetCCDTemperature), -40, 30, 1)
            out["cooler"] = Control("cooler", "Cooler on", float(bool(c.CoolerOn)), 0, 1, 1)
        return out

    def set_control(self, name: str, value: float, auto: bool = False) -> None:
        c = self.c
        if name == "exposure_ms":
            self._stream_exp = value / 1000.0
        elif name == "gain":
            c.Gain = int(value)
        elif name == "offset":
            c.Offset = int(value)
        elif name == "target_temp":
            c.SetCCDTemperature = float(value)
        elif name == "cooler":
            c.CoolerOn = bool(value)

    def set_roi(self, x: int, y: int, w: int, h: int, binning: int = 1) -> dict:
        c = self.c
        c.BinX = c.BinY = binning
        c.StartX, c.StartY = x // binning, y // binning
        c.NumX, c.NumY = w // binning, h // binning
        return self.get_roi()

    def get_roi(self) -> dict:
        c = self.c
        b = int(c.BinX)
        return {"x": int(c.StartX) * b, "y": int(c.StartY) * b, "w": int(c.NumX) * b, "h": int(c.NumY) * b, "bin": b,
                "out_w": int(c.NumX), "out_h": int(c.NumY)}

    def start_stream(self) -> None:
        self._streaming = True

    def stop_stream(self) -> None:
        self._streaming = False

    def get_frame(self, timeout_s: float = 2.0) -> np.ndarray | None:
        return self.expose(self._stream_exp)

    def expose(self, seconds: float) -> np.ndarray:
        c = self.c
        self._abort.clear()
        try:
            c.StartExposure(float(seconds), True)
            t_end = time.time() + seconds + 60
            while not c.ImageReady:
                if self._abort.is_set():
                    try:
                        c.AbortExposure()
                    except Exception:
                        pass
                    raise CameraError("Exposure aborted")
                if time.time() > t_end:
                    raise CameraError("Exposure timed out")
                time.sleep(0.05)
            arr = np.array(c.ImageArray, dtype=np.int64)
        except CameraError:
            raise
        except Exception as e:
            raise CameraDisconnected(f"ASCOM camera error: {e}") from e
        # ASCOM ImageArray is [x][y]; transpose to rows x columns.
        img = arr.T if arr.ndim == 2 else np.transpose(arr, (1, 0, 2))
        return np.clip(img, 0, 65535).astype(np.uint16)

    def abort(self) -> None:
        self._abort.set()

    def temperature(self) -> float | None:
        try:
            return float(self.c.CCDTemperature)
        except Exception:
            return None
