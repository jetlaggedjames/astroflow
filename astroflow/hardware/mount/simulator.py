"""A simulated SynScan motor controller that speaks the real protocol over UDP.

The real SynScanMotorMount driver talks to this exactly as it would to the
WiFi dongle, so GoTo, tracking, manual moves, STOP and the safety guards are
all exercised end to end. The simulator also knows where the virtual
telescope "really" points (with a deliberate set-up error), which the
simulated camera uses to render the sky - so syncing and plate-solve-and-
centre can be tested for real.
"""
from __future__ import annotations

import logging
import math
import socket
import threading
import time

from .synscan_motor import OFFSET, dec_hex, enc24

log = logging.getLogger("astroflow.mount.sim")


class _Axis:
    def __init__(self, cpr: int, tmr: int, hsr: int, slew_deg_s: float = 4.0):
        self.cpr, self.tmr, self.hsr = cpr, tmr, hsr
        self.slew_deg_s = slew_deg_s
        self.pos = 0.0           # counts
        self.running = False
        self.tracking = True     # mode bit: True = tracking/speed, False = goto
        self.fast = False
        self.ccw = False
        self.slow_goto = False
        self.goto_inc = 0
        self.brake = 3500
        self.goto_target = None
        self.period = 0xFFFFFF
        self.last = time.monotonic()
        self.blocked = False

    def update(self, reverse: bool = False) -> None:
        now = time.monotonic()
        dt = now - self.last
        self.last = now
        if not self.running or dt <= 0:
            return
        sign = -1 if self.ccw else 1
        if reverse:
            sign = -sign
        if self.tracking:
            cps = self.tmr / max(self.period, 1) * (self.hsr if self.fast else 1)
            self.pos += sign * cps * dt
        else:
            speed_deg = self.slew_deg_s / 10 if self.slow_goto else self.slew_deg_s
            step = speed_deg * self.cpr / 360 * dt
            remaining = self.goto_target - self.pos
            if abs(remaining) <= step:
                self.pos = self.goto_target
                self.running = False
                self.tracking = True
            else:
                self.pos += math.copysign(step, remaining) if not reverse else -math.copysign(step, remaining)
                if reverse and abs(self.pos) > 10 * self.cpr:
                    self.running = False


class SimSynScanServer:
    """UDP server on 127.0.0.1:<port>. ``true_offset`` = (az, alt) degrees the 'real' sky
    differs from the mount's idea after power-on, to make syncing meaningful."""

    def __init__(self, port: int = 0, cpr: int = 3628800, tmr: int = 50000, hsr: int = 16,
                 true_offset: tuple[float, float] = (1.7, -0.6), drop_rate: float = 0.0, reverse_axis: int | None = None,
                 slew_deg_s: float = 4.0):
        self.axes = {1: _Axis(cpr, tmr, hsr, slew_deg_s), 2: _Axis(cpr, tmr, hsr, slew_deg_s)}
        self.true_offset = true_offset
        self.drop_rate = drop_rate
        self.reverse_axis = reverse_axis   # fault injection: this motor turns the wrong way
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.commands: list[str] = []
        self._t = threading.Thread(target=self._serve, name="sim-synscan", daemon=True)
        self._t.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    # -- what the virtual telescope really points at -------------------------
    def true_altaz(self) -> tuple[float, float]:
        with self._lock:
            for ax in (1, 2):
                self.axes[ax].update(self.reverse_axis == ax)
            az = self.axes[1].pos * 360 / self.axes[1].cpr
            alt = self.axes[2].pos * 360 / self.axes[2].cpr
        return (az + self.true_offset[0]) % 360, alt + self.true_offset[1]

    # -- protocol --------------------------------------------------------------
    def _serve(self) -> None:
        import random
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(1024)
            except (socket.timeout, OSError):
                continue
            if self.drop_rate and random.random() < self.drop_rate:
                continue
            msg = data.decode("ascii", "replace").strip()
            rep = self.handle(msg)
            try:
                self.sock.sendto(rep.encode("ascii"), addr)
            except OSError:
                pass

    def handle(self, msg: str) -> str:
        if not msg.startswith(":") or len(msg) < 3:
            return "!03\r"
        c, axc, data = msg[1], msg[2], msg[3:]
        self.commands.append(msg)
        del self.commands[:-500]
        if axc not in "123":
            return "!03\r"
        axes = [1, 2] if axc == "3" else [int(axc)]
        with self._lock:
            for a in axes:
                self.axes[a].update(self.reverse_axis == a)
            A = self.axes[axes[0]]
            if c == "e":
                return "=" + enc24(0x0A9F01)[:6] + "\r"   # version, AZ-type board
            if c == "a":
                return "=" + enc24(A.cpr) + "\r"
            if c == "b":
                return "=" + enc24(A.tmr) + "\r"
            if c == "g":
                return "=" + f"{A.hsr:02X}" + "\r"
            if c == "j":
                return "=" + enc24(int(round(A.pos)) + OFFSET) + "\r"
            if c == "f":
                d1 = (1 if A.tracking else 0) | (2 if A.ccw else 0) | (4 if A.fast else 0)
                d2 = (1 if A.running else 0) | (2 if A.blocked else 0)
                return f"={d1:X}{d2:X}1\r"
            if c == "i":
                return "=" + enc24(A.period) + "\r"
            if c == "F":
                return "=\r"
            if c in "KL":
                for a in axes:
                    self.axes[a].running = False
                    self.axes[a].tracking = True
                return "=\r"
            if c == "G":
                if A.running:
                    return "!02\r"
                db1, db2 = int(data[0], 16), int(data[1], 16)
                for a in axes:
                    ax = self.axes[a]
                    ax.tracking = bool(db1 & 1)
                    ax.fast = bool(db1 & 2) if ax.tracking else False
                    ax.slow_goto = (not ax.tracking) and bool(db1 & 2)
                    ax.ccw = bool(db2 & 1)
                return "=\r"
            if c == "H":
                for a in axes:
                    self.axes[a].goto_inc = dec_hex(data)
                return "=\r"
            if c == "S":
                for a in axes:
                    self.axes[a].goto_target = dec_hex(data) - OFFSET
                    self.axes[a].goto_inc = None
                return "=\r"
            if c == "M":
                for a in axes:
                    self.axes[a].brake = dec_hex(data)
                return "=\r"
            if c == "I":
                for a in axes:
                    self.axes[a].period = max(1, dec_hex(data))
                return "=\r"
            if c == "E":
                if A.running:
                    return "!02\r"
                for a in axes:
                    self.axes[a].pos = dec_hex(data) - OFFSET
                return "=\r"
            if c == "J":
                for a in axes:
                    ax = self.axes[a]
                    if not ax.tracking:
                        if ax.goto_inc is not None:
                            ax.goto_target = ax.pos + (-ax.goto_inc if ax.ccw else ax.goto_inc)
                        if ax.goto_target is None:
                            return "!04\r"
                    ax.running = True
                    ax.last = time.monotonic()
                return "=\r"
            if c in "PBOVq":
                return "=000000\r" if c == "q" else "=\r"
        return "!00\r"
