"""Safe mount control used by the Mount tab.

Safety rules (enforced here, for every driver):
  * Nothing moves unless you asked: there are no automatic slews.
  * Targets below your altitude limit are refused.
  * Large or unusual moves need a confirmation that says where the tube will go.
  * The arrow buttons work as a "dead-man switch": motion stops by itself if the
    app stops hearing that the button is still held (e.g. a dropped connection).
  * STOP always works, connected or not, and is logged.
  * Every command sent is logged with its time.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import secrets
import threading
import time
from typing import Callable

from ...astro.coords import angular_separation, radec_to_altaz
from ...config import settings
from .base import Mount, MountError, MountStatus

log = logging.getLogger("astroflow.mount")

DRIVERS = {
    "none": "No mount",
    "simulator": "Simulator (for practice and testing)",
    "synscan_wifi": "SynScan WiFi - direct (no extra software)",
    "synscan_serial": "SynScan - USB/serial cable to the motor controller or HC in PC-Direct mode",
    "synscan_hc": "SynScan hand controller (USB cable)",
    "synscan_app": "SynScan app on this PC/phone (network)",
    "ascom": "ASCOM driver (Windows, e.g. SynScan App driver)",
    "alpaca": "ASCOM Alpaca (network)",
}


class MountController:
    def __init__(self):
        self.mount: Mount | None = None
        self.driver = "none"
        self.sim_server = None
        self.log: list[dict] = []
        self._pending: dict[str, dict] = {}
        self._move_deadline = 0.0
        self._move_lock = threading.Lock()
        self._watch = threading.Thread(target=self._watchdog, name="mount-watchdog", daemon=True)
        self._watch.start()
        self.warnings: list[str] = []

    # ------------------------------------------------------------- helpers
    def _note(self, action: str, detail: str = "", source: str = "user") -> None:
        e = {"time": dt.datetime.now().strftime("%H:%M:%S"), "action": action, "detail": detail, "source": source}
        self.log.append(e)
        del self.log[:-300]
        log.info("MOUNT %s %s", action, detail)

    def _loc(self) -> tuple[float, float]:
        s = settings()
        lat, lon = s.get("location.lat"), s.get("location.lon")
        if lat is None or lon is None:
            raise MountError("Set your observing location first (Settings or the setup wizard)")
        return float(lat), float(lon)

    def _need(self) -> Mount:
        if not self.mount or not self.mount.connected:
            raise MountError("The mount is not connected")
        return self.mount

    # ---------------------------------------------------------- connection
    def connect(self, driver: str | None = None) -> MountStatus:
        s = settings()
        driver = driver or s.get("mount.driver", "none")
        if self.mount and self.mount.connected:
            self.disconnect()
        lat, lon = self._loc()
        m = s.get("mount")
        mount: Mount
        if driver == "simulator":
            from .simulator import SimSynScanServer
            from .synscan_motor import SynScanMotorMount
            self.sim_server = SimSynScanServer()
            mount = SynScanMotorMount(lat, lon, "udp", "127.0.0.1", self.sim_server.port)
        elif driver in ("synscan_wifi", "synscan_serial"):
            from .synscan_motor import SynScanMotorMount
            mount = SynScanMotorMount(lat, lon, "serial" if driver == "synscan_serial" else "udp", m.get("host", "192.168.4.1"),
                                      int(m.get("port", 11880)), m.get("serial_port", ""), bool(m.get("reverse_az")),
                                      bool(m.get("reverse_alt")), model=m.get("alignment"))
        elif driver == "synscan_hc":
            from .synscan_hc import SynScanHCMount
            mount = SynScanHCMount(lat, lon, "serial", m.get("serial_port", ""))
        elif driver == "synscan_app":
            from .synscan_hc import SynScanHCMount
            mount = SynScanHCMount(lat, lon, "tcp", m.get("app_host", "127.0.0.1"), int(m.get("app_port", 11882)))
        elif driver == "ascom":
            from .ascom import AscomComMount
            mount = AscomComMount(lat, lon, m.get("ascom_progid", ""))
        elif driver == "alpaca":
            from .ascom import AlpacaMount
            mount = AlpacaMount(lat, lon, m.get("alpaca_url", "http://127.0.0.1:11111"), int(m.get("alpaca_device", 0)))
        else:
            raise MountError("Choose a mount connection method first")
        mount.connect()
        if hasattr(mount, "on_direction_problem"):
            mount.on_direction_problem = self._direction_problem
        self.mount, self.driver = mount, driver
        self._note("connect", DRIVERS.get(driver, driver))
        return mount.status()

    def disconnect(self) -> None:
        if self.mount:
            try:
                self.mount.stop()
            except Exception:
                pass
            self.mount.disconnect()
            self._note("disconnect")
        self.mount = None
        if self.sim_server:
            self.sim_server.close()
            self.sim_server = None

    def _direction_problem(self, axis: int) -> None:
        name = "azimuth" if axis == 1 else "altitude"
        self.warnings.append(f"The {name} motor turned the wrong way and was stopped. Tick 'Reverse {name} direction' "
                             "in Mount settings, then try again.")

    # ------------------------------------------------------------- status
    def status(self) -> dict:
        s = settings()
        st = self.mount.status().to_dict() if self.mount else MountStatus().to_dict()
        st.update({"driver_id": self.driver, "drivers": DRIVERS, "alt_limit": float(s.get("mount.alt_limit_deg", 10)),
                   "log": self.log[-40:], "warnings": self.warnings[-5:],
                   "untested_on_hardware": bool(self.mount and self.mount.untested_on_hardware and self.driver != "simulator")})
        return st

    # ------------------------------------------------------------- motion
    def stop(self, source: str = "user") -> None:
        """STOP: always allowed, never raises."""
        with self._move_lock:
            self._move_deadline = 0.0
        self._note("STOP", source=source)
        if self.mount:
            try:
                self.mount.stop()
            except Exception as e:
                log.error("stop failed: %s", e)

    def goto(self, ra: float, dec: float, label: str = "", confirm: str | None = None) -> dict:
        """Start a GoTo after safety checks. Returns {"started": bool, ...} or a confirmation request."""
        m = self._need()
        s = settings()
        lat, lon = self._loc()
        now = dt.datetime.now(dt.timezone.utc)
        alt, az = radec_to_altaz(ra, dec, now, lat, lon)
        alt, az = float(alt), float(az)
        limit = float(s.get("mount.alt_limit_deg", 10))
        if alt < limit:
            return {"started": False, "refused": True,
                    "message": f"{label or 'That target'} is {alt:.0f} deg up, below your altitude limit of {limit:.0f} deg."
                               + (" It is below the horizon." if alt < 0 else " Wait until it rises higher.")}
        st = m.status()
        dist = float(angular_separation(st.ra, st.dec, ra, dec)) if st.ra is not None else 180.0
        if dist > float(s.get("mount.confirm_slew_deg", 30)):
            pend = self._pending.get(confirm or "")
            if not pend or abs(pend["ra"] - ra) > 1e-6 or abs(pend["dec"] - dec) > 1e-6 or time.time() > pend["expires"]:
                token = secrets.token_hex(6)
                self._pending[token] = {"ra": ra, "dec": dec, "expires": time.time() + 90}
                return {"started": False, "needs_confirmation": True, "token": token,
                        "message": (f"The telescope will swing about {dist:.0f} deg to point {_compass(az)} "
                                    f"(azimuth {az:.0f} deg), {alt:.0f} deg above the horizon. "
                                    "Make sure nothing (and nobody) is in the way. Start the move?")}
            self._pending.pop(confirm, None)
        m.slew_to_radec(ra, dec)
        self._note("goto", f"{label} RA {ra:.3f} Dec {dec:.3f} (alt {alt:.0f}, az {az:.0f})")
        return {"started": True, "alt": alt, "az": az, "distance_deg": dist}

    def move(self, direction: str, rate_index: int) -> dict:
        """Start/continue a manual move. The UI must call this at least every second while the button is held."""
        m = self._need()
        st = m.status()
        limit = float(settings().get("mount.alt_limit_deg", 10))
        if direction.lower() in ("down", "s") and st.alt is not None and st.alt <= limit:
            return {"moving": False, "message": f"Already at the altitude limit ({limit:.0f} deg)."}
        with self._move_lock:
            starting = self._move_deadline == 0.0
            self._move_deadline = time.time() + 1.5
        if starting or getattr(self, "_move_dir", None) != (direction, rate_index):
            m.move(direction, rate_index)
            self._move_dir = (direction, rate_index)
            self._note("move", f"{direction} speed {rate_index}")
        return {"moving": True}

    def stop_move(self) -> None:
        with self._move_lock:
            was = self._move_deadline != 0.0
            self._move_deadline = 0.0
        self._move_dir = None
        if was and self.mount:
            try:
                self.mount.stop_move()
            except Exception:
                self.mount.stop()
            self._note("move stop")

    def _watchdog(self) -> None:
        while True:
            time.sleep(0.25)
            try:
                with self._move_lock:
                    expired = self._move_deadline and time.time() > self._move_deadline
                if expired:
                    self._note("move stop", "button release not received - stopped for safety", source="watchdog")
                    self.stop_move()
                m = self.mount
                if m and m.connected and self._move_deadline:
                    st = m.status()
                    limit = float(settings().get("mount.alt_limit_deg", 10))
                    if st.alt is not None and st.alt < limit - 1:
                        self._note("STOP", f"altitude {st.alt:.1f} below limit", source="watchdog")
                        self.stop("watchdog")
            except Exception:
                log.exception("mount watchdog")

    def sync(self, ra: float, dec: float, label: str = "") -> None:
        m = self._need()
        m.sync(ra, dec)
        self._note("sync", f"{label} RA {ra:.3f} Dec {dec:.3f}")
        self._save_alignment()

    def _save_alignment(self) -> None:
        model = getattr(self.mount, "model", None)
        if model is not None and self.driver in ("synscan_wifi", "synscan_serial"):
            settings().set("mount.alignment", model.to_dict())

    def set_tracking(self, on: bool, rate: str = "sidereal") -> None:
        self._need().set_tracking(on, rate)
        self._note("tracking", f"{'on' if on else 'off'} ({rate})")

    def park(self, confirm: str | None = None) -> dict:
        m = self._need()
        if confirm != "yes":
            return {"needs_confirmation": True, "token": "yes",
                    "message": "Park returns the tube to its home position (level, pointing north) and stops tracking. Continue?"}
        m.park()
        self._note("park")
        return {"started": True}

    def set_home(self) -> None:
        m = self._need()
        if not hasattr(m, "set_home_here"):
            raise MountError("This mount keeps its own home position")
        m.set_home_here()
        settings().set("mount.alignment", None)
        self._note("set home", "current position = level, pointing north")

    def reset_alignment(self) -> None:
        self._need().reset_alignment()
        settings().set("mount.alignment", None)
        self._note("reset alignment")

    # ------------------------------------------------- plate solve & centre
    def solve_and_center(self, ra: float, dec: float, grab: Callable[[], tuple], job=None, tolerance_arcmin: float = 2.0,
                         max_iter: int = 5) -> dict:
        """Take a frame, plate-solve it, sync, re-aim; repeat until the target is centred."""
        from ...solve.solver import solve_image
        steps = []
        for i in range(max_iter):
            if job:
                job.update(i / max_iter, f"Centring: taking frame {i + 1}")
            img, scale = grab()
            st = self._need().status()
            sol, notes = solve_image(img, {"ra": st.ra, "dec": st.dec} if st.ra is not None else None, scale,
                                     check=job.check if job else None)
            if sol is None:
                return {"centered": False, "steps": steps, "message": "Could not plate-solve the frame: " + "; ".join(notes)}
            err = float(angular_separation(sol.ra, sol.dec, ra, dec)) * 60
            steps.append({"iteration": i + 1, "solved_ra": sol.ra, "solved_dec": sol.dec, "error_arcmin": round(err, 2)})
            if err <= tolerance_arcmin:
                return {"centered": True, "steps": steps, "message": f"Centred to {err:.1f} arc-minutes"}
            self.sync(sol.ra, sol.dec, "plate solve")
            self._need().slew_to_radec(ra, dec)
            self._note("goto", f"centring correction {err:.1f}'", source="plate-solve")
            t0 = time.time()
            while self._need().status().slewing and time.time() - t0 < 120:
                if job:
                    job.check()
                time.sleep(0.5)
            time.sleep(1.0)
        return {"centered": False, "steps": steps, "message": "Did not converge - check focus and exposure."}


def _compass(az: float) -> str:
    names = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
    return names[int(((az % 360) + 22.5) // 45) % 8]


controller = MountController()
