"""The web API as the user interface uses it: the whole pipeline, safety rules and second-computer access."""
import hashlib
import os
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from astroflow.config import settings
from astroflow.library import cleanup
from astroflow.server.app import create_app

REMOTE = ("192.168.1.50", 50123)


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture()
def local(app):
    with TestClient(app) as c:
        yield c


def remote_client(app, code=None):
    headers = {"X-Access-Code": code} if code else None
    return TestClient(app, client=REMOTE, headers=headers)


def wait(client, job_id, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] == "done":
            return j["result"]
        if j["status"] in ("error", "cancelled"):
            raise AssertionError(f"job {j['title']} {j['status']}: {j.get('error')}")
        time.sleep(0.2)
    raise AssertionError("job timed out")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ------------------------------------------------------------------ basics
def test_status_settings_and_ui(local):
    s = local.get("/api/status").json()
    assert s["local"] is True and s["version"]
    assert local.get("/").status_code == 200 and b"AstroFlow" in local.get("/").content
    assert local.get("/js/app.js").status_code == 200
    r = local.patch("/api/settings", json={"theme": "night"})
    assert r.json()["theme"] == "night"
    assert local.get("/api/process/ops").json()["ops"]
    fov = local.get("/api/wizard/fov").json()
    assert abs(fov["fov_deg"][0] - 0.427) < 0.01 and fov["recommended"]["name"] in ("d80", "d50")


# ----------------------------------------------------- second computer rules
def test_remote_blocked_until_allowed_then_needs_code(app, local):
    local.patch("/api/settings", json={"network": {"allow_lan": False}})
    with remote_client(app) as r:
        assert r.get("/api/status").status_code == 403
    local.patch("/api/settings", json={"network": {"allow_lan": True}})
    code = settings().get("network.access_code")
    with remote_client(app) as r:
        assert r.get("/api/status").status_code == 401
        assert r.get("/").status_code == 401 and b"access code" in r.get("/").content.lower()
        assert r.post("/api/auth", json={"code": "nope"}).status_code == 401
        assert r.post("/api/auth", json={"code": code}).status_code == 200
        st = r.get("/api/status")            # the cookie from /api/auth now lets it in
        assert st.status_code == 200 and st.json()["local"] is False
    local.patch("/api/settings", json={"network": {"allow_lan": False}})


def test_remote_cannot_touch_hardware_or_protected_settings(app, local, tmp_path):
    local.patch("/api/settings", json={"network": {"allow_lan": True}})
    code = settings().get("network.access_code")
    with remote_client(app, code) as r:
        assert r.get("/api/capture/status").status_code == 403
        assert r.post("/api/mount/goto", json={"name": "M 31"}).status_code == 403
        assert r.post("/api/mount/stop").status_code == 403
        assert r.patch("/api/settings", json={"mount": {"alt_limit_deg": 0}}).status_code == 403
        assert r.patch("/api/settings", json={"network": {"allow_lan": True}}).status_code == 403
        assert r.patch("/api/settings", json={"theme": "day"}).status_code == 200
        assert "access_code" not in r.get("/api/settings").json()["network"]
        # files outside the shared library folders are off limits
        secret = tmp_path / "private.png"
        secret.write_bytes(b"x")
        assert r.get("/api/image/preview", params={"path": str(secret)}).status_code == 403
        assert r.get("/api/download", params={"path": "/etc/passwd"}).status_code == 403
        assert r.get("/api/fs/list", params={"path": "/"}).status_code == 403
        assert r.post("/api/shutdown").status_code == 403
        assert r.post("/api/cleanup/plan/offload", json={"ids": [1], "dest": str(tmp_path)}).status_code == 403
    local.patch("/api/settings", json={"network": {"allow_lan": False}})


def test_remote_upload_then_download(app, local):
    local.patch("/api/settings", json={"network": {"allow_lan": True}})
    code = settings().get("network.access_code")
    img = (np.random.default_rng(1).random((40, 60, 3)) * 255).astype(np.uint8)
    import cv2
    ok, buf = cv2.imencode(".png", img)
    with remote_client(app, code) as r:
        res = r.post("/api/upload", files={"file": ("moon.png", buf.tobytes(), "image/png")}, data={"subfolder": "from-laptop"}).json()
        assert Path(res["path"]).exists() and res["ids"]
        got = r.get("/api/download", params={"path": res["path"]})
        assert got.status_code == 200 and got.content == buf.tobytes()
    local.patch("/api/settings", json={"network": {"allow_lan": False}})


# --------------------------------------------------------- clean-up safety
def test_cleanup_needs_confirmed_single_use_plan(local, tmp_path, monkeypatch):
    root = Path(settings().folder("captures")) / "cleanup-test"
    root.mkdir(parents=True, exist_ok=True)
    keep, junk = root / "keep.png", root / "junk.png"
    import cv2
    cv2.imwrite(str(keep), np.zeros((32, 32), np.uint8))
    cv2.imwrite(str(junk), np.full((32, 32), 9, np.uint8))
    raw = root / "capture.ser"
    from astroflow.sim.scenes import jupiter_ser
    jupiter_ser(raw, n_frames=5, size=(48, 64))
    wait(local, local.post("/api/library/scan", json={"roots": [str(root)]}).json()["job"])
    items = {Path(i["path"]).name: i for i in local.get("/api/library/query", params={"text": "", "limit": 2000}).json()["items"]}
    trashed = []
    monkeypatch.setattr(cleanup, "_to_trash", lambda p: (trashed.append(p), os.remove(p)))

    # Nothing happens without a plan, and a made-up token is refused.
    assert local.post("/api/cleanup/execute", json={"token": "made-up"}).status_code == 409
    # Raw captures are protected unless explicitly included.
    r = local.post("/api/cleanup/plan/trash", json={"ids": [items["capture.ser"]["id"]]})
    assert r.status_code == 409 and "RAW" in r.json()["detail"]
    assert raw.exists()
    # A plan lists exactly what will happen and the total size...
    plan = local.post("/api/cleanup/plan/trash", json={"ids": [items["junk.png"]["id"]]}).json()
    assert [Path(f["path"]).name for f in plan["files"]] == ["junk.png"]
    assert plan["total_size"] == junk.stat().st_size and "Recycle Bin" in plan["summary"]
    assert junk.exists()                     # planning alone deletes nothing
    # ...and only the confirmed token executes it, once.
    res = wait(local, local.post("/api/cleanup/execute", json={"token": plan["token"]}).json()["job"])
    assert res["done"] == 1 and not junk.exists() and keep.exists() and raw.exists()
    assert local.post("/api/cleanup/execute", json={"token": plan["token"]}).status_code == 409
    # A cancelled plan cannot be executed either.
    plan2 = local.post("/api/cleanup/plan/trash", json={"ids": [items["keep.png"]["id"]]}).json()
    local.post("/api/cleanup/cancel", json={"token": plan2["token"]})
    assert local.post("/api/cleanup/execute", json={"token": plan2["token"]}).status_code == 409
    assert keep.exists() and trashed == [str(junk)]


# ------------------------------------------------- planetary pipeline (API)
def test_planetary_pipeline_capture_to_export(local):
    # 1. Capture with the simulated camera
    st = local.post("/api/capture/connect", json={"kind": "simulator", "scene": "jupiter"}).json()
    assert st["camera"]["name"]
    local.post("/api/capture/roi", json={"center_size": [320, 240]})
    local.post("/api/capture/control", json={"name": "exposure_ms", "value": 5})
    local.post("/api/capture/mode", json={"mode": "stream"})
    time.sleep(1.0)
    rec = local.post("/api/capture/record/start", json={"target": "Jupiter", "format": "ser", "max_frames": 150}).json()
    t0 = time.time()
    while local.get("/api/capture/status").json().get("recording") and time.time() - t0 < 60:
        time.sleep(0.3)
    last = local.get("/api/capture/status").json()["last_recording"]
    assert last["frames"] == 150 and last["path"] == rec["path"]
    ser = last["path"]
    raw_hash = sha(ser)
    assert local.get("/api/capture/preview.jpg").status_code == 200
    local.post("/api/capture/disconnect")

    # 2. Analyze
    proj = local.post("/api/projects", json={"sources": [ser], "name": "Jupiter API"}).json()
    a = local.post("/api/analyze", json={"sources": [ser], "project_id": proj["id"]}).json()
    wait(local, a["job"])
    rep = local.get(f"/api/analyze/report/{a['key']}").json()
    assert rep["kind"] == "planetary" and len(rep["quality"]) == 150
    sel = local.post("/api/analyze/select", json={"key": a["key"], "mode": "percent", "value": 40}).json()
    assert sel["count"] == 60
    assert local.get("/api/analyze/frame", params={"key": a["key"], "index": sel["frames"][0]}).status_code == 200

    # 3. Stack (estimate first, then run)
    est = local.post("/api/stack/estimate", json={"sources": [ser], "kind": "lucky", "frames": 60}).json()
    assert est["seconds"] > 0 and est["peak_memory_bytes"] > 0
    st = wait(local, local.post("/api/stack/run", json={"kind": "lucky", "sources": [ser], "frames": sel["frames"], "project_id": proj["id"]}).json()["job"])
    assert st["frames"] == 60 and isinstance(st["aps"], int) and Path(st["fits"]).exists() and Path(st["tiff"]).exists()
    p = local.get(f"/api/projects/{proj['id']}").json()
    assert p["process"]["base"] == st["fits"]

    # 4. Post-process: Auto, preview render, history, full-resolution save
    auto = local.post("/api/process/auto", json={"path": st["fits"]}).json()
    assert auto["target"] == "planet" and auto["steps"]
    rnd = local.post("/api/process/render", json={"path": st["fits"], "steps": auto["steps"], "max_size": 400}).json()
    assert rnd["image"].startswith("data:image/jpeg") and rnd["histogram"]
    h = local.post(f"/api/projects/{proj['id']}/history/commit", json={"steps": auto["steps"], "masks": {}, "label": "Auto", "base": st["fits"]}).json()
    assert [e["label"] for e in h["history"]] == ["Original (no edits)", "Auto"]
    assert local.post(f"/api/projects/{proj['id']}/history/undo").json()["steps"] == []
    assert len(local.post(f"/api/projects/{proj['id']}/history/redo").json()["steps"]) == len(auto["steps"])
    saved = wait(local, local.post("/api/process/save", json={"path": st["fits"], "project_id": proj["id"], "steps": auto["steps"]}).json()["job"])
    assert Path(saved["tiff"]).exists()

    # 5. Export (JPEG with watermark + caption, and a 16-bit TIFF)
    opts = {"format": "jpg", "quality": 90, "size": "custom", "width": 300, "watermark": {"enabled": True, "text": "Test"}, "caption": True}
    out = wait(local, local.post("/api/export", json={"items": [{"path": st["fits"], "project_id": proj["id"]}], "options": opts}).json()["job"])
    f = out["files"][0]
    assert Path(f["path"]).exists() and f["width"] == 300
    out2 = wait(local, local.post("/api/export", json={"items": [{"path": saved["tiff"]}], "options": {"format": "tiff", "bits": 16}}).json()["job"])
    import tifffile
    assert tifffile.imread(out2["files"][0]["path"]).dtype == np.uint16
    # The raw recording was never modified.
    assert sha(ser) == raw_hash
    # Everything shows up in the library with the right status.
    statuses = {i["status"] for i in local.get("/api/library/query", params={"target": "Jupiter", "limit": 2000}).json()["items"]}
    assert {"raw", "stacked", "processed", "exported"} <= statuses


# ---------------------------------------------- deep-sky + solve + labels
def test_deepsky_stack_solve_and_annotate(local, tmp_path):
    from astroflow.io.images import write_fits
    from astroflow.sim.scenes import render_sky
    folder = Path(settings().folder("captures")) / "M31-api"
    folder.mkdir(parents=True, exist_ok=True)
    scale = 30.0      # a wide field (~10 x 6 deg): the bundled catalogue has ~2 stars per square degree to mag 9
    for i in range(6):
        img, _ = render_sky(10.68, 41.27, scale, 20 + i * 0.4, 1200, 760, fwhm=2.4, noise=0.004, seed=i, mag9_peak=0.3)
        write_fits(folder / f"m31_{i:02d}.fits", (np.clip(img, 0, 1) * 65535).astype(np.uint16),
                   {"OBJECT": "M 31", "EXPTIME": 10, "FOCALLEN": round(206.265 * 2.9 / scale, 3), "XPIXSZ": 2.9, "IMAGETYP": "Light"})
    srcs = [str(folder)]
    a = local.post("/api/analyze", json={"sources": srcs, "kind": "dso"}).json()
    wait(local, a["job"])
    rep = local.get(f"/api/analyze/report/{a['key']}").json()
    assert rep["kind"] == "dso" and len(rep["files"]) == 6
    assert local.get("/api/analyze/frame", params={"key": a["key"], "index": 3}).status_code == 200
    res = wait(local, local.post("/api/stack/run", json={"kind": "dso", "groups": [{"lights": rep["files"][:5], "dark": "none", "flat": "none", "bias": "none"}]}).json()["job"])
    assert res["frames_used"] == 5 and res["max_field_rotation_deg"] > 1.0
    sol = wait(local, local.post("/api/solve", json={"path": res["fits"], "engine": "builtin"}).json()["job"])
    assert sol["solved"], sol["notes"]
    assert abs(sol["solution"]["ra"] - 10.68) < 0.3 and abs(sol["solution"]["dec"] - 41.27) < 0.3
    ann = local.get("/api/solve/annotations", params={"path": res["fits"], "stars": False}).json()
    assert any("M 31" in o["label"] for o in ann["objects"])
    rec = [i for i in local.get("/api/library/query", params={"text": Path(res["fits"]).name}).json()["items"]][0]
    assert rec["solved"] == 1
    out = wait(local, local.post("/api/export", json={"items": [{"path": res["fits"]}], "options": {"format": "png", "annotate": True, "compass": True, "scale_bar": True}}).json()["job"])
    assert Path(out["files"][0]["path"]).exists()


# ------------------------------------------------------- mount safety (API)
def test_mount_api_confirmation_heartbeat_and_stop(local):
    local.patch("/api/settings", json={"location": {"lat": 40.7, "lon": -74.0, "tz": "America/New_York"}})
    st = local.post("/api/mount/connect", json={"driver": "simulator"}).json()
    assert st["connected"]
    try:
        assert local.post("/api/mount/goto", json={"name": "Sun"}).status_code == 400
        # pick something well above the horizon right now
        tonight = local.get("/api/catalog/tonight", params={"limit": 40}).json()["targets"]
        tgt = None
        for t in tonight:
            pos = local.get("/api/catalog/position", params={"name": t["id"]}).json()
            if pos.get("alt", 0) > 30:
                tgt = pos
                break
        assert tgt, "nothing up?"
        r = local.post("/api/mount/goto", json={"ra": tgt["ra"], "dec": tgt["dec"], "label": tgt["name"]}).json()
        assert r.get("needs_confirmation") and r["token"]            # big move: asks first, nothing moved
        assert local.get("/api/mount/status").json()["slewing"] is False
        r2 = local.post("/api/mount/goto", json={"ra": tgt["ra"], "dec": tgt["dec"], "label": tgt["name"], "confirm": r["token"]}).json()
        assert r2["started"]
        time.sleep(0.5)
        local.post("/api/mount/stop")
        time.sleep(0.5)
        assert local.get("/api/mount/status").json()["slewing"] is False
        # Hand moves need a heartbeat: without one the watchdog stops the mount by itself.
        assert local.post("/api/mount/move", json={"direction": "left", "rate": 6}).json()["moving"]
        time.sleep(2.5)
        log = local.get("/api/mount/status").json()["log"]
        assert any(e["source"] == "watchdog" for e in log)
        park = local.post("/api/mount/park", json={}).json()
        assert park["needs_confirmation"]
    finally:
        local.post("/api/mount/disconnect")
