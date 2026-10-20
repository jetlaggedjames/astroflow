"""Tests for the core image maths: stars, registration, scoring, lucky stacking."""
import numpy as np
import cv2
import pytest

from astroflow.imaging import lucky, quality
from astroflow.imaging.register import match_stars, xcorr_shift, template_shift, warp, Transform
from astroflow.imaging.stars import detect_stars, measure_summary
from astroflow.io.source import FrameSource
from astroflow.sim.scenes import PlanetScene, Turbulence, sensor_readout


def gaussian_field(n=250, fwhm=3.5, shape=(600, 800), seed=0, noise=0.003):
    rng = np.random.default_rng(seed)
    H, W = shape
    img = np.full(shape, 0.05, np.float32)
    xs, ys = rng.uniform(15, W - 15, n), rng.uniform(15, H - 15, n)
    fl = rng.uniform(0.03, 0.5, n)
    s = fwhm / 2.3548
    yy, xx = np.mgrid[0:H, 0:W]
    for x, y, f in zip(xs, ys, fl):
        r = int(4 * s) + 2
        sl = (slice(int(y) - r, int(y) + r + 1), slice(int(x) - r, int(x) + r + 1))
        img[sl] += f * np.exp(-((xx[sl] - x) ** 2 + (yy[sl] - y) ** 2) / (2 * s * s))
    return img + rng.normal(0, noise, shape).astype(np.float32), np.column_stack([xs, ys])


def test_star_detection_measures_fwhm():
    img, truth = gaussian_field(fwhm=4.0)
    st = detect_stars(img)
    s = measure_summary(st)
    assert len(st) > 200
    assert abs(s["fwhm"] - 4.0) < 0.4


def test_registration_recovers_rotation_and_shift():
    rng = np.random.default_rng(3)
    ref = np.column_stack([rng.uniform(0, 1500, 200), rng.uniform(0, 1000, 200)])
    a = np.radians(3.7)
    M = np.array([[np.cos(a), -np.sin(a), 25.3], [np.sin(a), np.cos(a), -12.8], [0, 0, 1]])
    mv = (np.column_stack([ref, np.ones(200)]) @ np.linalg.inv(M).T)[:, :2] + rng.normal(0, 0.15, (200, 2))
    mv = np.vstack([mv[rng.random(200) > 0.2], rng.uniform(0, 1500, (15, 2))])
    tr = match_stars(mv, ref)
    assert abs(tr.rotation_deg - 3.7) < 0.02
    assert abs(tr.shift[0] - 25.3) < 0.3 and abs(tr.shift[1] + 12.8) < 0.3
    assert tr.rms < 0.4


def test_warp_aligns_rotated_image():
    img, _ = gaussian_field(n=150, shape=(300, 400), seed=2)
    a = np.radians(2.0)
    M = np.array([[np.cos(a), -np.sin(a), 4.0], [np.sin(a), np.cos(a), -3.0], [0, 0, 1]])
    moved = cv2.warpPerspective(img, np.linalg.inv(M), (400, 300), flags=cv2.INTER_CUBIC)
    tr = match_stars(detect_stars(moved), detect_stars(img))
    back = warp(moved, tr, img.shape, "bicubic")
    ok = np.isfinite(back)
    ok[:20] = ok[-20:] = False
    ok[:, :20] = ok[:, -20:] = False
    assert np.mean(np.abs(back[ok] - img[ok])) < 0.004


def test_shift_estimators_subpixel():
    sc = PlanetScene("jupiter", (240, 320), 70)
    truth = sc.render(0).mean(axis=2).astype(np.float32)
    rng = np.random.default_rng(5)
    errs, perrs = [], []
    for _ in range(15):
        dx, dy = rng.uniform(-6, 6, 2)
        moved = cv2.warpAffine(truth, np.float32([[1, 0, dx], [0, 1, dy]]), (320, 240), flags=cv2.INTER_CUBIC)
        moved = cv2.GaussianBlur(moved, (0, 0), 1.5) + rng.normal(0, 0.01, moved.shape).astype(np.float32)
        ex, ey, _ = xcorr_shift(truth, moved)
        errs.append(np.hypot(ex - dx, ey - dy))
        sx, sy = rng.uniform(-3, 3, 2)
        m2 = cv2.warpAffine(truth, np.float32([[1, 0, sx], [0, 1, sy]]), (320, 240), flags=cv2.INTER_CUBIC)
        b1, b2 = cv2.GaussianBlur(truth, (0, 0), 1), cv2.GaussianBlur(m2, (0, 0), 1)
        y, x, h, m = 110, 150, 16, 6
        tx, ty, _ = template_shift(b1[y - h:y + h, x - h:x + h], b2[y - h - m:y + h + m, x - h - m:x + h + m], m)
        perrs.append(np.hypot(tx - sx, ty - sy))
    assert np.mean(errs) < 0.15
    assert np.mean(perrs) < 0.3


class MemSource(FrameSource):
    def __init__(self, frames, bayer="RGGB"):
        self.frames = frames
        self.count = len(frames)
        self.height, self.width = frames[0].shape[:2]
        self.channels = 1
        self.bayer = bayer
        self.bit_depth = 12
        self.meta = {}
        self.paths = []

    def get_raw(self, i):
        return self.frames[i]


def _jupiter_frames(n=200, seeing=1.2, seed=0):
    truth = PlanetScene("jupiter", (240, 320), 70).render(0)
    tb = Turbulence((240, 320), seeing, seed + 10)
    rng = np.random.default_rng(seed)
    frames, blurs = [], []
    for _ in range(n):
        fr, b = tb.apply(truth)
        frames.append(sensor_readout(fr, 12, 1.0, "RGGB", rng=rng))
        blurs.append(b)
    return truth, frames, np.array(blurs)


def _flow_error(img, truth):
    """Mean error after removing small geometric offsets (measures blur + noise, not tiny bends)."""
    tg = truth.mean(axis=2).astype(np.float32)
    g = img.mean(axis=2).astype(np.float32)
    H, W = tg.shape
    inner = cv2.erode((tg > 0.05).astype(np.uint8), np.ones((15, 15))) > 0
    dx, dy, _ = xcorr_shift(tg, g)
    g = cv2.warpAffine(g, np.float32([[1, 0, -dx], [0, 1, -dy]]), (W, H))
    g = g * (tg[inner].mean() / g[inner].mean())
    a8 = np.clip(tg * 255 / 0.9, 0, 255).astype(np.uint8)
    b8 = np.clip(g * 255 / 0.9, 0, 255).astype(np.uint8)
    flow = cv2.calcOpticalFlowFarneback(a8, b8, None, 0.5, 3, 21, 3, 5, 1.1, 0)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    gw = cv2.remap(g, xx + flow[..., 0], yy + flow[..., 1], cv2.INTER_LINEAR)
    return float(np.mean(np.abs(gw - tg)[inner]))


def test_quality_score_tracks_seeing():
    _, frames, blurs = _jupiter_frames(150)
    rep = quality.analyze(MemSource(frames), "planetary")
    assert np.corrcoef(rep["quality"], -blurs)[0, 1] > 0.85
    assert rep["recommendation"]["keep_count"] > 0


def test_lucky_stack_beats_single_frame_and_multipoint_beats_global():
    truth, frames, _ = _jupiter_frames(200, seeing=1.5, seed=1)
    src = MemSource(frames)
    rep = quality.analyze(src, "planetary")
    sel = quality.select_frames(rep, "percent", 25)
    single = _flow_error(src.get_float(sel[0]), truth)
    glob = _flow_error(lucky.stack(src, sel, {"multipoint": False}).image, truth)
    res = lucky.stack(src, sel, {})
    multi = _flow_error(res.image, truth)
    assert res.info["aps"] > 20
    assert glob < single
    assert multi < glob * 0.9


def test_upsampled_stack_shape():
    truth, frames, _ = _jupiter_frames(40)
    res = lucky.stack(MemSource(frames), list(range(20)), {"upsample": 2})
    assert res.image.shape == (480, 640, 3)


def test_derotation_undoes_jupiter_rotation():
    sc = PlanetScene("jupiter", (240, 320), 90)
    t0, t1 = sc.render(0.0).mean(axis=2), sc.render(900.0).mean(axis=2)  # 15 minutes later
    mask, _ = quality.object_mask(t0.astype(np.float32))
    der = lucky.Derotator((240, 320), (160.0, 120.0), 90.0, "jupiter", 0.0, 1)
    fx, fy = der.field(900.0)
    yy, xx = np.mgrid[0:240, 0:320].astype(np.float32)
    back = cv2.remap(t1.astype(np.float32), xx + fx, yy + fy, cv2.INTER_LINEAR) * der.gain(900.0)
    inner = cv2.erode(mask.astype(np.uint8), np.ones((25, 25))) > 0
    before = np.mean(np.abs(t1 - t0)[inner])
    after = np.mean(np.abs(back - t0)[inner])
    assert after < before * 0.5
