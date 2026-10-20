"""SynScan hand controller (HC) protocol - via a USB/serial cable to the hand controller,
or via the network port of the SynScan app (which then does alignment and tracking).

The hand controller keeps its own star alignment and tracking, so this route is
the simplest and most proven if you align with the hand controller first.
Coordinates are J2000, sent as 32-bit fractions of a full turn (as used by INDI's
SynScan driver and SkySafari).

UNTESTED ON REAL HARDWARE (tested against a protocol simulator).
"""
from __future__ import annotations

import logging
import socket
import threading
import time

from ...astro.coords import radec_to_altaz
from .base import MOVE_RATES, SIDEREAL_ARCSEC_S, Mount, MountError, MountStatus

log = logging.getLogger("astroflow.mount.hc")


def _frac(deg: float) -> str:
    return f"{int(round((deg % 360) / 360.0 * 0x100000000)) & 0xFFFFFFFF:08X}"


def _unfrac(h: str) -> float:
    v = int(h, 16)
    if len(h) <= 6:
        v <<= 8
    return v / 0x100000000 * 360.0


class _Link:
    def __init__(self, kind: str, target: str, port: int, timeout: float = 2.0):
        self.kind = kind
        self.lock = threading.Lock()
        if kind == "serial":
            import serial
            self.ser = serial.Serial(target, 9600, timeout=timeout)
        elif kind == "tcp":
            self.sock = socket.create_connection((target, port), timeout=timeout)
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(timeout)
            self.addr = (target, port)

    def ask(self, cmd: bytes, expect_hash: bool = True) -> bytes:
        with self.lock:
            if self.kind == "serial":
                self.ser.reset_input_buffer()
                self.ser.write(cmd)
                out = b""
                while not out.endswith(b"#"):
                    c = self.ser.read(1)
                    if not c:
                        raise MountError(f"Hand controller did not answer {cmd[:1]!r}")
                    out += c
                return out[:-1]
            if self.kind == "tcp":
                self.sock.sendall(cmd)
                out = b""
                while not out.endswith(b"#"):
                    c = self.sock.recv(64)
                    if not c:
                        raise MountError("Connection closed")
                    out += c
                return out[:-1]
            self.sock.sendto(cmd, self.addr)
            data, _ = self.sock.recvfrom(256)
            return data.rstrip(b"#")

    def close(self) -> None:
        try:
            (self.ser if self.kind == "serial" else self.sock).close()
        except Exception:
            pass


class SynScanHCMount(Mount):
    name = "SynScan hand controller"
    untested_on_hardware = True

    def __init__(self, lat: float, lon: float, link: str = "serial", target: str = "", port: int = 11882):
        super().__init__(lat, lon)
        self.link_kind, self.target, self.port = link, target, port
        self.link: _Link | None = None
        self._status = MountStatus(driver=self.name, untested=True)
        self._tracking_rate = "sidereal"

    def connect(self) -> None:
        try:
            self.link = _Link(self.link_kind, self.target, self.port)
            echo = self.link.ask(b"Kx")
        except (OSError, MountError) as e:
            self.link = None
            raise MountError(f"Could not reach the hand controller ({e}). Check the cable/port and that it shows the main menu.")
        if echo != b"x":
            self.link.close()
            self.link = None
            raise MountError("Unexpected answer from the hand controller")
        try:
            aligned = self.link.ask(b"J") in (b"\x01", b"1")
        except MountError:
            aligned = False
        self._status.aligned = aligned
        self._status.connected = True
        if not aligned:
            self._status.message = "The hand controller is not aligned yet: do its star alignment first (or sync here)."

    def disconnect(self) -> None:
        if self.link:
            self.link.close()
        self.link = None
        self._status.connected = False

    @property
    def connected(self) -> bool:
        return self.link is not None

    def capabilities(self) -> dict:
        return {"sync": True, "altaz_goto": True, "move": True, "tracking": True, "park": False}

    def _l(self) -> _Link:
        if not self.link:
            raise MountError("Mount is not connected")
        return self.link

    def status(self) -> MountStatus:
        st = self._status
        if self.link:
            try:
                r = self._l().ask(b"e").decode()
                a, b = r.split(",")
                ra, dec = _unfrac(a), _unfrac(b)
                dec = dec - 360 if dec > 180 else dec
                st.ra, st.dec = ra, dec
                alt, az = radec_to_altaz(ra, dec, self.now(), self.lat, self.lon)
                st.alt, st.az = float(alt), float(az)
                st.slewing = self._l().ask(b"L") in (b"1", b"\x01")
                t = self._l().ask(b"t")
                st.tracking = bool(t and t[0] not in (0, 48))
                st.error = ""
            except (MountError, OSError, ValueError) as e:
                st.error = str(e)
        st.capabilities = self.capabilities()
        st.time = self.now().isoformat()
        return MountStatus(**st.to_dict())

    def slew_to_radec(self, ra: float, dec: float) -> None:
        r = self._l().ask(f"r{_frac(ra)},{_frac(dec)}".encode())
        if r not in (b"",):
            log.debug("goto reply %r", r)

    def slew_to_altaz(self, alt: float, az: float) -> None:
        self._l().ask(f"b{_frac(az)},{_frac(alt)}".encode())

    def sync(self, ra: float, dec: float) -> None:
        self._l().ask(f"s{_frac(ra)},{_frac(dec)}".encode())

    def stop(self) -> None:
        if not self.link:
            return
        try:
            self._l().ask(b"M")
            for axis in (16, 17):
                self._l().ask(bytes([ord("P"), 2, axis, 36, 0, 0, 0, 0]))
        except MountError:
            pass

    def move(self, direction: str, rate_index: int) -> None:
        rate = MOVE_RATES[max(0, min(rate_index, len(MOVE_RATES) - 1))] * SIDEREAL_ARCSEC_S
        d = direction.lower()
        axis = 17 if d in ("up", "down", "n", "s") else 16
        positive = d in ("up", "n", "right", "e")
        v = int(round(rate * 4))
        self._l().ask(bytes([ord("P"), 3, axis, 6 if positive else 7, (v >> 8) & 0xFF, v & 0xFF, 0, 0]))

    def stop_move(self) -> None:
        for axis in (16, 17):
            self._l().ask(bytes([ord("P"), 3, axis, 6, 0, 0, 0, 0]))

    def set_tracking(self, on: bool, rate: str = "sidereal") -> None:
        # 0 = off, 1 = alt-az tracking (the right mode for a Dobsonian)
        self._l().ask(bytes([ord("T"), 1 if on else 0]))


class SimHCServer:
    """Tiny TCP simulator of the hand-controller protocol (for tests)."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.ra, self.dec = 0.0, 0.0
        self.goto_until = 0.0
        self.tracking = 0
        self.log: list[bytes] = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        buf = b""
        while True:
            try:
                d = conn.recv(64)
            except OSError:
                return
            if not d:
                return
            buf += d
            while buf:
                c = buf[:1]
                n = {b"K": 2, b"e": 1, b"z": 1, b"L": 1, b"M": 1, b"J": 1, b"t": 1, b"T": 2, b"P": 8, b"V": 1,
                     b"r": 18, b"s": 18, b"b": 18}.get(c, 1)
                if len(buf) < n:
                    break
                cmd, buf = buf[:n], buf[n:]
                self.log.append(cmd)
                conn.sendall(self._reply(cmd))

    def _reply(self, cmd: bytes) -> bytes:
        c = cmd[:1]
        if c == b"K":
            return cmd[1:2] + b"#"
        if c == b"e":
            return f"{_frac(self.ra)},{_frac(self.dec)}#".encode()
        if c in (b"r", b"s"):
            a, b = cmd[1:].decode().split(",")
            self.ra, self.dec = _unfrac(a), _unfrac(b)
            if self.dec > 180:
                self.dec -= 360
            if c == b"r":
                self.goto_until = time.time() + 1.0
                self.tracking = 1
            return b"#"
        if c == b"L":
            return (b"1" if time.time() < self.goto_until else b"0") + b"#"
        if c == b"J":
            return b"1#"
        if c == b"t":
            return bytes([self.tracking]) + b"#"
        if c == b"T":
            self.tracking = cmd[1]
            return b"#"
        return b"#"

    def close(self):
        self.srv.close()
