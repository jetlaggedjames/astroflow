"""The common interface every camera driver provides (Player One SDK, ASCOM, simulator).

Two ways of taking pictures:
  * streaming ("video") - the camera exposes continuously; used for planetary
    and lunar recordings and for the live view.
  * single exposures - one long exposure at a time; used for deep-sky subs.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

import numpy as np


class CameraError(RuntimeError):
    pass


class CameraDisconnected(CameraError):
    pass


@dataclass
class CameraInfo:
    id: str
    name: str
    sensor: str = ""
    width: int = 0
    height: int = 0
    pixel_um: float = 0.0
    bit_depth: int = 12
    color: bool = True
    bayer: str | None = "RGGB"
    has_cooler: bool = False
    usb3: bool = True
    bins: list = field(default_factory=lambda: [1, 2])
    driver: str = ""
    serial: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Control:
    name: str
    label: str
    value: float
    min: float
    max: float
    step: float = 1.0
    auto: bool = False
    can_auto: bool = False
    writable: bool = True
    help: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


CONTROL_HELP = {
    "exposure_ms": "How long each frame collects light. Planets: 2-20 ms to freeze the air's shimmer. Deep sky: 2-30 s on an alt-az mount.",
    "gain": "Electronic amplification. Higher = brighter but noisier. Planets often use high gain with short exposures.",
    "offset": "Lifts the black level slightly so the faintest signal isn't cut off. Leave at the camera's default unless you know why.",
    "usb_bandwidth": "Share of the USB 3 link the camera may use. Lower it if frames get dropped on a slow laptop.",
    "frame_limit": "Maximum frames per second (0 = as fast as possible).",
    "target_temp": "Cooler set point (cooled cameras only).",
    "cooler": "Switch the sensor cooler on or off (cooled cameras only).",
    "wb_r": "Red white balance (only affects colour preview data, not RAW).",
    "wb_b": "Blue white balance (only affects colour preview data, not RAW).",
}


class Camera(ABC):
    info: CameraInfo

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def controls(self) -> dict[str, Control]: ...

    @abstractmethod
    def set_control(self, name: str, value: float, auto: bool = False) -> None: ...

    @abstractmethod
    def set_roi(self, x: int, y: int, w: int, h: int, binning: int = 1) -> dict:
        """Region of interest in unbinned sensor pixels. Returns the ROI actually applied."""

    @abstractmethod
    def get_roi(self) -> dict: ...

    def set_bit_depth(self, bits: int) -> None:
        pass

    @abstractmethod
    def start_stream(self) -> None: ...

    @abstractmethod
    def stop_stream(self) -> None: ...

    @abstractmethod
    def get_frame(self, timeout_s: float = 2.0) -> np.ndarray | None:
        """Next streamed frame (uint8/uint16, Bayer mosaic for colour sensors), or None on timeout."""

    @abstractmethod
    def expose(self, seconds: float) -> np.ndarray:
        """One single exposure; blocks until done (abort() cancels)."""

    def abort(self) -> None:
        pass

    def temperature(self) -> float | None:
        return None

    def dropped_frames(self) -> int:
        return 0

    @property
    def bit_depth_out(self) -> int:
        return 16
