"""ASCOM telescope drivers: classic COM drivers on Windows, and ASCOM Alpaca over the network.

ASCOM is the Windows standard for astronomy devices. Sky-Watcher's "SynScan
App" for Windows comes with an ASCOM driver, so this route lets the official
SynScan app keep doing alignment and tracking while AstroFlow sends GoTo/sync.
Alpaca is the network version of ASCOM (works across computers and
operating systems).

UNTESTED ON REAL HARDWARE. The Alpaca client is tested against a simulated
Alpaca server; the COM path can only run on Windows.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import random
import socket
import sys
import urllib.parse
import urllib.request

from ...astro.coords import date_to_j2000, j2000_to_date
from .base import MOVE_RATES, SIDEREAL_ARCSEC_S, Mount, MountError, MountStatus

log = logging.getLogger("astroflow.mount.ascom")
TRACK_RATE_CODES = {"sidereal": 0, "lunar": 1, "solar": 2}


class _ITelescope(Mount):
    """Shared logic; subclasses implement _get(name) / _put(name, **args)."""

    def _get(self, name: str, **args):
        raise NotImplementedError

    def _put(self, name: str, **args):
        raise NotImplementedError

    def _jnow(self) -> bool:
        try:
            return int(self._get("EquatorialSystem")) != 2   # 2 = J2000; 1 = topocentric (JNow)
        except Exception:
            return True

    def status(self) -> MountStatus:
        st = MountStatus(driver=self.name, connected=self.connected, untested=True, capabilities=self.capabilities())
        if not self.connected:
            return st
        try:
            ra_h, dec = float(self._get("RightAscension")), float(self._get("Declination"))
            ra = ra_h * 15
            if self._jnow():
                ra, dec = date_to_j2000(ra, dec, self.now())
            st.ra, st.dec = float(ra), float(dec)
            try:
                st.alt, st.az = float(self._get("Altitude")), float(self._get("Azimuth"))
            except Exception:
                pass
            st.slewing = bool(self._get("Slewing"))
            st.tracking = bool(self._get("Tracking"))
            try:
                st.parked = bool(self._get("AtPark"))
            except Exception:
                pass
        except Exception as e:
            st.error = str(e)
        st.time = self.now().isoformat()
        return st

    def _to_mount(self, ra: float, dec: float) -> tuple[float, float]:
        if self._jnow():
            ra, dec = j2000_to_date(ra, dec, self.now())
        return float(ra) / 15.0, float(dec)

    def slew_to_radec(self, ra: float, dec: float) -> None:
        rh, d = self._to_mount(ra, dec)
        try:
            if not bool(self._get("Tracking")):
                self._put("Tracking", Tracking=True)
        except Exception:
            pass
        self._put("SlewToCoordinatesAsync", RightAscension=rh, Declination=d)

    def slew_to_altaz(self, alt: float, az: float) -> None:
        self._put("SlewToAltAzAsync", Azimuth=az, Altitude=alt)

    def sync(self, ra: float, dec: float) -> None:
        rh, d = self._to_mount(ra, dec)
        self._put("SyncToCoordinates", RightAscension=rh, Declination=d)

    def stop(self) -> None:
        if not self.connected:
            return
        for call in (lambda: self._put("AbortSlew"), lambda: self._put("MoveAxis", Axis=0, Rate=0.0),
                     lambda: self._put("MoveAxis", Axis=1, Rate=0.0)):
            try:
                call()
            except Exception:
                pass

    def move(self, direction: str, rate_index: int) -> None:
        rate = MOVE_RATES[max(0, min(rate_index, len(MOVE_RATES) - 1))] * SIDEREAL_ARCSEC_S / 3600.0
        d = direction.lower()
        axis = 1 if d in ("up", "down", "n", "s") else 0
        sign = 1 if d in ("up", "n", "right", "e") else -1
        self._put("MoveAxis", Axis=axis, Rate=sign * rate)

    def stop_move(self) -> None:
        for axis in (0, 1):
            self._put("MoveAxis", Axis=axis, Rate=0.0)

    def set_tracking(self, on: bool, rate: str = "sidereal") -> None:
        if on:
            try:
                self._put("TrackingRate", TrackingRate=TRACK_RATE_CODES.get(rate, 0))
            except Exception:
                pass
        self._put("Tracking", Tracking=on)

    def park(self) -> None:
        self._put("Park")

    def unpark(self) -> None:
        self._put("Unpark")

    def capabilities(self) -> dict:
        caps = {"sync": True, "altaz_goto": False, "move": True, "tracking": True, "park": False}
        if self.connected:
            for k, n in (("park", "CanPark"), ("altaz_goto", "CanSlewAltAzAsync"), ("sync", "CanSync")):
                try:
                    caps[k] = bool(self._get(n))
                except Exception:
                    pass
        return caps


class AscomComMount(_ITelescope):
    name = "ASCOM (Windows driver)"
    untested_on_hardware = True

    def __init__(self, lat: float, lon: float, progid: str):
        super().__init__(lat, lon)
        self.progid = progid
        self.t = None

    @staticmethod
    def choose(current: str = "") -> str | None:
        """Open the standard ASCOM chooser dialog (Windows only)."""
        import win32com.client  # type: ignore
        ch = win32com.client.Dispatch("ASCOM.Utilities.Chooser")
        ch.DeviceType = "Telescope"
        return ch.Choose(current) or None

    def connect(self) -> None:
        if not sys.platform.startswith("win"):
            raise MountError("ASCOM drivers only exist on Windows")
        try:
            import win32com.client  # type: ignore
        except ImportError as e:
            raise MountError("The 'pywin32' package is needed for ASCOM") from e
        if not self.progid:
            raise MountError("Choose an ASCOM telescope driver first")
        self.t = win32com.client.Dispatch(self.progid)
        self.t.Connected = True

    def disconnect(self) -> None:
        if self.t is not None:
            try:
                self.t.Connected = False
            except Exception:
                pass
        self.t = None

    @property
    def connected(self) -> bool:
        return self.t is not None

    def _get(self, name: str, **args):
        if args:
            return getattr(self.t, name)(*args.values())
        return getattr(self.t, name)

    def _put(self, name: str, **args):
        attr = getattr(self.t, name)
        if callable(attr):
            return attr(*args.values())
        setattr(self.t, name, next(iter(args.values())))


class AlpacaMount(_ITelescope):
    name = "ASCOM Alpaca (network)"
    untested_on_hardware = True

    def __init__(self, lat: float, lon: float, base_url: str, device: int = 0):
        super().__init__(lat, lon)
        self.base = base_url.rstrip("/") + f"/api/v1/telescope/{int(device)}/"
        self.client_id = random.randint(1, 65535)
        self.tx = 0
        self._connected = False

    def _req(self, method: str, name: str, args: dict) -> object:
        self.tx += 1
        params = {"ClientID": self.client_id, "ClientTransactionID": self.tx}
        url = self.base + name.lower()
        try:
            if method == "GET":
                params.update(args)
                with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=5) as r:
                    d = json.loads(r.read().decode())
            else:
                body = {**params, **{k: (str(v).lower() if isinstance(v, bool) else v) for k, v in args.items()}}
                req = urllib.request.Request(url, data=urllib.parse.urlencode(body).encode(), method="PUT",
                                             headers={"Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.loads(r.read().decode())
        except OSError as e:
            raise MountError(f"Alpaca device not reachable: {e}") from e
        if d.get("ErrorNumber"):
            raise MountError(f"Alpaca error {d['ErrorNumber']}: {d.get('ErrorMessage')}")
        return d.get("Value")

    def _get(self, name: str, **args):
        return self._req("GET", name, args)

    def _put(self, name: str, **args):
        return self._req("PUT", name, args)

    def connect(self) -> None:
        self._put("Connected", Connected=True)
        self._connected = bool(self._get("Connected"))
        if not self._connected:
            raise MountError("The Alpaca telescope did not connect")

    def disconnect(self) -> None:
        try:
            self._put("Connected", Connected=False)
        except MountError:
            pass
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected


def discover_alpaca(timeout: float = 2.0) -> list[dict]:
    """Find Alpaca servers on the local network (UDP discovery, port 32227)."""
    found = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    try:
        s.sendto(b"alpacadiscovery1", ("255.255.255.255", 32227))
        end = dt.datetime.now() + dt.timedelta(seconds=timeout)
        while dt.datetime.now() < end:
            try:
                data, addr = s.recvfrom(1024)
            except socket.timeout:
                break
            try:
                port = json.loads(data.decode()).get("AlpacaPort")
                found.append({"host": addr[0], "port": port, "url": f"http://{addr[0]}:{port}"})
            except ValueError:
                continue
    except OSError as e:
        log.info("Alpaca discovery failed: %s", e)
    finally:
        s.close()
    return found
