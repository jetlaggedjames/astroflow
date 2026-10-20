"""Minimal reader for PixInsight XISF images (uncompressed, zlib, lz4 if available).

XISF = 8-byte signature "XISF0100", 4-byte header length, 4 reserved bytes,
an XML header, then attached data blocks. We read the first <Image>.
"""
from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import numpy as np

_SAMPLE = {"UInt8": np.uint8, "UInt16": np.uint16, "UInt32": np.uint32,
           "Float32": np.float32, "Float64": np.float64}


def _parse_header(path: Path) -> tuple[ET.Element, dict]:
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig != b"XISF0100":
            raise ValueError("Not an XISF file")
        (hlen,) = struct.unpack("<I", f.read(4))
        f.read(4)
        xml = f.read(hlen)
    root = ET.fromstring(xml)
    ns = {"x": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    img = root.find("x:Image", ns) if ns else root.find("Image")
    if img is None:
        raise ValueError("XISF file has no image")
    meta: dict = {}
    for kw in img.iter():
        tag = kw.tag.split("}")[-1]
        if tag == "FITSKeyword":
            name = kw.attrib.get("name", "")
            val = kw.attrib.get("value", "").strip().strip("'").strip()
            if name and name not in meta:
                meta[name] = val
    return img, meta


def read_header(path: str | Path) -> dict:
    img, meta = _parse_header(Path(path))
    geo = [int(x) for x in img.attrib["geometry"].split(":")]
    meta["_width"], meta["_height"] = geo[0], geo[1]
    meta["_channels"] = geo[2] if len(geo) > 2 else 1
    return meta


def read(path: str | Path) -> tuple[np.ndarray, dict]:
    path = Path(path)
    img, meta = _parse_header(path)
    geo = [int(x) for x in img.attrib["geometry"].split(":")]
    w, h = geo[0], geo[1]
    c = geo[2] if len(geo) > 2 else 1
    dtype = np.dtype(_SAMPLE[img.attrib.get("sampleFormat", "UInt16")])
    loc = img.attrib["location"].split(":")
    if loc[0] != "attachment":
        raise ValueError("Only attached XISF data blocks are supported")
    pos, size = int(loc[1]), int(loc[2])
    with open(path, "rb") as f:
        f.seek(pos)
        raw = f.read(size)
    comp = img.attrib.get("compression")
    if comp:
        parts = comp.split(":")
        codec = parts[0]
        shuffled = codec.endswith("+sh")
        codec = codec.replace("+sh", "")
        if codec == "zlib":
            raw = zlib.decompress(raw)
        elif codec in ("lz4", "lz4hc"):
            try:
                import lz4.block  # type: ignore
            except ImportError as e:
                raise ValueError("This XISF uses LZ4 compression; install the 'lz4' package") from e
            raw = lz4.block.decompress(raw, uncompressed_size=int(parts[1]))
        else:
            raise ValueError(f"Unsupported XISF compression: {codec}")
        if shuffled:
            item = int(parts[2]) if len(parts) > 2 else dtype.itemsize
            arr = np.frombuffer(raw, np.uint8)
            n = len(arr) // item
            raw = arr[: n * item].reshape(item, n).T.copy().tobytes() + arr[n * item:].tobytes()
    data = np.frombuffer(raw, dtype=dtype.newbyteorder("<"))
    if img.attrib.get("pixelStorage", "Planar") == "Planar":
        data = data.reshape(c, h, w)
        data = data[0] if c == 1 else np.moveaxis(data, 0, -1)
    else:
        data = data.reshape(h, w, c) if c > 1 else data.reshape(h, w)
    cfa = img.find(".//{*}ColorFilterArray")
    if cfa is not None and "pattern" in cfa.attrib:
        meta.setdefault("BAYERPAT", cfa.attrib["pattern"])
    return np.ascontiguousarray(data), meta
