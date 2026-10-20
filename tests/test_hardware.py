"""Mount and camera drivers against simulators (no real hardware needed)."""
import datetime as dt
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest

from astroflow.astro.coords import altaz_to_radec, angular_separation, radec_to_altaz
from astroflow.config import settings
from astroflow.hardware.mount.simulator import SimSynScanServer
from astroflow.hardware.mount.synscan_motor import AltAzModel, SynScanMotorMount, dec_hex, enc24

LAT, LON = 40.0, -75.0


@pytest.fixture(autouse=True)
def location():
    settings().update({"location": {"lat": LAT, "lon": LON}, "mount": {"alt_limit_deg": 10.0, "confirm_slew_deg": 30.0}})


def now():
    return dt.datetime.now(dt.timezone.utc)


def wait_goto(m, timeout=60):
    t0 = time.time()
    while m._goto_thread and m._goto_thread.is_alive() and time.time() - t0 < timeout:
        time.sleep(0.1)


def true_err_arcmin(sim, ra, dec):
    az, alt = sim.true_altaz()
    a, z = radec_to_altaz(ra, dec, now(), LAT, LON)
    return float(angular_separation(az, alt, float(z), float(a))) * 60


def test_protocol_number_encoding():
    assert enc24(0x123456) == "563412"
    assert dec_hex("563412") == 0x123456
    assert dec_hex("3412") == 0x1234 and dec_hex("12") == 0x12


def test_alignment_model_recovers_offsets_and_tilt():
    m = AltAzModel()
    truth = AltAzModel()
    truth.dA, truth.dE, truth.tx, truth.ty = 2.0, -0.7, 0.3, -0.2
    for az, alt in [(20, 30), (120, 50), (220, 40), (300, 65)]:
        ma, me = truth.to_mount(az, alt)
        m.add_point(az, alt, ma, me)
    assert m.rms_arcmin() < 0.5
    az, alt = m.to_true(*truth.to_mount(75, 45))
    assert abs(az - 75) < 0.02 and abs(alt - 45) < 0.02


def test_goto_sync_and_tracking_against_simulator():
    sim = SimSynScanServer(slew_deg_s=40)
    m = SynScanMotorMount(LAT, LON, host="127.0.0.1", port=sim.port)
    m.connect()
    try:
        ra, dec = (float(v) for v in altaz_to_radec(45, 140, now(), LAT, LON))
        m.slew_to_radec(ra, dec)
        wait_goto(m)
        assert true_err_arcmin(sim, ra, dec) > 30          # unsynced: the simulated set-up error remains
        az, alt = sim.true_altaz()
        sra, sdec = altaz_to_radec(alt, az, now(), LAT, LON)
        m.sync(float(sra), float(sdec))
        ra2, dec2 = (float(v) for v in altaz_to_radec(60, 250, now(), LAT, LON))
        m.slew_to_radec(ra2, dec2)
        wait_goto(m)
        assert true_err_arcmin(sim, ra2, dec2) < 3
        time.sleep(6)
        assert true_err_arcmin(sim, ra2, dec2) < 3           # still on target while tracking
        m.stop()
        time.sleep(0.3)
        assert not sim.axes[1].running and not sim.axes[2].running
    finally:
        m.disconnect()
        sim.close()


def test_runaway_guard_stops_reversed_motor():
    sim = SimSynScanServer(slew_deg_s=20, reverse_axis=1)
    m = SynScanMotorMount(LAT, LON, host="127.0.0.1", port=sim.port)
    m.connect()
    hit = []
    m.on_direction_problem = hit.append
    try:
        ra, dec = (float(v) for v in altaz_to_radec(30, 90, now(), LAT, LON))
        m.slew_to_radec(ra, dec)
        wait_goto(m)
        assert hit == [1] and "AWAY" in m.status().error
        assert not sim.axes[1].running
    finally:
        m.disconnect()
        sim.close()


def test_controller_safety_rules():
    from astroflow.hardware.mount.controller import MountController
    settings().set("mount.driver", "simulator")
    c = MountController()
    c.connect("simulator")
    try:
        c.sim_server.axes[1].slew_deg_s = c.sim_server.axes[2].slew_deg_s = 40
        low = altaz_to_radec(5, 180, now(), LAT, LON)
        r = c.goto(float(low[0]), float(low[1]), "low target")
        assert r.get("refused") and "altitude limit" in r["message"]
        far = altaz_to_radec(50, 200, now(), LAT, LON)
        r = c.goto(float(far[0]), float(far[1]), "far target")
        assert r.get("needs_confirmation") and "swing" in r["message"]
        r2 = c.goto(float(far[0]), float(far[1]), "far target", confirm=r["token"])
        assert r2["started"]
        wait_goto(c.mount)
        # Dead-man switch: a move without heartbeats stops by itself.
        c.move("left", 4)
        time.sleep(2.5)
        assert not c.mount.status().moving
        assert any(e["source"] == "watchdog" for e in c.log)
        c.stop()
        assert c.log[-1]["action"] == "STOP"
    finally:
        c.disconnect()


def test_hand_controller_protocol():
    from astroflow.hardware.mount.synscan_hc import SimHCServer, SynScanHCMount
    sim = SimHCServer()
    m = SynScanHCMount(LAT, LON, "tcp", "127.0.0.1", sim.port)
    m.connect()
    try:
        m.slew_to_radec(83.822, -5.391)
        st = m.status()
        assert abs(st.ra - 83.822) < 1e-4 and abs(st.dec + 5.391) < 1e-4 and st.slewing
        m.sync(84.0, -5.0)
        assert abs(m.status().ra - 84.0) < 1e-4
        m.set_tracking(True)
        assert sim.tracking == 1
    finally:
        m.disconnect()
        sim.close()


class _AlpacaHandler(BaseHTTPRequestHandler):
    state = {"connected": False, "rightascension": 5.5, "declination": 20.0, "slewing": False, "tracking": False,
             "equatorialsystem": 2, "altitude": 40.0, "azimuth": 120.0, "atpark": False, "canpark": True,
             "canslewaltazasync": False, "cansync": True}
    calls = []

    def log_message(self, *a):
        pass

    def _reply(self, value=None):
        body = json.dumps({"Value": value, "ErrorNumber": 0, "ErrorMessage": "", "ClientTransactionID": 1,
                           "ServerTransactionID": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        name = urlparse(self.path).path.rsplit("/", 1)[-1]
        self._reply(self.state.get(name))

    def do_PUT(self):
        name = urlparse(self.path).path.rsplit("/", 1)[-1]
        n = int(self.headers.get("Content-Length", 0))
        args = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        self.calls.append((name, args))
        if name == "connected":
            self.state["connected"] = args["Connected"] == "true"
        elif name in ("slewtocoordinatesasync", "synctocoordinates"):
            self.state["rightascension"] = float(args["RightAscension"])
            self.state["declination"] = float(args["Declination"])
        elif name == "tracking":
            self.state["tracking"] = args["Tracking"] == "true"
        self._reply()


def test_alpaca_client_against_mock_server():
    from astroflow.hardware.mount.ascom import AlpacaMount
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _AlpacaHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        m = AlpacaMount(LAT, LON, f"http://127.0.0.1:{srv.server_address[1]}", 0)
        m.connect()
        m.slew_to_radec(83.82, -5.39)
        st = m.status()
        assert abs(st.ra - 83.82) < 1e-6 and abs(st.dec + 5.39) < 1e-6   # J2000 mount: no conversion
        names = [c[0] for c in _AlpacaHandler.calls]
        assert "slewtocoordinatesasync" in names and "tracking" in names
        m.disconnect()
    finally:
        srv.shutdown()


def test_capture_records_and_survives_unplug(tmp_path):
    from astroflow.hardware.capture import CaptureManager
    from astroflow.io.ser import SerReader
    settings().set("folders.captures", str(tmp_path))
    cm = CaptureManager()
    cm.connect("simulator", scene="jupiter")
    try:
        cm.camera.set_control("exposure_ms", 2)
        cm.start_recording("Jupiter", max_frames=60)
        t0 = time.time()
        while cm.recorder is not None and time.time() - t0 < 30:
            time.sleep(0.05)
        rec = cm.last_recording
        with SerReader(rec["path"]) as r:
            assert r.frame_count == 60 and r.header.color == "RGGB"
        meta = json.loads(open(rec["path"] + ".json").read())
        assert meta["OBJECT"] == "Jupiter" and "GAIN" in meta and "SWCREATE" in meta
        cm.start_recording("Jupiter")
        time.sleep(0.6)
        cm.camera.unplugged = True
        time.sleep(1.5)
        rec2 = cm.last_recording
        assert cm.state == "disconnected"
        with SerReader(rec2["path"]) as r:
            assert r.frame_count == r.header.frame_count > 0
    finally:
        cm.reconnect_wanted = False
        cm.disconnect()


def test_plate_solve_and_center_with_simulators():
    from astroflow.hardware.camera.simulator import SimCamera
    from astroflow.hardware.mount.controller import MountController
    from astroflow.io.debayer import debayer
    c = MountController()
    c.connect("simulator")
    try:
        c.sim_server.axes[1].slew_deg_s = c.sim_server.axes[2].slew_deg_s = 60
        cam = SimCamera("deepsky", c.sim_server.true_altaz, LAT, LON, sky_scale_arcsec=12.0)
        cam.connect()
        cam.set_roi(0, 0, 1200, 900, 1)
        ra, dec = (float(v) for v in altaz_to_radec(55, 160, now(), LAT, LON))
        c.goto(ra, dec, confirm=None)
        r = c.goto(ra, dec, confirm=c.goto(ra, dec).get("token"))
        wait_goto(c.mount)

        def grab():
            raw = cam.expose(0.2)
            return debayer(raw, "RGGB").astype(np.float32) / 65535, 12.0

        res = c.solve_and_center(ra, dec, grab, tolerance_arcmin=2.0)
        assert res["centered"], res
        assert res["steps"][0]["error_arcmin"] > 30 and res["steps"][-1]["error_arcmin"] <= 2.0
    finally:
        c.disconnect()
