"""The common interface every mount driver provides.

Drivers: SynScan WiFi/serial motor protocol (direct), SynScan hand controller
(USB cable), ASCOM (Windows), ASCOM Alpaca (network), and the simulator.
New drivers (INDI, an equatorial wedge, an autoguider) plug in by
implementing this class.
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

SIDEREAL_ARCSEC_S = 15.041
# Manual-move speeds as multiples of the sidereal rate (index 0 = slowest).
MOVE_RATES = [1, 2, 8, 16, 32, 64, 128, 400, 800]
TRACK_RATES = {"sidereal": 15.041, "lunar": 14.685, "solar": 15.0}  # arcsec/s


class MountError(RuntimeError):
    pass


@dataclass
class MountStatus:
    connected: bool = False
    driver: str = ""
    model: str = ""
    ra: float | None = None          # J2000 degrees
    dec: float | None = None
    alt: float | None = None
    az: float | None = None
    slewing: bool = False
    tracking: bool = False
    track_rate: str = "sidereal"
    parked: bool = False
    moving: bool = False
    aligned: bool = False
    alignment_points: int = 0
    target: dict | None = None
    message: str = ""
    error: str = ""
    untested: bool = False
    capabilities: dict = field(default_factory=dict)
    time: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Mount(ABC):
    name = "mount"
    untested_on_hardware = False

    def __init__(self, lat: float, lon: float):
        self.lat, self.lon = lat, lon

    # -- lifecycle ----------------------------------------------------------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    # -- position -------------------------------------------------------------
    @abstractmethod
    def status(self) -> MountStatus: ...

    # -- motion ---------------------------------------------------------------
    @abstractmethod
    def slew_to_radec(self, ra: float, dec: float) -> None:
        """Start a GoTo to J2000 RA/Dec (degrees) and keep tracking it afterwards."""

    def slew_to_altaz(self, alt: float, az: float) -> None:
        raise MountError("This mount cannot slew to alt/az directly")

    @abstractmethod
    def stop(self) -> None:
        """Stop all motion immediately (including tracking)."""

    def sync(self, ra: float, dec: float) -> None:
        raise MountError("This mount does not support sync")

    def move(self, direction: str, rate_index: int) -> None:
        """Start moving: direction in up/down/left/right (alt-az) or N/S/E/W."""
        raise MountError("Manual moves are not supported by this mount")

    def stop_move(self) -> None:
        self.stop()

    def set_tracking(self, on: bool, rate: str = "sidereal") -> None:
        raise MountError("Tracking control is not supported by this mount")

    def park(self) -> None:
        raise MountError("This mount cannot park")

    def unpark(self) -> None:
        pass

    def reset_alignment(self) -> None:
        pass

    def capabilities(self) -> dict:
        return {"sync": True, "altaz_goto": False, "move": False, "tracking": False, "park": False}

    @staticmethod
    def now() -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)
