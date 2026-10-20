"""Reading and writing still images in every format the app understands.

Internally, processed images are float32 arrays scaled 0..1, shaped (H, W)
for mono or (H, W, 3) for RGB. Raw camera data keeps its integer type until
it is calibrated/debayered.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import warnings
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .. import SOFTWARE_ID

log = logging.getLogger("astroflow.io")

FITS_EXT = {".fits", ".fit", ".fts"}
TIFF_EXT = {".tif", ".tiff"}
STD_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
XISF_EXT = {".xisf"}
RAW_EXT = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw"}
VIDEO_EXT = {".ser", ".avi", ".mp4", ".mov", ".mkv"}
IMAGE_EXT = FITS_EXT | TIFF_EXT | STD_EXT | XISF_EXT | RAW_EXT
ALL_EXT = IMAGE_EXT | VIDEO_EXT


def kind_of(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".ser":
        return "ser"
    if ext in VIDEO_EXT:
        return "video"
    if ext in FITS_EXT:
        return "fits"
    if ext in TIFF_EXT:
        return "tiff"
    if ext in XISF_EXT:
        return "xisf"
    if ext in RAW_EXT:
        return "raw"
    if ext in STD_EXT:
        return ext.lstrip(".").replace("jpeg", "jpg")
    return "other"


# ---------------------------------------------------------------- reading ---

def read_image(path: str | Path) -> tuple[np.ndarray, dict]:
    """Read pixel data + a metadata dict (FITS-style upper-case keys)."""
    path = Path(path)
    k = kind_of(path)
    if k == "fits":
        return _read_fits(path)
    if k == "tiff":
        return _read_tiff(path)
    if k == "xisf":
        from . import xisf
        return xisf.read(path)
    if k == "raw":
        return _read_raw(path)
    if k in ("png", "jpg", "webp", "bmp"):
        data = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if data is None:
            from PIL import Image
            data = np.asarray(Image.open(path))
        elif data.ndim == 3:
            data = cv2.cvtColor(data, cv2.COLOR_BGRA2RGB if data.shape[2] == 4 else cv2.COLOR_BGR2RGB)
        return data, _sidecar(path)
    if k in ("ser", "video"):
        from .source import open_source
        src = open_source(path)
        try:
            return src.get_raw(0), dict(src.meta)
        finally:
            src.close()
    raise ValueError(f"Unsupported file type: {path.suffix}")


def _sidecar(path: Path) -> dict:
    """Metadata we wrote next to a file (for formats without rich headers)."""
    side = path.with_name(path.name + ".json")
    if side.exists():
        try:
            return {k.upper(): v for k, v in json.loads(side.read_text(encoding="utf-8")).items()}
        except Exception:
            pass
    return {}


def _read_fits(path: Path) -> tuple[np.ndarray, dict]:
    from astropy.io import fits
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with fits.open(path, memmap=False) as hdul:
            hdu = next((h for h in hdul if getattr(h, "data", None) is not None and h.data.ndim >= 2), None)
            if hdu is None:
                raise ValueError("FITS file has no image data")
            data = np.asarray(hdu.data)
            header = _fits_header_dict(hdu.header)
    if data.ndim == 3:
        if data.shape[0] in (1, 3, 4):
            data = np.moveaxis(data[:3], 0, -1) if data.shape[0] >= 3 else data[0]
    if data.dtype.byteorder not in ("=", "|"):
        data = data.astype(data.dtype.newbyteorder("="))
    if data.dtype == np.int16:  # signed data without BZERO: shift to unsigned
        data = (data.astype(np.int32) + 32768).astype(np.uint16)
    # FITS stores the first row at the bottom by convention; keep the
    # orientation software like Siril/SharpCap show (top-down) unless ROWORDER says otherwise.
    if str(header.get("ROWORDER", "TOP-DOWN")).upper() == "BOTTOM-UP":
        data = data[::-1]
    return np.ascontiguousarray(data), header


def _fits_header_dict(h) -> dict:
    out: dict[str, Any] = {}
    for card in h.cards:
        k = card.keyword
        if not k or k in ("COMMENT", "HISTORY", ""):
            continue
        out[k] = card.value
    hist = [str(v) for v in h.get("HISTORY", [])]
    if hist:
        out["HISTORY"] = hist
    return out


def _read_tiff(path: Path) -> tuple[np.ndarray, dict]:
    import tifffile
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        data = page.asarray()
        meta: dict[str, Any] = {}
        desc = page.description or ""
        if desc.startswith("{"):
            try:
                meta = {k.upper(): v for k, v in json.loads(desc).items()}
            except Exception:
                pass
    if data.ndim == 3 and data.shape[0] in (3, 4) and data.shape[-1] not in (3, 4):
        data = np.moveaxis(data, 0, -1)
    if data.ndim == 3 and data.shape[-1] == 4:
        data = data[..., :3]
    meta.update(_sidecar(path))
    return data, meta


def _read_raw(path: Path) -> tuple[np.ndarray, dict]:
    try:
        import rawpy  # optional dependency
    except ImportError as e:
        raise ValueError("Camera RAW files need the optional 'rawpy' package") from e
    with rawpy.imread(str(path)) as raw:
        data = raw.raw_image_visible.copy()
        desc = raw.color_desc.decode()
        pat = "".join(desc[i] for i in raw.raw_pattern.flatten())
        pat = pat.replace("G", "G")
        bits = int(np.ceil(np.log2(max(int(raw.white_level), 2))))
        black = float(np.mean(raw.black_level_per_channel))
    # Scale to full 16-bit so all sources share one range.
    scale = 65535.0 / max(1.0, (2**bits - 1))
    data = np.clip((data.astype(np.float32) - black) * scale, 0, 65535).astype(np.uint16)
    meta = {"BAYERPAT": pat if set(pat) <= set("RGB") and len(pat) == 4 else "RGGB", "BITPIX": 16}
    meta.update(_exif_meta(path))
    return data, meta


def _exif_meta(path: Path) -> dict:
    out: dict[str, Any] = {}
    try:
        from PIL import Image, ExifTags
        with Image.open(path) as im:
            ex = im.getexif()
            tags = {ExifTags.TAGS.get(k, k): v for k, v in ex.items()}
            try:
                sub = ex.get_ifd(0x8769)
                tags.update({ExifTags.TAGS.get(k, k): v for k, v in sub.items()})
            except Exception:
                pass
        if "DateTimeOriginal" in tags or "DateTime" in tags:
            s = str(tags.get("DateTimeOriginal") or tags.get("DateTime"))
            try:
                out["DATE-OBS"] = dt.datetime.strptime(s, "%Y:%m:%d %H:%M:%S").isoformat()
            except ValueError:
                pass
        if "ExposureTime" in tags:
            out["EXPTIME"] = float(tags["ExposureTime"])
        if "ISOSpeedRatings" in tags:
            out["GAIN"] = tags["ISOSpeedRatings"]
        if "Model" in tags:
            out["INSTRUME"] = str(tags["Model"]).strip()
    except Exception:
        pass
    return out


def read_header(path: str | Path) -> dict:
    """Cheap metadata read (no pixel data) for the library indexer.

    Always includes _width, _height, _channels, _frames when they can be known.
    """
    path = Path(path)
    k = kind_of(path)
    meta: dict[str, Any] = {}
    try:
        if k == "ser":
            from .ser import SerReader
            with SerReader(path) as r:
                h = r.header
                meta.update({"_width": h.width, "_height": h.height, "_frames": r.frame_count,
                             "_channels": h.planes, "BAYERPAT": h.color if h.is_bayer else None,
                             "BITPIX": h.bit_depth, "INSTRUME": h.instrument, "TELESCOP": h.telescope,
                             "OBSERVER": h.observer, "_truncated": r.truncated})
                t = r.frame_time(0)
                if t:
                    meta["DATE-OBS"] = t.isoformat()
            meta.update(_sidecar(path))
        elif k == "video":
            cap = cv2.VideoCapture(str(path))
            meta.update({"_width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), "_height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                         "_frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), "_channels": 3})
            cap.release()
            meta.update(_sidecar(path))
        elif k == "fits":
            from astropy.io import fits
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with fits.open(path, memmap=True) as hdul:
                    hdu = next((h for h in hdul if h.header.get("NAXIS", 0) >= 2), hdul[0])
                    meta.update(_fits_header_dict(hdu.header))
            nax = int(meta.get("NAXIS", 0))
            meta["_width"], meta["_height"] = meta.get("NAXIS1"), meta.get("NAXIS2")
            meta["_channels"] = int(meta.get("NAXIS3", 1)) if nax >= 3 else 1
            meta["_frames"] = 1
        elif k == "tiff":
            import tifffile
            with tifffile.TiffFile(path) as tf:
                p = tf.pages[0]
                shape = p.shape
                desc = p.description or ""
            if desc.startswith("{"):
                try:
                    meta.update({kk.upper(): v for kk, v in json.loads(desc).items()})
                except Exception:
                    pass
            if len(shape) == 3 and shape[0] in (3, 4) and shape[-1] not in (3, 4):
                shape = (shape[1], shape[2], shape[0])
            meta.update({"_height": shape[0], "_width": shape[1], "_channels": shape[2] if len(shape) > 2 else 1, "_frames": 1})
            meta.update(_sidecar(path))
        elif k == "xisf":
            from . import xisf
            meta.update(xisf.read_header(path))
            meta["_frames"] = 1
        elif k in ("png", "jpg", "webp", "bmp", "raw"):
            from PIL import Image
            try:
                with Image.open(path) as im:
                    meta.update({"_width": im.width, "_height": im.height,
                                 "_channels": len(im.getbands()), "_frames": 1})
            except Exception:
                pass
            meta.update(_exif_meta(path))
            meta.update(_sidecar(path))
    except Exception as e:  # unreadable/corrupt file: report rather than crash
        meta["_error"] = f"{type(e).__name__}: {e}"
    return meta


# ------------------------------------------------------------- conversion ---

def to_float(data: np.ndarray) -> np.ndarray:
    """Scale any pixel data to float32 0..1."""
    if data.dtype == np.uint8:
        return data.astype(np.float32) / 255.0
    if data.dtype == np.uint16:
        return data.astype(np.float32) / 65535.0
    if data.dtype in (np.uint32, np.int32):
        return (data.astype(np.float64) / 4294967295.0).astype(np.float32)
    out = data.astype(np.float32, copy=True)
    mx = float(np.nanmax(out)) if out.size else 1.0
    if mx > 1.5:  # float data saved in 0..65535 (common for other stackers)
        out /= 65535.0 if mx <= 65535.0 else mx
    np.nan_to_num(out, copy=False, nan=0.0, posinf=1.0, neginf=0.0)
    return out


def to_uint(img: np.ndarray, bits: int) -> np.ndarray:
    img = np.clip(np.nan_to_num(img), 0.0, 1.0)
    if bits == 8:
        return (img * 255.0 + 0.5).astype(np.uint8)
    return (img * 65535.0 + 0.5).astype(np.uint16)


def to_display_u8(img: np.ndarray) -> np.ndarray:
    return to_uint(img, 8)


# ---------------------------------------------------------------- writing ---

def write_fits(path: str | Path, img: np.ndarray, header: dict | None = None, history: list[str] | None = None,
               bits: int = 32) -> Path:
    """Write FITS. bits=32 -> 32-bit float (0..1), bits=16 -> unsigned 16-bit."""
    from astropy.io import fits
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = img
    if bits == 16:
        data = to_uint(img, 16) if img.dtype != np.uint16 else img
    elif data.dtype != np.float32 and np.issubdtype(data.dtype, np.floating):
        data = data.astype(np.float32)
    if data.ndim == 3:
        data = np.moveaxis(data, -1, 0)  # FITS convention for colour: (3, H, W)
    hdu = fits.PrimaryHDU(np.ascontiguousarray(data))
    h = hdu.header
    h["ROWORDER"] = ("TOP-DOWN", "Row order of the image data")
    h["SWCREATE"] = (SOFTWARE_ID, "Software that created this file")
    h["DATE"] = (dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "File creation date (UTC)")
    for k, v in (header or {}).items():
        if k.startswith("_") or k in ("SIMPLE", "BITPIX", "NAXIS", "NAXIS1", "NAXIS2", "NAXIS3", "EXTEND",
                                      "BZERO", "BSCALE", "HISTORY", "COMMENT", "ROWORDER"):
            continue
        if v is None or isinstance(v, (dict, list)):
            continue
        key = str(k).upper()[:8]
        try:
            h[key] = v if not isinstance(v, str) else v[:68]
        except Exception:
            pass
    for line in history or []:
        for i in range(0, len(line), 70):
            h.add_history(line[i:i + 70])
    tmp = path.with_name(path.name + ".part")
    hdu.writeto(tmp, overwrite=True)
    tmp.replace(path)
    return path


def write_tiff(path: str | Path, img: np.ndarray, bits: int = 16, meta: dict | None = None,
               icc_profile: bytes | None = None) -> Path:
    import tifffile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if bits == 32:
        data = np.clip(img, 0, 1).astype(np.float32)
    else:
        data = to_uint(img, bits)
    extratags = []
    if icc_profile:
        extratags.append((34675, 7, len(icc_profile), icc_profile, True))
    desc = json.dumps({k: v for k, v in (meta or {}).items() if _jsonable(v)})
    tmp = path.with_name(path.name + ".part")
    tifffile.imwrite(tmp, data, photometric="rgb" if data.ndim == 3 else "minisblack",
                     description=desc, software=SOFTWARE_ID, extratags=extratags,
                     compression="zlib" if bits != 32 else None, metadata=None)
    tmp.replace(path)
    return path


def _jsonable(v: Any) -> bool:
    try:
        json.dumps(v)
        return True
    except Exception:
        return False


def write_standard(path: str | Path, img: np.ndarray, fmt: str, bits: int = 8, quality: int = 92,
                   icc_profile: bytes | None = None, meta: dict | None = None) -> Path:
    """PNG (8/16-bit), JPG, WebP."""
    from PIL import Image
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = fmt.lower()
    tmp = path.with_name(path.name + ".part")
    if fmt == "png" and bits == 16:
        data = to_uint(img, 16)
        if data.ndim == 3:
            data = cv2.cvtColor(data, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".png", data, [cv2.IMWRITE_PNG_COMPRESSION, 6])
        if not ok:
            raise IOError("PNG encoding failed")
        tmp.write_bytes(buf.tobytes())
    else:
        data = to_uint(img, 8)
        im = Image.fromarray(data)
        kw: dict[str, Any] = {}
        if icc_profile:
            kw["icc_profile"] = icc_profile
        if fmt in ("jpg", "jpeg"):
            if im.mode != "RGB" and im.mode != "L":
                im = im.convert("RGB")
            kw.update(quality=int(quality), subsampling=0 if quality >= 90 else 2, optimize=True)
            im.save(tmp, "JPEG", **kw)
        elif fmt == "webp":
            kw.update(quality=int(quality), method=4, lossless=quality >= 100)
            im.save(tmp, "WEBP", **kw)
        elif fmt == "png":
            from PIL import PngImagePlugin
            info = PngImagePlugin.PngInfo()
            info.add_text("Software", SOFTWARE_ID)
            for k, v in (meta or {}).items():
                if isinstance(v, (str, int, float)):
                    info.add_text(str(k), str(v))
            im.save(tmp, "PNG", pnginfo=info, **kw)
        else:
            raise ValueError(f"Unknown format {fmt}")
    tmp.replace(path)
    return path


def save_any(path: str | Path, img: np.ndarray, bits: int = 16, meta: dict | None = None,
             history: list[str] | None = None, quality: int = 95) -> Path:
    path = Path(path)
    k = kind_of(path)
    if k == "fits":
        return write_fits(path, img, meta, history, bits=32 if bits == 32 else 16)
    if k == "tiff":
        return write_tiff(path, img, bits, {**(meta or {}), "history": history or []})
    return write_standard(path, img, k, bits=bits, quality=quality, meta=meta)
