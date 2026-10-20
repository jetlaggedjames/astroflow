import datetime as dt
import struct

import numpy as np
import pytest

from astroflow.io import ser
from astroflow.io.debayer import debayer
from astroflow.io.images import read_header, read_image, to_float, write_fits, write_standard, write_tiff
from astroflow.io.source import open_source


def make_mosaic(rgb, pattern):
    idx = {"R": 0, "G": 1, "B": 2}
    m = np.zeros(rgb.shape[:2], rgb.dtype)
    for i, ch in enumerate(pattern):
        m[i // 2::2, i % 2::2] = rgb[i // 2::2, i % 2::2, idx[ch]]
    return m


@pytest.mark.parametrize("pattern", ["RGGB", "BGGR", "GRBG", "GBRG"])
def test_debayer_patterns(pattern):
    rgb = np.zeros((32, 32, 3), np.uint16)
    rgb[..., 0], rgb[..., 1], rgb[..., 2] = 50000, 20000, 5000
    out = debayer(make_mosaic(rgb, pattern), pattern)
    assert abs(int(out[16, 16, 0]) - 50000) < 100
    assert abs(int(out[16, 16, 2]) - 5000) < 100


def test_ser_roundtrip_and_timestamps(tmp_path):
    p = tmp_path / "a.ser"
    frames = [np.full((40, 60), i * 1000 + 7, np.uint16) for i in range(5)]
    t0 = dt.datetime(2026, 10, 20, 22, 0, 0, tzinfo=dt.timezone.utc)
    with ser.SerWriter(p, 60, 40, 12, "RGGB", instrument="Sim") as w:
        for i, f in enumerate(frames):
            w.write(f, t0 + dt.timedelta(seconds=i))
    with ser.SerReader(p) as r:
        assert r.frame_count == 5 and r.header.color == "RGGB" and r.header.bit_depth == 12
        assert np.array_equal(r.read_frame(3), frames[3])
        assert r.frame_time(2) == t0 + dt.timedelta(seconds=2)
    assert not (tmp_path / "a.ser.ts").exists()


def test_ser_crash_recovery(tmp_path):
    p = tmp_path / "crash.ser"
    w = ser.SerWriter(p, 32, 16, 8, "MONO")
    for i in range(10):
        w.write(np.full((16, 32), i, np.uint8))
    w._f.flush(); w._ts.flush()
    # Simulate a crash: header count never updated (still 0) and a partial frame at the end.
    w._f.write(b"\x01" * 100)
    w._f.flush()
    # (do not call close)
    r = ser.SerReader(p)
    assert r.frame_count == 10  # reader trusts the data on disk
    r.close()
    info = ser.repair(p)
    assert info["frames"] == 10 and info["timestamps"]
    with ser.SerReader(p) as r:
        assert r.header.frame_count == 10
        assert r.read_frame(9)[0, 0] == 9
        assert r.timestamps() is not None


def test_ser_big_endian_detection(tmp_path):
    p = tmp_path / "be.ser"
    yy, xx = np.mgrid[0:64, 0:64]
    img = (1000 + 30 * xx + 20 * yy).astype(np.uint16)
    with ser.SerWriter(p, 64, 64, 16, "MONO") as w:
        w.write(img)
    # Rewrite pixel data as big-endian to mimic other capture software.
    raw = bytearray(p.read_bytes())
    data = np.frombuffer(bytes(raw[178:178 + 64 * 64 * 2]), "<u2").astype(">u2").tobytes()
    raw[178:178 + len(data)] = data
    p.write_bytes(bytes(raw))
    with ser.SerReader(p) as r:
        assert np.array_equal(r.read_frame(0), img)


def test_fits_tiff_png_roundtrip(tmp_path):
    img = np.random.default_rng(1).random((20, 30, 3)).astype(np.float32)
    write_fits(tmp_path / "x.fits", img, {"OBJECT": "M42", "EXPTIME": 10.0}, ["step one"])
    d, m = read_image(tmp_path / "x.fits")
    assert d.shape == (20, 30, 3) and m["OBJECT"] == "M42" and np.allclose(d, img)
    assert read_header(tmp_path / "x.fits")["_width"] == 30
    write_tiff(tmp_path / "x.tif", img, 16, {"object": "M42"})
    d, m = read_image(tmp_path / "x.tif")
    assert d.dtype == np.uint16 and np.allclose(to_float(d), img, atol=1e-4) and m["OBJECT"] == "M42"
    write_standard(tmp_path / "x.png", img, "png", bits=16)
    d, _ = read_image(tmp_path / "x.png")
    assert d.dtype == np.uint16 and np.allclose(to_float(d), img, atol=1e-4)
    write_standard(tmp_path / "x.jpg", img, "jpg", quality=95)
    assert read_header(tmp_path / "x.jpg")["_width"] == 30


def test_open_source_filelist(tmp_path):
    for i in range(3):
        write_fits(tmp_path / f"s{i}.fits", np.full((8, 10), (i + 1) * 1000, np.uint16), {"BAYERPAT": "RGGB", "DATE-OBS": f"2026-01-01T00:00:0{i}"}, bits=16)
    src = open_source(tmp_path)
    assert src.count == 3 and src.bayer == "RGGB"
    assert src.get_float(1).shape == (8, 10, 3)
    assert src.frame_time(2).second == 2
