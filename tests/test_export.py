"""Export: formats, sizes, colour profiles, watermark, caption, labels, DPI."""
import io

import numpy as np
import pytest
from PIL import Image, ImageCms

from astroflow import export
from astroflow.io.images import read_image
from astroflow.sim.scenes import render_sky
from astroflow.solve.wcs import Solution


@pytest.fixture()
def img():
    yy, xx = np.mgrid[0:300, 0:400].astype(np.float32)
    return np.dstack([xx / 400, yy / 300, 0.5 * np.ones_like(xx)]).astype(np.float32)


@pytest.mark.parametrize("fmt,bits", [("tiff", 8), ("tiff", 16), ("tiff", 32), ("png", 8), ("png", 16), ("jpg", 8),
                                      ("webp", 8), ("fits", 16), ("fits", 32)])
def test_formats_roundtrip(tmp_path, img, fmt, bits):
    r = export.export_image(img, tmp_path / "out", {"format": fmt, "bits": bits, "quality": 95}, {"OBJECT": "Test"})
    data, meta = read_image(r["path"])
    assert data.shape[:2] == (300, 400)
    if fmt in ("tiff", "png") and bits == 16:
        assert data.dtype == np.uint16
    if fmt == "tiff" and bits == 32:
        assert data.dtype == np.float32


def test_adobe_rgb_profile_is_valid_icc(tmp_path, img):
    icc = export.adobe_rgb_icc()
    prof = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    assert "Adobe RGB" in ImageCms.getProfileDescription(prof)
    r = export.export_image(img, tmp_path / "a.jpg", {"format": "jpg", "profile": "adobergb"})
    with Image.open(r["path"]) as im:
        assert im.info.get("icc_profile") == icc
    r = export.export_image(img, tmp_path / "s.png", {"format": "png", "profile": "srgb"})
    with Image.open(r["path"]) as im:
        assert "sRGB" in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(im.info["icc_profile"])))


@pytest.mark.parametrize("opts,expect", [({"size": "percent", "percent": 50}, (150, 200)),
                                         ({"size": "custom", "width": 200}, (150, 200)),
                                         ({"size": "instagram_square"}, (1080, 1080)),
                                         ({"size": "4k_wallpaper"}, (2160, 3840)),
                                         ({"size": "forum"}, (300, 400))])
def test_sizes(tmp_path, img, opts, expect):
    r = export.export_image(img, tmp_path / "x.png", {"format": "png", **opts})
    with Image.open(r["path"]) as im:
        assert (im.height, im.width) == expect


def test_print_size_sets_dpi(tmp_path, img):
    r = export.export_image(img, tmp_path / "p.jpg", {"format": "jpg", "size": "print_8x10"})
    with Image.open(r["path"]) as im:
        assert (im.width, im.height) == (3000, 2400)
        assert round(im.info["dpi"][0]) == 300


def test_watermark_caption_and_labels(tmp_path):
    sky, hdr = render_sky(83.82, -5.39, 9.0, 0, 800, 600, fwhm=2.5)
    sky = np.repeat(np.clip(sky * 5, 0, 1)[..., None], 3, axis=2)
    sol = Solution.from_header(hdr, 800, 600).to_dict()
    opts = {"format": "png", "annotate": True, "compass": True, "scale_bar": True, "caption": True,
            "watermark": {"enabled": True, "type": "text", "text": "(c) Me", "position": "bottom-right", "size": 4, "opacity": 0.8}}
    r = export.export_image(sky, tmp_path / "ann.png", opts, {"OBJECT": "M 42", "EXPTIME": 10, "NCOMBINE": 30}, sol)
    data, _ = read_image(r["path"])
    assert data.shape[0] > 600 and data.shape[1] == 800       # caption bar added
    plain = (np.clip(sky, 0, 1) * 255).astype(np.uint8)
    assert np.mean(np.abs(data[:600].astype(int) - plain.astype(int))) > 0.5   # labels drawn


def test_watermark_presets():
    export.save_watermark("Mine", {"type": "text", "text": "AstroFlow user"})
    assert export.list_watermarks()["Mine"]["text"] == "AstroFlow user"
    export.delete_watermark("Mine")
    assert "Mine" not in export.list_watermarks()
