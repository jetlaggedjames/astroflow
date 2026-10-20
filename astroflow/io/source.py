"""One interface for every kind of capture: SER/AVI video or a list of image files.

Frames are always fetched one at a time (``get_raw``/``get_float``), so even a
huge 18 GB video never has to fit in memory.
"""
from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

from .debayer import bayer_luminance, debayer
from .images import IMAGE_EXT, kind_of, read_header, read_image, to_float
from .ser import SerReader


class FrameSource:
    """Base class. Subclasses implement ``_raw(i)`` and set the attributes below."""

    count: int = 0
    width: int = 0
    height: int = 0
    bayer: str | None = None   # "RGGB" etc. when the raw frames are a colour mosaic
    channels: int = 1          # 3 when frames are already RGB
    bit_depth: int = 8
    meta: dict
    paths: list[Path]

    def __len__(self) -> int:
        return self.count

    @property
    def is_color(self) -> bool:
        return self.channels == 3 or self.bayer is not None

    def get_raw(self, i: int) -> np.ndarray:
        raise NotImplementedError

    def get_float(self, i: int, debayer_method: str = "bilinear") -> np.ndarray:
        """Frame i as float32 0..1; RGB (H,W,3) for colour data."""
        raw = self.get_raw(i)
        if self.bayer and raw.ndim == 2:
            raw = debayer(raw, self.bayer, debayer_method)
        return to_float(raw)

    def get_mono(self, i: int, half: bool = False) -> np.ndarray:
        """Fast luminance float32 frame for scoring/alignment.

        For Bayer data, ``half=True`` returns a 2x2-binned luminance (quick, noise-robust).
        """
        raw = self.get_raw(i)
        if self.bayer and raw.ndim == 2:
            if half:
                lum = bayer_luminance(raw)
            else:
                lum = cv2.blur(raw.astype(np.float32), (2, 2))
        elif raw.ndim == 3:
            lum = raw.astype(np.float32).mean(axis=2)
        else:
            lum = raw.astype(np.float32)
        scale = 255.0 if raw.dtype == np.uint8 else 65535.0 if raw.dtype == np.uint16 else 1.0
        return lum / scale

    def frame_time(self, i: int) -> dt.datetime | None:
        return None

    def frame_meta(self, i: int) -> dict:
        return dict(self.meta)

    def name(self, i: int) -> str:
        return f"frame {i}"

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def describe(self) -> dict:
        return {"count": self.count, "width": self.width, "height": self.height, "bayer": self.bayer,
                "channels": self.channels, "bit_depth": self.bit_depth, "is_color": self.is_color,
                "paths": [str(p) for p in self.paths[:5]], "n_paths": len(self.paths)}


class SerSource(FrameSource):
    def __init__(self, path: Path, bayer_override: str | None = None):
        self.paths = [Path(path)]
        self.reader = SerReader(path)
        h = self.reader.header
        self.count = self.reader.frame_count
        self.width, self.height = h.width, h.height
        self.channels = h.planes
        self.bayer = bayer_override or (h.color if h.is_bayer else None)
        self.bit_depth = h.bit_depth
        self.meta = read_header(path)
        if self.bayer:
            self.meta["BAYERPAT"] = self.bayer

    def get_raw(self, i: int) -> np.ndarray:
        # A fresh buffer per call: callers may keep frames, and freed buffers are reused by numpy.
        return np.ascontiguousarray(self.reader.read_frame(i))

    def frame_time(self, i: int) -> dt.datetime | None:
        return self.reader.frame_time(i)

    def close(self) -> None:
        self.reader.close()


class VideoSource(FrameSource):
    """AVI/MP4 via OpenCV. Sequential reads are fast; random access seeks."""

    def __init__(self, path: Path):
        self.paths = [Path(path)]
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise ValueError(f"Cannot open video {path}")
        self.count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.channels = 3
        self.bit_depth = 8
        self.meta = read_header(path)
        self._pos = 0
        self._lock = threading.Lock()

    def get_raw(self, i: int) -> np.ndarray:
        with self._lock:
            if i != self._pos:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, frame = self.cap.read()
            self._pos = i + 1
        if not ok:
            raise IOError(f"Cannot read frame {i}")
        if frame.ndim == 3 and np.array_equal(frame[..., 0], frame[..., 2]) and np.array_equal(frame[..., 0], frame[..., 1]):
            return frame[..., 0].copy()
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def close(self) -> None:
        self.cap.release()


class FileListSource(FrameSource):
    """A sequence of still images (FITS subs, TIFFs, RAWs...)."""

    def __init__(self, paths: Sequence[Path], bayer_override: str | None = None):
        self.paths = [Path(p) for p in paths]
        if not self.paths:
            raise ValueError("No image files given")
        self.count = len(self.paths)
        first, meta = read_image(self.paths[0])
        self.height, self.width = first.shape[:2]
        self.channels = 3 if first.ndim == 3 else 1
        pat = bayer_override or meta.get("BAYERPAT") or meta.get("COLORTYP")
        self.bayer = str(pat).upper().strip() if pat and str(pat).upper().strip() in ("RGGB", "BGGR", "GRBG", "GBRG") and first.ndim == 2 else None
        self.bit_depth = 8 if first.dtype == np.uint8 else 16 if first.dtype == np.uint16 else 32
        self.meta = meta
        self._metas: dict[int, dict] = {0: meta}
        self._lock = threading.Lock()

    def get_raw(self, i: int) -> np.ndarray:
        data, meta = read_image(self.paths[i])
        with self._lock:
            self._metas[i] = meta
        if data.shape[:2] != (self.height, self.width):
            raise ValueError(f"{self.paths[i].name} has a different size from the first frame")
        return data

    def frame_meta(self, i: int) -> dict:
        with self._lock:
            if i in self._metas:
                return self._metas[i]
        m = read_header(self.paths[i])
        with self._lock:
            self._metas[i] = m
        return m

    def frame_time(self, i: int) -> dt.datetime | None:
        s = self.frame_meta(i).get("DATE-OBS")
        if not s:
            return None
        try:
            t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None

    def name(self, i: int) -> str:
        return self.paths[i].name


def expand_paths(items: Sequence[str | Path]) -> list[Path]:
    """Folders become their image files (sorted); files pass through."""
    out: list[Path] = []
    for it in items:
        p = Path(it)
        if p.is_dir():
            out.extend(sorted(q for q in p.iterdir() if q.suffix.lower() in IMAGE_EXT and q.is_file()))
        elif p.exists():
            out.append(p)
    return out


def open_source(items: str | Path | Sequence[str | Path], bayer_override: str | None = None) -> FrameSource:
    if isinstance(items, (str, Path)):
        items = [items]
    paths = expand_paths(items)
    if not paths:
        raise FileNotFoundError("No readable files found")
    if len(paths) == 1:
        k = kind_of(paths[0])
        if k == "ser":
            return SerSource(paths[0], bayer_override)
        if k == "video":
            return VideoSource(paths[0])
    vids = [p for p in paths if kind_of(p) in ("ser", "video")]
    if vids:
        raise ValueError("Open one video at a time (or a set of still images)")
    return FileListSource(paths, bayer_override)
