"""Sky-Watcher SynScan mounts via the official motor-controller protocol.

Verified from Sky-Watcher's "Motor Controller Command Set" document:
  * Commands ":<letter><axis><data>\\r", replies "=<data>\\r" or "!<error>\\r".
  * Axis 1 = azimuth (RA), axis 2 = altitude (Dec).
  * 24-bit numbers are sent low byte first ("563412" = 0x123456); positions
    are offset by 0x800000.
  * WiFi dongle / built-in WiFi: a UDP server on port 11880, one command
    per packet; in access-point mode its address is 192.168.4.1.
  * The same protocol works over a serial cable to the motor controller,
    or through a SynScan hand controller in "PC Direct" mode.

The motor controller only counts steps: it doesn't know where the sky is.
So this driver does the pointing maths itself:
  * "Home" = the standard SynScan Dob start position: tube level, pointing north.
  * Every sync (on a star, planet or a plate-solve) refines an alignment model
    (offsets; with 3+ points also the tilt of a not-quite-level base).
  * Alt-az tracking: both motors are continuously re-timed to follow the sky.

UNTESTED ON REAL HARDWARE: built and tested against a protocol simulator.
Direction conventions in particular must be confirmed on the real mount
(see HARDWARE_CHECKLIST.md). A runaway guard stops any GoTo that moves
away from its target.
"""
from __future__ import annotations

import logging
import math
import select
import socket
import threading
import time

import numpy as np

from ...astro.coords import altaz_to_radec, radec_to_altaz
from .base import MOVE_RATES, SIDEREAL_ARCSEC_S, TRACK_RATES, Mount, MountError, MountStatus

log = logging.getLogger("astroflow.mount.synscan")

ERRORS = {0: "Unknown command", 1: "Command length error", 2: "Motor not stopped", 3: "Invalid character",
          4: "Not initialised", 5: "Driver sleeping", 7: "PEC training running", 8: "No valid PEC data"}
OFFSET = 0x800000


def enc24(v: int) -> str:
    v &= 0xFFFFFF
    s = f"{v:06X}"
    return s[4:6] + s[2:4] + s[0:2]


def dec_hex(s: str) -> int:
    """Little-endian hex (2, 4 or 6 digits) -> int."""
    s = s.strip()
    if len(s) % 2:
        return int(s, 16)
    b = [s[i:i + 2] for i in range(0, len(s), 2)]
    return int("".join(reversed(b)), 16)


# ------------------------------------------------------------- transports ---

class UdpTransport:
    def __init__(self, host: str = "192.168.4.1", port: int = 11880, timeout: float = 0.35):
        self.addr = (host, int(port))
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def exchange(self, msg: bytes) -> bytes:
        # Drop stale replies (from a timed-out earlier request) first.
        while True:
            r, _, _ = select.select([self.sock], [], [], 0)
            if not r:
                break
            try:
                self.sock.recvfrom(1024)
            except OSError:
                break
        self.sock.sendto(msg, self.addr)
        r, _, _ = select.select([self.sock], [], [], self.timeout)
        if not r:
            raise TimeoutError("no reply")
        data, _ = self.sock.recvfrom(1024)
        return data

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class SerialTransport:
    def __init__(self, port: str, baud: int = 9600, timeout: float = 0.5):
        import serial  # pyserial
        self.ser = serial.Serial(port, baud, timeout=timeout)

    def exchange(self, msg: bytes) -> bytes:
        self.ser.reset_input_buffer()
        self.ser.write(msg)
        out = b""
        t0 = time.time()
        while time.time() - t0 < self.ser.timeout * 2:
            c = self.ser.read(1)
            if not c:
                break
            out += c
            if c == b"\r" and (out.startswith(b"=") or out.startswith(b"!") or b"=" in out or b"!" in out):
                break
        # Some adapters echo the command: keep only the reply.
        for lead in (b"=", b"!"):
            i = out.find(lead)
            if i >= 0:
                return out[i:]
        raise TimeoutError("no reply")

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


class MotorProtocol:
    def __init__(self, transport, retries: int = 6):
        self.t = transport
        self.retries = retries
        self.lock = threading.Lock()
        self.failures = 0

    def cmd(self, letter: str, axis: int, data: str = "") -> str:
        msg = f":{letter}{axis}{data}\r".encode("ascii")
        last: Exception | None = None
        with self.lock:
            for _ in range(self.retries):
                try:
                    rep = self.t.exchange(msg).decode("ascii", "replace").strip()
                except (TimeoutError, OSError) as e:
                    last = e
                    continue
                self.failures = 0
                if rep.startswith("="):
                    return rep[1:]
                if rep.startswith("!"):
                    code = int(rep[1:3] or "0", 16) if rep[1:3].strip() else 0
                    raise MountError(f"Mount refused :{letter}{axis} - {ERRORS.get(code, code)}")
                last = MountError(f"Unexpected reply {rep!r}")
        self.failures += 1
        raise MountError(f"Mount not responding to :{letter}{axis} ({last})")


# -------------------------------------------------------- alignment model ---

class AltAzModel:
    """mount angles = true angles + offsets (+ base tilt with 3+ points)."""

    def __init__(self):
        self.points: list[tuple[float, float, float, float]] = []  # (true_az, true_alt, mount_az, mount_alt)
        self.dA = self.dE = self.tx = self.ty = 0.0

    def to_mount(self, az: float, alt: float) -> tuple[float, float]:
        a = math.radians(az)
        e = alt + self.dE + self.tx * math.sin(a) + self.ty * math.cos(a)
        A = az + self.dA - (self.tx * math.cos(a) - self.ty * math.sin(a)) * math.tan(math.radians(min(alt, 85)))
        return A, e

    def to_true(self, maz: float, malt: float) -> tuple[float, float]:
        az, alt = maz - self.dA, malt - self.dE
        for _ in range(5):  # invert by fixed-point iteration
            A, E = self.to_mount(az, alt)
            az -= _wrap(A - maz)
            alt -= E - malt
        return az % 360, alt

    def add_point(self, true_az: float, true_alt: float, mount_az: float, mount_alt: float) -> None:
        self.points.append((true_az, true_alt, mount_az, mount_alt))
        self.points = self.points[-12:]
        self.fit()

    def fit(self) -> None:
        P = self.points
        if not P:
            self.dA = self.dE = self.tx = self.ty = 0.0
            return
        if len(P) < 3:
            # Offsets only (latest point weighted most when two points disagree).
            w = np.linspace(0.5, 1.0, len(P))
            self.dA = float(np.average([_wrap(m - t) for t, _, m, _ in P], weights=w))
            self.dE = float(np.average([me - te for _, te, _, me in P], weights=w))
            self.tx = self.ty = 0.0
            return
        rows, rhs = [], []
        for ta, te, ma, me in P:
            a = math.radians(ta)
            tn = math.tan(math.radians(min(te, 85)))
            rows.append([1, 0, -math.cos(a) * tn, math.sin(a) * tn]); rhs.append(_wrap(ma - ta))
            rows.append([0, 1, math.sin(a), math.cos(a)]); rhs.append(me - te)
        sol, *_ = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)
        self.dA, self.dE, self.tx, self.ty = (float(v) for v in sol)

    def rms_arcmin(self) -> float:
        if not self.points:
            return 0.0
        err = []
        for ta, te, ma, me in self.points:
            A, E = self.to_mount(ta, te)
            err.append(math.hypot(_wrap(A - ma) * math.cos(math.radians(te)), E - me))
        return float(np.sqrt(np.mean(np.square(err))) * 60)

    def to_dict(self) -> dict:
        return {"points": self.points}

    def load(self, d: dict) -> None:
        self.points = [tuple(p) for p in d.get("points", [])]
        self.fit()


def _wrap(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


# ------------------------------------------------------------------ mount ---

class SynScanMotorMount(Mount):
    name = "SynScan (direct motor protocol)"
    untested_on_hardware = True

    def __init__(self, lat: float, lon: float, transport: str = "udp", host: str = "192.168.4.1", port: int = 11880,
                 serial_port: str = "", reverse_az: bool = False, reverse_alt: bool = False,
                 home_az: float = 0.0, home_alt: float = 0.0, model: dict | None = None):
        super().__init__(lat, lon)
        self.transport_kind, self.host, self.port, self.serial_port = transport, host, port, serial_port
        self.rev = {1: -1 if reverse_az else 1, 2: -1 if reverse_alt else 1}
        self.home = {1: home_az, 2: home_alt}
        self.proto: MotorProtocol | None = None
        self.cpr = {1: 0, 2: 0}
        self.tmr = {1: 0, 2: 0}
        self.hsr = {1: 16, 2: 16}
        self.version = ""
        self.model = AltAzModel()
        if model:
            self.model.load(model)
        self._lock = threading.RLock()
        self._status = MountStatus(driver=self.name, untested=True)
        self._target: dict | None = None      # {"ra","dec"} being tracked
        self._tracking = False
        self._track_rate = "sidereal"
        self._goto_thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._stop = threading.Event()
        self._mon: threading.Thread | None = None
        self._axis_mode = {1: None, 2: None}  # (tracking, ccw, fast) currently programmed
        self._moving = False
        self.on_direction_problem = None      # callback(axis) when a runaway is detected

    # -- connection ----------------------------------------------------------
    def connect(self) -> None:
        if self.transport_kind == "serial":
            if not self.serial_port:
                raise MountError("Choose the serial (COM) port of the cable")
            t = SerialTransport(self.serial_port)
        else:
            t = UdpTransport(self.host, self.port)
        self.proto = MotorProtocol(t)
        try:
            ver = self.proto.cmd("e", 1)
            self.version = ver
            for ax in (1, 2):
                self.cpr[ax] = dec_hex(self.proto.cmd("a", ax))
                self.tmr[ax] = dec_hex(self.proto.cmd("b", ax))
                try:
                    self.hsr[ax] = dec_hex(self.proto.cmd("g", ax)) or 16
                except MountError:
                    self.hsr[ax] = 16
                self.proto.cmd("F", ax)  # "initialisation done"
        except MountError:
            t.close()
            self.proto = None
            raise MountError(f"No answer from the mount at {self.host}:{self.port}. Is your PC joined to the "
                             "SynScan WiFi network, and is the SynScan phone app closed?" if self.transport_kind == "udp"
                             else f"No answer from the mount on {self.serial_port}.")
        if not self.cpr[1] or not self.cpr[2]:
            raise MountError("Mount reported invalid gear data")
        self._stop.clear()
        self._first_status = threading.Event()
        self._mon = threading.Thread(target=self._monitor, name="mount-monitor", daemon=True)
        self._mon.start()
        self._first_status.wait(3.0)      # so the status reported right after connecting is already current
        log.info("SynScan connected: version %s CPR %s TMR %s HSR %s", self.version, self.cpr, self.tmr, self.hsr)

    def disconnect(self) -> None:
        self._stop.set()
        self._abort.set()
        try:
            if self.proto:
                for ax in (1, 2):
                    self.proto.cmd("K", ax)
        except MountError:
            pass
        if self.proto:
            self.proto.t.close()
        self.proto = None
        self._status.connected = False

    @property
    def connected(self) -> bool:
        return self.proto is not None

    def capabilities(self) -> dict:
        return {"sync": True, "altaz_goto": True, "move": True, "tracking": True, "park": True, "set_home": True}

    # -- low level helpers ---------------------------------------------------
    def _p(self) -> MotorProtocol:
        if self.proto is None:
            raise MountError("Mount is not connected")
        return self.proto

    def _counts(self, ax: int) -> int:
        return dec_hex(self._p().cmd("j", ax)) - OFFSET

    def _deg(self, ax: int) -> float:
        """Mount angle of an axis in degrees (relative to home)."""
        return self.rev[ax] * self._counts(ax) * 360.0 / self.cpr[ax] + self.home[ax]

    def _axis_status(self, ax: int) -> dict:
        s = self._p().cmd("f", ax)
        if len(s) < 3:
            return {"running": False, "tracking": False, "init": True}
        d1, d2, d3 = (int(c, 16) for c in s[:3])
        return {"tracking": bool(d1 & 1), "ccw": bool(d1 & 2), "fast": bool(d1 & 4),
                "running": bool(d2 & 1), "blocked": bool(d2 & 2), "init": bool(d3 & 1)}

    def _stop_axis(self, ax: int, wait: bool = True, instant: bool = False) -> None:
        self._p().cmd("L" if instant else "K", ax)
        self._axis_mode[ax] = None
        if wait:
            t0 = time.time()
            while time.time() - t0 < 10:
                if not self._axis_status(ax)["running"]:
                    return
                time.sleep(0.1)
            self._p().cmd("L", ax)

    def _goto_axis(self, ax: int, delta_deg: float) -> None:
        """Relative GoTo of one axis by ``delta_deg`` (mount degrees)."""
        counts = int(round(abs(delta_deg) * self.cpr[ax] / 360.0))
        if counts < 2:
            return
        positive = (delta_deg > 0) == (self.rev[ax] > 0)
        slow = abs(delta_deg) < 1.0
        self._stop_axis(ax)
        db1 = "2" if slow else "0"          # goto, fast/slow
        db2 = "0" if positive else "1"       # CW = counts up, CCW = counts down
        p = self._p()
        p.cmd("G", ax, db1 + db2)
        p.cmd("H", ax, enc24(counts))
        p.cmd("M", ax, enc24(min(3500, max(counts // 2, 1))))
        p.cmd("J", ax)

    def _set_rate(self, ax: int, deg_per_s: float) -> None:
        """Run an axis continuously at a signed speed (mount degrees / second)."""
        speed = deg_per_s * self.rev[ax]
        if abs(speed) < 1e-7:
            if self._axis_mode[ax] is not None:
                self._stop_axis(ax, wait=False)
            return
        ccw = speed < 0
        fast = abs(speed) * 3600 > 128 * SIDEREAL_ARCSEC_S
        cps = abs(speed) * self.cpr[ax] / 360.0
        period = self.tmr[ax] / cps
        if fast:
            period *= self.hsr[ax]
        period = int(min(max(period, 6), 0xFFFFFF))
        mode = (True, ccw, fast)
        p = self._p()
        if self._axis_mode[ax] != mode:
            self._stop_axis(ax)
            p.cmd("G", ax, ("3" if fast else "1") + ("1" if ccw else "0"))
            p.cmd("I", ax, enc24(period))
            p.cmd("J", ax)
            self._axis_mode[ax] = mode
        else:
            p.cmd("I", ax, enc24(period))  # changing the period on the fly is allowed at low speed

    # -- coordinates ---------------------------------------------------------
    def _true_altaz(self) -> tuple[float, float]:
        maz, malt = self._deg(1), self._deg(2)
        return self.model.to_true(maz, malt)

    def status(self) -> MountStatus:
        with self._lock:
            return MountStatus(**{**self._status.to_dict()})

    def _monitor(self) -> None:
        last_track = 0.0
        while not self._stop.is_set():
            try:
                az, alt = self._true_altaz()
                t = self.now()
                ra, dec = altaz_to_radec(alt, az, t, self.lat, self.lon)
                s1, s2 = self._axis_status(1), self._axis_status(2)
                with self._lock:
                    st = self._status
                    st.connected, st.alt, st.az, st.ra, st.dec = True, alt, az, float(ra), float(dec)
                    st.slewing = bool(self._goto_thread and self._goto_thread.is_alive())
                    st.tracking = self._tracking
                    st.track_rate = self._track_rate
                    st.moving = self._moving
                    st.aligned = len(self.model.points) > 0
                    st.alignment_points = len(self.model.points)
                    st.target = self._target
                    st.model = f"SynScan motor controller {self.version}"
                    st.capabilities = self.capabilities()
                    st.time = t.isoformat()
                    st.error = "" if not (s1.get("blocked") or s2.get("blocked")) else "A motor reports BLOCKED - check for obstructions"
                getattr(self, "_first_status", threading.Event()).set()
                if self._tracking and not st.slewing and not self._moving and time.time() - last_track > 1.0:
                    self._track_step()
                    last_track = time.time()
            except MountError as e:
                with self._lock:
                    self._status.error = str(e)
                    if self.proto and self.proto.failures >= 3:
                        self._status.message = "Lost contact with the mount. Check the WiFi connection."
            except Exception as e:  # never let the monitor die
                log.exception("mount monitor: %s", e)
            self._stop.wait(0.5)

    # -- tracking -----------------------------------------------------------
    def _track_step(self) -> None:
        tgt = self._target
        if not tgt:
            return
        t = self.now()
        rate_scale = TRACK_RATES.get(self._track_rate, 15.041) / 15.041
        dt_s = 2.0
        if tgt.get("altaz_fixed"):
            for ax in (1, 2):
                self._set_rate(ax, 0.0)
            return
        a0, z0 = radec_to_altaz(tgt["ra"], tgt["dec"], t, self.lat, self.lon)
        a1, z1 = radec_to_altaz(tgt["ra"], tgt["dec"], t + _td(dt_s * rate_scale), self.lat, self.lon)
        m0 = self.model.to_mount(float(z0), float(a0))
        m1 = self.model.to_mount(float(z1), float(a1))
        cur = (self._deg(1), self._deg(2))
        for ax, i in ((1, 0), (2, 1)):
            rate = (_wrap(m1[i] - m0[i]) if ax == 1 else (m1[i] - m0[i])) / dt_s
            err = _wrap(m0[i] - cur[i]) if ax == 1 else (m0[i] - cur[i])
            if abs(err) > 2.0:
                # Far off (e.g. after a manual nudge): don't chase at tracking speed, re-aim.
                self._set_rate(ax, rate)
                continue
            corr = max(-0.02, min(0.02, err / 5.0))   # close the gap over ~5 s, bounded
            self._set_rate(ax, rate + corr)

    def set_tracking(self, on: bool, rate: str = "sidereal") -> None:
        self._track_rate = rate if rate in TRACK_RATES else "sidereal"
        if on:
            if not self._target:
                az, alt = self._true_altaz()
                ra, dec = altaz_to_radec(alt, az, self.now(), self.lat, self.lon)
                self._target = {"ra": float(ra), "dec": float(dec)}
            self._tracking = True
            self._track_step()
        else:
            self._tracking = False
            for ax in (1, 2):
                self._stop_axis(ax, wait=False)

    # -- GoTo ----------------------------------------------------------------
    def slew_to_radec(self, ra: float, dec: float, track: bool = True) -> None:
        self._start_goto({"ra": ra, "dec": dec}, track)

    def slew_to_altaz(self, alt: float, az: float) -> None:
        self._start_goto({"alt": alt, "az": az, "altaz_fixed": True}, track=False)

    def _start_goto(self, tgt: dict, track: bool) -> None:
        if self._goto_thread and self._goto_thread.is_alive():
            self.stop()
            self._goto_thread.join(5)
        self._abort.clear()
        self._tracking = False
        self._goto_thread = threading.Thread(target=self._goto, args=(tgt, track), name="mount-goto", daemon=True)
        self._goto_thread.start()

    def _target_mount_angles(self, tgt: dict, when) -> tuple[float, float, float]:
        if tgt.get("altaz_fixed"):
            alt, az = tgt["alt"], tgt["az"]
        else:
            alt, az = radec_to_altaz(tgt["ra"], tgt["dec"], when, self.lat, self.lon)
        A, E = self.model.to_mount(float(az), float(alt))
        return A, E, float(alt)

    def _goto(self, tgt: dict, track: bool) -> None:
        try:
            for ax in (1, 2):
                self._stop_axis(ax)
            for attempt in range(3):
                if self._abort.is_set():
                    return
                cur = {1: self._deg(1), 2: self._deg(2)}
                A, E, _ = self._target_mount_angles(tgt, self.now() + _td(5))
                dA = _wrap(A - cur[1])
                # Keep the azimuth within +-270 deg of home so cables don't wind up.
                if abs(cur[1] + dA - self.home[1]) > 270:
                    dA = dA - 360 if dA > 0 else dA + 360
                dE = E - cur[2]
                if abs(dA) < 0.01 and abs(dE) < 0.01:
                    break
                self._goto_axis(1, dA)
                self._goto_axis(2, dE)
                if not self._wait_goto({1: cur[1] + dA, 2: cur[2] + dE}):
                    return
            with self._lock:
                self._status.message = "Arrived at target"
            if track and not tgt.get("altaz_fixed"):
                self._target = {"ra": tgt["ra"], "dec": tgt["dec"]}
                self._tracking = True
            else:
                self._target = tgt if tgt.get("altaz_fixed") else None
        except MountError as e:
            with self._lock:
                self._status.error = str(e)
            log.error("GoTo failed: %s", e)

    def _wait_goto(self, goal: dict[int, float], timeout: float = 240.0) -> bool:
        """Wait until both axes stop; abort on STOP or if an axis moves away from its goal."""
        t0 = time.time()
        start = {ax: abs(goal[ax] - self._deg(ax)) for ax in (1, 2)}
        worst = dict(start)
        misses = 0
        while time.time() - t0 < timeout:
            if self._abort.is_set():
                return False
            time.sleep(0.25)
            running = False
            try:
                states = {ax: (self._axis_status(ax), self._deg(ax)) for ax in (1, 2)}
                misses = 0
            except MountError:
                # A lost WiFi packet: keep watching unless contact is really gone.
                misses += 1
                if misses >= 8:
                    self.stop()
                    with self._lock:
                        self._status.error = "Lost contact with the mount during GoTo - motors stopped"
                    return False
                continue
            for ax in (1, 2):
                st, pos = states[ax]
                running |= st["running"]
                dist = abs(goal[ax] - pos)
                worst[ax] = min(worst[ax], dist)
                # Runaway guard: getting clearly further away than at the start -> stop now.
                if dist > start[ax] + 1.0 and dist > worst[ax] + 1.0:
                    self.stop()
                    msg = (f"Safety stop: the {'azimuth' if ax == 1 else 'altitude'} axis moved AWAY from the target. "
                           "The motor direction setting is probably reversed - see Mount settings.")
                    with self._lock:
                        self._status.error = msg
                    log.error(msg)
                    if self.on_direction_problem:
                        self.on_direction_problem(ax)
                    return False
            if not running:
                return True
        self.stop()
        with self._lock:
            self._status.error = "GoTo took too long and was stopped"
        return False

    # -- manual moves --------------------------------------------------------
    def move(self, direction: str, rate_index: int) -> None:
        rate = MOVE_RATES[max(0, min(rate_index, len(MOVE_RATES) - 1))] * SIDEREAL_ARCSEC_S / 3600.0
        d = direction.lower()
        axis, sign = {"up": (2, 1), "n": (2, 1), "down": (2, -1), "s": (2, -1),
                      "right": (1, 1), "e": (1, 1), "left": (1, -1), "w": (1, -1)}.get(d, (None, 0))
        if axis is None:
            raise MountError(f"Unknown direction {direction}")
        self._abort.set()
        self._moving = True
        self._tracking_before_move = self._tracking
        self._tracking = False
        self._set_rate(axis, sign * rate)
        other = 1 if axis == 2 else 2
        self._stop_axis(other, wait=False)

    def stop_move(self) -> None:
        for ax in (1, 2):
            self._stop_axis(ax, wait=False)
        self._moving = False
        if getattr(self, "_tracking_before_move", False):
            # Resume tracking wherever the telescope now points.
            self._target = None
            self.set_tracking(True, self._track_rate)

    def stop(self) -> None:
        self._abort.set()
        self._tracking = False
        self._moving = False
        if self.proto:
            for ax in (1, 2):
                try:
                    self._p().cmd("L", ax)  # instant stop
                except MountError:
                    pass
                self._axis_mode[ax] = None
        with self._lock:
            self._status.message = "Stopped"

    # -- alignment -------------------------------------------------------------
    def sync(self, ra: float, dec: float) -> None:
        """'The telescope is really pointing at ra/dec' - adds an alignment point."""
        alt, az = radec_to_altaz(ra, dec, self.now(), self.lat, self.lon)
        self.model.add_point(float(az), float(alt), self._deg(1), self._deg(2))
        if self._target and self._tracking:
            self._target = {"ra": ra, "dec": dec}

    def reset_alignment(self) -> None:
        self.model = AltAzModel()

    def set_home_here(self) -> None:
        """Declare the current position as home (tube level, pointing north)."""
        for ax in (1, 2):
            self.home[ax] = self.home[ax] - self._deg(ax)
        self.model = AltAzModel()

    def park(self) -> None:
        """Return to home (level, pointing north) and stop."""
        self._abort.clear()
        self._tracking = False
        self._target = None

        def run():
            cur = {1: self._deg(1), 2: self._deg(2)}
            dA = self.home[1] - cur[1]
            dE = self.home[2] - cur[2]
            self._goto_axis(1, dA)
            self._goto_axis(2, dE)
            self._wait_goto({1: self.home[1], 2: self.home[2]})
            with self._lock:
                self._status.parked = True
        self._goto_thread = threading.Thread(target=run, name="mount-park", daemon=True)
        self._goto_thread.start()

    def unpark(self) -> None:
        with self._lock:
            self._status.parked = False


def _td(seconds: float):
    import datetime as dt
    return dt.timedelta(seconds=seconds)
