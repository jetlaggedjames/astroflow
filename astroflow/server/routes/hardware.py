"""Camera (Capture tab) and mount (Mount tab) control. Only from the computer they're plugged into."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from ...astro.catalog import Catalog
from ...astro.coords import parse_dec, parse_ra
from ...astro.ephemeris import body_position
from ...config import settings
from ...hardware.camera.base import CameraError
from ...hardware.capture import capture
from ...hardware.mount.base import MountError
from ...hardware.mount.controller import controller
from ...jobs import jobs
from ..common import is_local, require_local

log = logging.getLogger("astroflow.api.hw")
router = APIRouter(prefix="/api", dependencies=[Depends(require_local)])
ws_router = APIRouter(prefix="/api")   # websockets check "local" themselves


def _err(e: Exception):
    raise HTTPException(400, str(e))


# ================================================================ camera ===
@router.get("/capture/status")
def cap_status():
    return capture.status()


@router.get("/capture/cameras")
def cap_cameras():
    out = [{"kind": "simulator", "name": "Simulated Uranus-C (practice and testing)"}]
    try:
        from ...hardware.camera import playerone
        for c in playerone.list_cameras():
            out.append({"kind": "playerone", "name": c["name"], "camera_id": c["id"], "serial": c["serial"]})
    except Exception as e:
        out.append({"kind": "playerone", "name": "Player One camera (library not found)", "error": str(e), "disabled": True})
    import sys
    if sys.platform.startswith("win"):
        out.append({"kind": "ascom", "name": "ASCOM camera (choose driver)"})
    return out


@router.post("/capture/connect")
def cap_connect(body: dict):
    try:
        settings().set("camera.driver", body.get("kind", "simulator"))
        if body.get("scene"):
            settings().set("camera.sim_scene", body["scene"])
        return capture.connect(body.get("kind"), scene=body.get("scene"), camera_id=body.get("camera_id"))
    except (CameraError, OSError) as e:
        _err(e)


@router.post("/capture/disconnect")
def cap_disconnect():
    capture.disconnect()
    return capture.status()


@router.post("/capture/control")
def cap_control(body: dict):
    if not capture.camera:
        _err(CameraError("No camera connected"))
    try:
        capture.camera.set_control(body["name"], float(body["value"]), bool(body.get("auto", False)))
    except (CameraError, KeyError, ValueError) as e:
        _err(e)
    return capture.status()


@router.post("/capture/roi")
def cap_roi(body: dict):
    cam = capture.camera
    if not cam:
        _err(CameraError("No camera connected"))
    if capture.recorder:
        _err(CameraError("Stop recording before changing the frame size"))
    if body.get("full"):
        roi = cam.set_roi(0, 0, cam.info.width, cam.info.height, int(body.get("bin", 1)))
    elif body.get("center_size"):
        w, h = body["center_size"]
        roi = cam.set_roi((cam.info.width - w) // 2, (cam.info.height - h) // 2, w, h, int(body.get("bin", 1)))
    else:
        roi = cam.set_roi(int(body["x"]), int(body["y"]), int(body["w"]), int(body["h"]), int(body.get("bin", 1)))
    return {"roi": roi}


@router.post("/capture/bitdepth")
def cap_bits(body: dict):
    if not capture.camera:
        _err(CameraError("No camera connected"))
    capture.camera.set_bit_depth(int(body.get("bits", 16)))
    return capture.status()


@router.post("/capture/mode")
def cap_mode(body: dict):
    try:
        capture.set_mode(body.get("mode", "stream"), body.get("exposure_s"))
        if body.get("live", True) and capture.camera and not (capture._acq and capture._acq.is_alive()):
            capture.start_live()
    except CameraError as e:
        _err(e)
    return capture.status()


@router.post("/capture/live")
def cap_live(body: dict):
    try:
        capture.start_live() if body.get("on", True) else capture.stop_live()
    except CameraError as e:
        _err(e)
    return capture.status()


@router.post("/capture/scene")
def cap_scene(body: dict):
    cam = capture.camera
    if not cam or not hasattr(cam, "set_scene"):
        _err(CameraError("Only the simulator has scenes"))
    cam.set_scene(body["scene"])
    settings().set("camera.sim_scene", body["scene"])
    if body["scene"] in ("jupiter", "saturn", "mars", "moon"):
        cam.set_roi(cam.info.width // 2 - 320, cam.info.height // 2 - 240, 640, 480, 1)
    return capture.status()


@router.post("/capture/record/start")
def rec_start(body: dict):
    try:
        return capture.start_recording(body.get("target", ""), body.get("format"), int(body.get("max_frames") or 0),
                                       float(body.get("max_seconds") or 0), body.get("folder"))
    except CameraError as e:
        _err(e)


@router.post("/capture/record/stop")
def rec_stop():
    return capture.stop_recording() or {}


@router.post("/capture/target")
def cap_target(body: dict):
    capture.target = body.get("target", "")
    return {"target": capture.target}


@router.post("/capture/snapshot")
def snapshot(body: dict):
    try:
        return {"path": capture.snapshot(body.get("format", "fits"), body.get("target", ""))}
    except CameraError as e:
        _err(e)


@router.get("/capture/preview.jpg")
def preview(max_w: int = 1280, stretch: str = "auto"):
    data = capture.preview_jpeg(max_w, stretch)
    if data is None:
        raise HTTPException(404, "No frame yet")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/capture/livestack/start")
def ls_start(body: dict):
    try:
        capture.start_livestack(bool(body.get("calibrate", True)))
    except CameraError as e:
        _err(e)
    return capture.status()


@router.post("/capture/livestack/stop")
def ls_stop(body: dict):
    return {"saved": capture.stop_livestack(bool(body.get("save", True)))}


@router.get("/capture/livestack.jpg")
def ls_jpeg(max_w: int = 1280):
    data = capture.livestack_jpeg(max_w)
    if data is None:
        raise HTTPException(404, "Nothing stacked yet")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/capture/calibration/start")
def calib_start(body: dict):
    try:
        return capture.start_calibration(body["kind"], int(body.get("count", 20)), body.get("exposure_s"))
    except (CameraError, KeyError) as e:
        _err(e)


@router.post("/capture/calibration/cancel")
def calib_cancel():
    capture.cancel_calibration()
    return {"ok": True}


@router.get("/capture/collimation")
def collimation():
    return capture.collimation()


@router.post("/capture/solve")
def capture_solve(body: dict):
    """'Where am I pointing?': plate-solve the latest camera frame (nothing is saved to disk)."""
    with capture.lock:
        frame = capture.latest
    cam = capture.camera
    if frame is None or cam is None:
        _err(CameraError("No camera frame yet - start the live view first"))

    def run(job):
        from ...io.debayer import debayer
        from ...io.images import to_float
        from ...solve import annotate
        from ...solve.solver import scale_from_meta, solve_image
        img = debayer(frame, cam.info.bayer) if cam.info.bayer and frame.ndim == 2 and cam.get_roi().get("bin", 1) == 1 else frame
        roi = cam.get_roi()
        meta = {"FOCALLEN": settings().effective_focal_length(), "XPIXSZ": cam.info.pixel_um, "XBINNING": roi.get("bin", 1)}
        if hasattr(cam, "sky_scale"):   # simulator: same pixel scale as its rendered sky
            scale = cam.sky_scale or 10.5 * 3856 / max(roi["out_w"], 1)
        else:
            scale = scale_from_meta(meta, roi["out_w"])
        hint = None
        st = controller.status()
        if st.get("connected") and st.get("ra") is not None:
            hint = {"ra": st["ra"], "dec": st["dec"]}
        sol, notes = solve_image(to_float(img), hint, scale, body.get("engine"), progress=job.update, check=job.check)
        if not sol:
            return {"solved": False, "notes": notes}
        from ...astro.coords import angular_separation, format_dec, format_ra
        out = {"solved": True, "notes": notes, "solution": sol.to_dict(), "ra_str": format_ra(sol.ra), "dec_str": format_dec(sol.dec),
               "objects": annotate.objects_in_image(sol, include_stars=False)[:12]}
        if st.get("connected") and st.get("ra") is not None:
            out["mount_error_arcmin"] = float(angular_separation(st["ra"], st["dec"], sol.ra, sol.dec)) * 60
        return out

    j = jobs.submit("solve", "Where am I pointing?", run, heavy=False)
    return {"job": j.id}


@router.post("/capture/focusbox")
def focusbox(body: dict):
    b = body.get("box")
    capture.focus_box = tuple(b) if b else None
    capture.focus_history.clear()
    return {"box": b}


@ws_router.websocket("/ws/preview")
async def ws_preview(ws: WebSocket):
    if not is_local(ws):  # type: ignore[arg-type]
        await ws.close(code=4403)
        return
    await ws.accept()
    last_seq = -1
    params = {"max_w": 1280, "stretch": "auto", "fps": 12, "livestack": False}
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=1.0 / params["fps"])
                params.update(json.loads(msg))
            except asyncio.TimeoutError:
                pass
            st = await asyncio.to_thread(capture.status)
            await ws.send_text(json.dumps({"type": "status", "status": st}, default=str))
            if capture.seq != last_seq:
                last_seq = capture.seq
                if params.get("livestack") and capture.livestack is not None:
                    data = await asyncio.to_thread(capture.livestack_jpeg, int(params["max_w"]))
                else:
                    data = await asyncio.to_thread(capture.preview_jpeg, int(params["max_w"]), params["stretch"])
                if data:
                    await ws.send_bytes(data)
    except (WebSocketDisconnect, RuntimeError):
        return


# ================================================================= mount ===
def _resolve_target(body: dict) -> tuple[float, float, str]:
    if body.get("ra") is not None and body.get("dec") is not None:
        return parse_ra(body["ra"]), parse_dec(body["dec"]), body.get("label", "")
    name = body.get("name") or body.get("target")
    if not name:
        raise MountError("Choose a target")
    low = str(name).lower()
    if low in ("sun",):
        raise MountError("Pointing at the Sun is blocked for safety. Never look at the Sun without a proper solar filter.")
    if low in ("moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune"):
        p = body_position(low, dt.datetime.now(dt.timezone.utc))
        return p["ra"], p["dec"], p["name"]
    o = Catalog.default().get(name)
    if not o:
        raise MountError(f"Unknown object: {name}")
    return o.ra, o.dec, o.label


@router.get("/mount/status")
def mount_status():
    return controller.status()


@router.post("/mount/connect")
def mount_connect(body: dict):
    try:
        if body.get("driver"):
            settings().set("mount.driver", body["driver"])
        controller.connect(body.get("driver"))
    except (MountError, OSError, ValueError) as e:
        _err(e)
    return controller.status()


@router.post("/mount/disconnect")
def mount_disconnect():
    controller.disconnect()
    return controller.status()


@router.post("/mount/stop")
def mount_stop():
    controller.stop()
    return controller.status()


@router.post("/mount/goto")
def mount_goto(body: dict):
    try:
        ra, dec, label = _resolve_target(body)
        return controller.goto(ra, dec, label, body.get("confirm"))
    except MountError as e:
        _err(e)


@router.post("/mount/move")
def mount_move(body: dict):
    try:
        return controller.move(body["direction"], int(body.get("rate", 4)))
    except (MountError, KeyError) as e:
        _err(e)


@router.post("/mount/move/stop")
def mount_move_stop():
    controller.stop_move()
    return {"ok": True}


@router.post("/mount/sync")
def mount_sync(body: dict):
    try:
        ra, dec, label = _resolve_target(body)
        controller.sync(ra, dec, label)
    except MountError as e:
        _err(e)
    return controller.status()


@router.post("/mount/tracking")
def mount_tracking(body: dict):
    try:
        controller.set_tracking(bool(body.get("on", True)), body.get("rate", "sidereal"))
    except MountError as e:
        _err(e)
    return controller.status()


@router.post("/mount/park")
def mount_park(body: dict):
    try:
        return controller.park(body.get("confirm"))
    except MountError as e:
        _err(e)


@router.post("/mount/home")
def mount_home():
    try:
        controller.set_home()
    except MountError as e:
        _err(e)
    return controller.status()


@router.post("/mount/alignment/reset")
def mount_reset_alignment():
    try:
        controller.reset_alignment()
    except MountError as e:
        _err(e)
    return controller.status()


@router.post("/mount/center")
def mount_center(body: dict):
    """Plate-solve-and-centre: take frames with the connected camera until the target is centred."""
    try:
        ra, dec, label = _resolve_target(body)
    except MountError as e:
        _err(e)
    if not capture.camera:
        _err(CameraError("Connect the camera first (it takes the frames to solve)"))
    exp = float(body.get("exposure_s", 2.0))

    def grab():
        from ...io.debayer import debayer
        from ...io.images import to_float
        from ...solve.solver import scale_from_meta
        was_live = capture._acq is not None and capture._acq.is_alive()
        if was_live:
            capture.stop_live()
        try:
            raw = capture.camera.expose(exp)
        finally:
            if was_live:
                capture.start_live()
        cam = capture.camera
        img = debayer(raw, cam.info.bayer) if cam.info.bayer and raw.ndim == 2 else raw
        roi = cam.get_roi()
        meta = {"FOCALLEN": settings().effective_focal_length(), "XPIXSZ": cam.info.pixel_um, "XBINNING": roi.get("bin", 1)}
        if hasattr(cam, "sky_scale"):
            meta = {}
        return to_float(img), scale_from_meta(meta, roi["out_w"]) if meta else None

    def run(job):
        return controller.solve_and_center(ra, dec, grab, job, float(body.get("tolerance_arcmin", 2.0)))

    j = jobs.submit("center", f"Centring {label}", run)
    return {"job": j.id}


@router.get("/mount/serial_ports")
def serial_ports():
    try:
        from serial.tools import list_ports
        return [{"device": p.device, "description": p.description} for p in list_ports.comports()]
    except Exception:
        return []


@router.get("/mount/alpaca/discover")
def alpaca_discover():
    from ...hardware.mount.ascom import discover_alpaca
    return discover_alpaca()


@router.post("/mount/ascom/choose")
def ascom_choose(body: dict):
    kind = body.get("device", "Telescope")
    try:
        if kind == "Camera":
            from ...hardware.camera.ascom_camera import AscomCamera
            prog = AscomCamera.choose(settings().get("camera.ascom_progid", ""))
            if prog:
                settings().set("camera.ascom_progid", prog)
        else:
            from ...hardware.mount.ascom import AscomComMount
            prog = AscomComMount.choose(settings().get("mount.ascom_progid", ""))
            if prog:
                settings().set("mount.ascom_progid", prog)
        return {"progid": prog}
    except Exception as e:
        _err(e)
