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


def _write_xisf(path, data, sample, compression=None, shuffle=False, keywords=None, cfa=None):
    """Tiny XISF writer for tests (PixInsight layout: signature, header length, XML, attached block)."""
    import struct
    import zlib
    planar = np.moveaxis(data, -1, 0) if data.ndim == 3 else data[None]
    block = np.ascontiguousarray(planar).astype(planar.dtype.newbyteorder("<")).tobytes()
    comp_attr = ""
    if compression:
        raw = block
        if shuffle:
            item = planar.dtype.itemsize
            arr = np.frombuffer(raw, np.uint8)
            raw = arr.reshape(-1, item).T.copy().tobytes()
        block = zlib.compress(raw)
        comp_attr = f' compression="zlib{"+sh" if shuffle else ""}:{len(raw)}{f":{planar.dtype.itemsize}" if shuffle else ""}"'
    kws = "".join(f'<FITSKeyword name="{k}" value="{v}" comment=""/>' for k, v in (keywords or {}).items())
    cfa_el = f'<ColorFilterArray pattern="{cfa}" width="2" height="2"/>' if cfa else ""
    c = planar.shape[0]
    h, w = planar.shape[1:]

    def xml(pos):
        return (f'<?xml version="1.0" encoding="UTF-8"?><xisf version="1.0" xmlns="http://www.pixinsight.com/xisf">'
                f'<Image geometry="{w}:{h}:{c}" sampleFormat="{sample}" colorSpace="{"RGB" if c == 3 else "Gray"}" '
                f'location="attachment:{pos}:{len(block)}"{comp_attr}>{kws}{cfa_el}</Image></xisf>').encode()
    pos = 16 + len(xml(10**9)) + 64
    head = xml(pos).ljust(pos - 16, b" ")
    with open(path, "wb") as f:
        f.write(b"XISF0100" + struct.pack("<I", len(head)) + b"\0" * 4 + head + block)


def test_xisf_read_plain_compressed_and_cfa(tmp_path):
    from astroflow.io import xisf
    from astroflow.io.images import read_header, read_image
    rng = np.random.default_rng(5)
    mono = (rng.random((30, 40)) * 65535).astype(np.uint16)
    p1 = tmp_path / "mono.xisf"
    _write_xisf(p1, mono, "UInt16", keywords={"EXPTIME": "12.5", "OBJECT": "'M 51'"}, cfa="RGGB")
    img, meta = xisf.read(p1)
    assert np.array_equal(img, mono) and meta["EXPTIME"] == "12.5" and meta["OBJECT"] == "M 51" and meta["BAYERPAT"] == "RGGB"
    rgb = rng.random((24, 32, 3)).astype(np.float32)
    p2 = tmp_path / "rgb.xisf"
    _write_xisf(p2, rgb, "Float32", compression="zlib", shuffle=True)
    img2, _ = xisf.read(p2)
    assert img2.shape == (24, 32, 3) and np.allclose(img2, rgb)
    hdr = read_header(p2)
    assert (hdr["_width"], hdr["_height"], hdr["_channels"]) == (32, 24, 3)
    img3, _ = read_image(p2)            # through the general loader the app uses
    assert img3.shape == (24, 32, 3)
