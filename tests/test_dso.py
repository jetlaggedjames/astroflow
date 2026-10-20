"""End-to-end deep-sky stacking on a simulated session (field rotation, hot pixels, dust, satellite)."""
import cv2
import numpy as np

from astroflow.imaging import dso
from astroflow.imaging.calibrate import CalibrationLibrary
from astroflow.io.debayer import debayer
from astroflow.io.images import read_image
from astroflow.sim.scenes import write_dso_session


def _noise(img):
    g = img.mean(axis=2) if img.ndim == 3 else img
    hp = g - cv2.GaussianBlur(g, (0, 0), 3)
    return 1.4826 * np.median(np.abs(hp - np.median(hp)))


def test_dso_stack_end_to_end(tmp_path):
    info = write_dso_session(tmp_path, n_lights=8, size=(360, 480), n_darks=5, n_flats=5, n_bias=5, trail_frame=3)
    P = info["paths"]
    res = dso.stack([{"lights": P["lights"], "dark": P["darks"], "flat": P["flats"], "bias": P["bias"]}], {})
    assert res["frames_used"] == 8
    rot = [r["rotation_deg"] for r in res["frames"]]
    assert max(rot) - min(rot) > 0.5            # field rotation was present and measured
    assert all(r["rms"] < 0.6 for r in res["frames"])
    raw, _ = read_image(P["lights"][0])
    single = debayer(raw, "RGGB").astype(np.float32) / 65535
    assert _noise(single) / _noise(res["image"]) > 2.3
    # Masters were saved in the calibration library for next time.
    kinds = {e["kind"] for e in CalibrationLibrary().list()}
    assert {"bias", "dark", "flat"} <= kinds
    # Re-stack using the library automatically.
    res2 = dso.stack([{"lights": P["lights"][:4], "dark": "auto", "flat": "auto", "bias": "auto"}], {})
    assert res2["calibration"][0]["dark"] and res2["calibration"][0]["flat"]


def test_flat_removes_dust_donut(tmp_path):
    info = write_dso_session(tmp_path, n_lights=4, size=(300, 400), n_darks=3, n_flats=5, n_bias=3, trail_frame=None,
                             rotation_deg_per_s=0.0)
    P = info["paths"]
    res = dso.stack([{"lights": P["lights"], "dark": P["darks"], "flat": P["flats"]}], {"crop": False})
    img = res["image"].mean(axis=2)
    H, W = img.shape
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.hypot(xx - W * 0.3, yy - H * 0.6)
    ring = img[(d > 22) & (d < 28)].mean()
    around = img[(d > 40) & (d < 55)].mean()
    assert abs(ring - around) / around < 0.03


def test_live_stack_improves_and_rejects_trail(tmp_path):
    from astroflow.imaging.livestack import LiveStacker
    from astroflow.sim.scenes import StarFieldScene, sensor_readout
    scene = StarFieldScene((300, 400), n_stars=250, seed=5, rotation_deg_per_s=0.01)
    rng = np.random.default_rng(0)
    ls = LiveStacker(bayer="RGGB")
    first = None
    for i in range(10):
        sky = scene.render(i * 20.0, 10.0, trail=(i == 6))
        raw = sensor_readout(sky, 12, 1.0, "RGGB", read_noise=0.003, rng=rng, photon_scale=20000.0)
        r = ls.add(raw, 10.0)
        assert r["stacked"], r
        if first is None:
            first = ls.image().copy()
    final = ls.image()
    assert ls.frames == 10
    assert _noise(first[20:-20, 20:-20]) / _noise(final[20:-20, 20:-20]) > 2.0
    assert abs(ls.log[-1]["rotation_deg"]) > 1.5   # 180 s at 0.01 deg/s


def test_mosaic_reassembles_moon():
    from astroflow.imaging.mosaic import stitch
    from astroflow.sim.scenes import PlanetScene
    full = PlanetScene("moon", (500, 700), 0, seed=4).render(0)
    panels = [full[0:300, 0:420], full[0:300, 280:700], full[200:500, 0:420], full[200:500, 280:700]]
    res = stitch(panels)
    assert sorted(res["placed"]) == [0, 1, 2, 3]
    out = res["image"]
    assert abs(out.shape[0] - 500) <= 3 and abs(out.shape[1] - 700) <= 3
    from astroflow.imaging.register import xcorr_shift
    h, w = min(out.shape[0], 500), min(out.shape[1], 700)
    # The canvas origin is arbitrary: remove the overall offset, then compare.
    dx, dy, _ = xcorr_shift(full.mean(axis=2)[:h, :w].astype(np.float32), out.mean(axis=2)[:h, :w].astype(np.float32))
    dx, dy = int(round(dx)), int(round(dy))
    assert abs(dx) <= 2 and abs(dy) <= 2
    a = out[10 + dy:h - 10 + dy, 10 + dx:w - 10 + dx]
    err = np.abs(a - full[10:h - 10, 10:w - 10]).mean()
    assert err < 0.01
