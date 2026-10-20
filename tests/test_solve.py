"""Plate solving: WCS conventions, built-in solver, ASTAP (if available), annotation, ASTAP output parsing."""
import os
from pathlib import Path

import numpy as np
import pytest

from astroflow.astro.coords import angular_separation
from astroflow.sim.scenes import render_sky
from astroflow.solve import annotate, astap, builtin
from astroflow.solve.wcs import Solution, tan_header

ASTAP_DIR = os.environ.get("ASTROFLOW_TEST_ASTAP")


@pytest.mark.parametrize("rot", [0, 30, 90, 200, 359])
@pytest.mark.parametrize("flipped", [False, True])
def test_wcs_orientation_roundtrip(rot, flipped):
    s = Solution.from_header(tan_header(83.8, -5.4, 1.5, rot, 800, 600, flipped), 800, 600)
    assert abs(((s.rotation_deg - rot + 180) % 360) - 180) < 1e-3
    assert s.flipped == flipped
    assert abs(s.scale_arcsec - 1.5) < 1e-9


@pytest.mark.parametrize("ra,dec,rot,flip", [(83.8, -5.4, 25, False), (279.2, 38.8, 130, True)])
def test_builtin_solver_with_hint(ra, dec, rot, flip):
    img, hdr = render_sky(ra, dec, 10.5, rot, 1200, 900, fwhm=2.8, flipped=flip)
    s = builtin.solve(img, {"ra": ra + 0.7, "dec": dec - 0.4}, 10.5, blind=False)
    assert s is not None
    assert angular_separation(s.ra, s.dec, ra, dec) * 3600 < 5
    assert abs(((s.rotation_deg - rot + 180) % 360) - 180) < 0.1 and s.flipped == flip


@pytest.mark.skipif(not ASTAP_DIR, reason="set ASTROFLOW_TEST_ASTAP to a folder with astap_cli and a star database")
def test_astap_solves_real_star_field():
    d = Path(ASTAP_DIR)
    img, _ = render_sky(10.68, 41.27, 11.0, -70, 1200, 900, fwhm=2.8)
    s = astap.solve(img, d / "cli" / "astap_cli", d / "db", {"ra": 11.2, "dec": 41.0}, 900 * 11 / 3600)
    assert s is not None and angular_separation(s.ra, s.dec, 10.68, 41.27) * 3600 < 10


def test_astap_output_parsing(tmp_path):
    ini = "PLTSOLVD=T\nCRVAL1= 8.38E+001\nCRVAL2=-5.4E+000\nCD1_1=-1.0E-003\nWARNING=\n"
    info = astap.parse_ini(ini)
    assert info["PLTSOLVD"] is True and abs(info["CRVAL1"] - 83.8) < 1e-9
    cards = ["SIMPLE  =                    T", "CRVAL1  =                 83.8 / ra", "CRVAL2  =                 -5.4",
             "CTYPE1  = 'RA---TAN'", "CD1_1   =               -0.001", "END"]
    p = tmp_path / "x.wcs"
    p.write_bytes("".join(c.ljust(80) for c in cards).encode())
    h = astap.parse_wcs_file(p)
    assert h["CRVAL1"] == 83.8 and h["CTYPE1"] == "RA---TAN" and h["CD1_1"] == -0.001


def test_database_recommendation_for_users_scope():
    # 300P (1500 mm) + Uranus-C (3856x2180, 2.9 um): ~0.43 x 0.24 deg
    rec = astap.recommend_database(0.43, 0.24)
    assert rec["name"] in ("d80", "d50")
    assert astap.recommend_database(0.14, 0.08)["name"] is None   # with the 3x Barlow: too narrow


def test_annotation_finds_m42():
    img, hdr = render_sky(83.82, -5.39, 6.0, 15, 1200, 900, fwhm=2.5)
    sol = Solution.from_header(hdr, 1200, 900)
    items = annotate.objects_in_image(sol)
    labels = " ".join(i["label"] for i in items)
    assert "M 42" in labels
    m42 = next(i for i in items if "M 42" in i["label"])
    assert abs(m42["x"] - 600) < 30 and abs(m42["y"] - 450) < 30
    out = annotate.draw_overlay(img, items, sol)
    assert out.shape == (900, 1200, 3) and out.dtype == np.uint8
    sb = annotate.scale_bar(sol.scale_arcsec, 1200)
    assert sb["pixels"] > 50
