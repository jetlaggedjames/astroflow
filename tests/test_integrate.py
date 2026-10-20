import numpy as np
import pytest

from astroflow.imaging.integrate import _nanmedian, combine, reject


def test_fast_nanmedian_matches_numpy():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(9, 20, 30)).astype(np.float32)
    a[rng.random(a.shape) < 0.2] = np.nan
    a[:, 0, 0] = np.nan
    ref = np.nanmedian(a, axis=0)
    got = _nanmedian(a)
    assert np.allclose(np.nan_to_num(got, nan=-9), np.nan_to_num(ref, nan=-9), atol=1e-6)


@pytest.mark.parametrize("method", ["sigma", "winsorized", "linear_fit", "percentile", "minmax"])
def test_rejection_removes_satellite_trail(method):
    rng = np.random.default_rng(1)
    n = 30 if method == "linear_fit" else 12
    stack = (0.1 + rng.normal(0, 0.01, (n, 50, 50))).astype(np.float32)
    stack[3, 25, :] += 0.6      # a satellite trail in one frame
    out, rlo, rhi = combine(stack, None, method)
    assert abs(float(out[25].mean()) - 0.1) < 0.01
    assert rhi[25].sum() >= 45 if method != "minmax" else True


def test_weights_and_nan_coverage():
    stack = np.array([np.full((4, 4), 1.0), np.full((4, 4), 3.0)], np.float32)
    stack[1, 0, 0] = np.nan
    out, _, _ = combine(stack, np.array([3.0, 1.0]), "none")
    assert np.isclose(out[1, 1], (3 * 1 + 1 * 3) / 4)
    assert np.isclose(out[0, 0], 1.0)
