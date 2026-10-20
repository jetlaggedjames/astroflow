"""Post-processing tools: every tool runs; key tools do what they claim."""
import cv2
import numpy as np
import pytest

from astroflow.imaging.stars import detect_stars, measure_summary
from astroflow.process.masks import build_mask, rasterize_strokes
from astroflow.process.ops import REGISTRY, Ctx, run_op
from astroflow.process.pipeline import (Renderer, auto_pipeline, delete_preset, list_presets, load_preset,
                                        make_step, save_preset)
from astroflow.sim.scenes import PlanetScene, StarFieldScene


@pytest.fixture(scope="module")
def images():
    planet = PlanetScene("jupiter", (120, 160), 40).render(0)
    sky = StarFieldScene((160, 200), n_stars=120, seed=2).render(0, 10)
    return {"planet": planet, "dso": sky, "mono": sky.mean(axis=2)}


@pytest.mark.parametrize("op_id", sorted(REGISTRY))
def test_every_op_runs(images, op_id):
    for img in images.values():
        out = run_op(op_id, img, {}, Ctx())
        assert np.all(np.isfinite(out))
        assert out.ndim == img.ndim


def _blurred_stars(sigma):
    rng = np.random.default_rng(0)
    img = np.full((200, 200), 0.02, np.float32)
    for x, y in rng.uniform(15, 185, (40, 2)):
        img[int(y), int(x)] += 1.0
    return cv2.GaussianBlur(img, (0, 0), sigma) * 8 + rng.normal(0, 0.0005, img.shape).astype(np.float32)


def test_deconvolution_shrinks_stars():
    img = _blurred_stars(2.0)
    before = measure_summary(detect_stars(img))["fwhm"]
    out = run_op("deconvolution", img, {"psf": "gaussian", "fwhm": 2.0 * 2.3548, "iterations": 15, "regularization": 0.0,
                                         "protect_limb": 0}, Ctx())
    after = measure_summary(detect_stars(out))["fwhm"]
    assert after < before * 0.8


def test_wavelets_add_fine_detail(images):
    img = cv2.GaussianBlur(images["planet"], (0, 0), 1.5)
    out = run_op("wavelets", img, {"l2": 10, "protect_limb": 0}, Ctx())
    hp = lambda a: np.abs(a - cv2.GaussianBlur(a, (0, 0), 2)).mean()  # noqa: E731
    assert hp(out) > hp(img) * 1.5


def test_background_extraction_flattens_gradient():
    yy, xx = np.mgrid[0:150, 0:200].astype(np.float32)
    img = 0.05 + 0.1 * xx / 200 + np.random.default_rng(1).normal(0, 0.002, (150, 200)).astype(np.float32)
    out = run_op("background_extraction", img, {}, Ctx())
    assert abs(out[:, :20].mean() - out[:, -20:].mean()) < 0.005


def test_colour_calibration_neutralises_background(images):
    img = images["dso"].copy()
    img[..., 2] += 0.05  # blue cast
    out = run_op("color_calibration", img, {"method": "background"}, Ctx())
    med = np.median(out.reshape(-1, 3), axis=0)
    assert np.ptp(med) < 0.005


def test_auto_stretch_sets_background_level(images):
    out = run_op("auto_stretch", images["dso"] * 0.1, {"target": 0.2}, Ctx())
    assert 0.12 < float(np.median(out)) < 0.3


def test_luminance_mask_and_brush():
    img = np.linspace(0, 1, 100, dtype=np.float32)[None].repeat(50, 0)
    m = build_mask({"type": "luminance", "params": {"low": 0.5, "high": 1.0, "softness": 0.01}}, img)
    assert m[:, 10].max() == 0 and m[:, 90].min() == 1
    b = rasterize_strokes([{"points": [[0.5, 0.5]], "radius": 0.1, "hardness": 1.0}], (50, 100))
    assert b[25, 50] > 0.9 and b[0, 0] == 0


def test_renderer_reuses_cached_steps(images):
    r = Renderer()
    steps = [make_step("background_extraction"), make_step("auto_stretch"), make_step("saturation")]
    r.render(images["dso"], "k", steps)
    steps[2]["params"]["saturation"] = 0.9
    _, info = r.render(images["dso"], "k", steps)
    assert [t["cached"] for t in info["timings"]] == [True, True, False]


def test_masked_step_only_changes_masked_area():
    img = np.full((40, 40, 3), 0.3, np.float32)
    steps = [make_step("white_balance", {"r": 2.0}, mask="m")]
    masks = {"m": {"type": "brush", "strokes": [{"points": [[0.25, 0.5]], "radius": 0.1, "hardness": 1.0}]}}
    out, _ = Renderer().render(img, "x", steps, masks)
    assert out[20, 10, 0] > 0.5 and abs(out[20, 35, 0] - 0.3) < 1e-6


def test_presets_roundtrip():
    names = [p["name"] for p in list_presets()]
    assert "Jupiter default" in names
    save_preset("My test preset", [make_step("gamma", {"gamma": 1.3})], "planet")
    p = load_preset("My test preset")
    assert p["steps"][0]["params"]["gamma"] == 1.3
    delete_preset("My test preset")
    assert "My test preset" not in [x["name"] for x in list_presets()]


@pytest.mark.parametrize("target", ["planet", "moon", "dso"])
def test_auto_pipelines_run(images, target):
    img = images["dso"] if target == "dso" else images["planet"]
    out, info = Renderer().render(img, target, auto_pipeline(target))
    assert np.all(np.isfinite(out)) and out.max() > 0.3
