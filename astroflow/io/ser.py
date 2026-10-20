"""SER video files (the standard planetary capture format).

Format (SER v3): a 178-byte header, then raw frames back to back, then an
optional trailer with one 64-bit UTC timestamp per frame.

Design rules for huge (18 GB+) files:
  * The reader never loads the whole file. ``read_frame`` reads one frame
    into a reusable buffer, so memory use stays flat whatever the file size.
  * The writer is crash safe: it appends frames, regularly writes the frame
    count back into the header and flushes to disk, and logs timestamps to a
    small side file. If the app or PC crashes, ``repair`` rebuilds a valid
    file from whatever reached the disk.

Byte order: the spec says the "LittleEndian" field is 1 for little-endian
16-bit data, but most capture programs write 0 together with little-endian
data. We write 0 + little-endian (the de-facto convention) and, when reading
16-bit files, detect the real byte order from the data itself.
"""
from __future__ import annotations

import datetime as dt
import os
import struct
import threading
import time
from pathlib import Path

import numpy as np

HEADER_SIZE = 178
FILE_ID = b"LUCAM-RECORDER"

COLOR_IDS = {
    0: "MONO", 8: "RGGB", 9: "GRBG", 10: "GBRG", 11: "BGGR",
    16: "CYYM", 17: "YCMY", 18: "YMCY", 19: "MYYC", 100: "RGB", 101: "BGR",
}
COLOR_CODES = {v: k for k, v in COLOR_IDS.items()}

_EPOCH = dt.datetime(1, 1, 1, tzinfo=dt.timezone.utc)


def datetime_to_ticks(t: dt.datetime) -> int:
    """SER time stamps count 100 ns ticks since 0001-01-01."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    delta = t - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 10_000_000 + delta.microseconds * 10


def ticks_to_datetime(ticks: int) -> dt.datetime | None:
    if ticks <= 0:
        return None
    try:
        return _EPOCH + dt.timedelta(microseconds=ticks // 10)
    except OverflowError:
        return None


class SerHeader:
    def __init__(self, width: int, height: int, bit_depth: int, color: str = "MONO",
                 frame_count: int = 0, observer: str = "", instrument: str = "", telescope: str = "",
                 date_local: int = 0, date_utc: int = 0, lu_id: int = 0, little_endian_flag: int = 0):
        self.width, self.height, self.bit_depth = int(width), int(height), int(bit_depth)
        self.color = color
        self.frame_count = int(frame_count)
        self.observer, self.instrument, self.telescope = observer, instrument, telescope
        self.date_local, self.date_utc = int(date_local), int(date_utc)
        self.lu_id = lu_id
        self.little_endian_flag = little_endian_flag

    @property
    def planes(self) -> int:
        return 3 if self.color in ("RGB", "BGR") else 1

    @property
    def bytes_per_sample(self) -> int:
        return 1 if self.bit_depth <= 8 else 2

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * self.planes * self.bytes_per_sample

    @property
    def is_bayer(self) -> bool:
        return self.color in ("RGGB", "GRBG", "GBRG", "BGGR")

    def pack(self) -> bytes:
        def s40(x: str) -> bytes:
            return x.encode("ascii", "replace")[:40].ljust(40, b"\0")
        return (FILE_ID
                + struct.pack("<iiiiiii", self.lu_id, COLOR_CODES.get(self.color, 0), self.little_endian_flag,
                              self.width, self.height, self.bit_depth, self.frame_count)
                + s40(self.observer) + s40(self.instrument) + s40(self.telescope)
                + struct.pack("<qq", self.date_local, self.date_utc))

    @classmethod
    def unpack(cls, raw: bytes) -> "SerHeader":
        if len(raw) < HEADER_SIZE:
            raise ValueError("File is too small to be a SER video")
        if raw[:14] != FILE_ID:
            # Some programs write other ids; accept anything SER-shaped but warn via exception text.
            if not raw[:14].isascii():
                raise ValueError("Not a SER file (bad file id)")
        lu, color, le, w, h, depth, count = struct.unpack("<iiiiiii", raw[14:42])
        def txt(b: bytes) -> str:
            return b.split(b"\0")[0].decode("latin-1").strip()
        obs, ins, tel = txt(raw[42:82]), txt(raw[82:122]), txt(raw[122:162])
        d_local, d_utc = struct.unpack("<qq", raw[162:178])
        return cls(w, h, depth, COLOR_IDS.get(color, "MONO"), count, obs, ins, tel, d_local, d_utc, lu, le)


class SerReader:
    """Random-access, streaming SER reader. Thread-safe."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._f = open(self.path, "rb", buffering=0)
        self._lock = threading.Lock()
        self.header = SerHeader.unpack(self._f.read(HEADER_SIZE))
        h = self.header
        if h.width <= 0 or h.height <= 0 or h.bit_depth <= 0:
            raise ValueError("SER header has invalid image size")
        size = self.path.stat().st_size
        available = max(0, (size - HEADER_SIZE) // h.frame_bytes)
        # A crashed recording may have a stale count; trust what is on disk.
        self.frame_count = min(h.frame_count, available) if h.frame_count > 0 else available
        self.truncated = h.frame_count > available
        self._has_trailer = size >= HEADER_SIZE + h.frame_count * h.frame_bytes + 8 * h.frame_count and h.frame_count > 0
        self._dtype = np.dtype(np.uint8) if h.bytes_per_sample == 1 else np.dtype("<u2")
        if h.bytes_per_sample == 2:
            self._dtype = self._detect_byte_order()
        self._timestamps: np.ndarray | None = None

    # -- properties -------------------------------------------------------
    @property
    def shape(self) -> tuple[int, ...]:
        h = self.header
        return (h.height, h.width, 3) if h.planes == 3 else (h.height, h.width)

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(np.uint8) if self.header.bytes_per_sample == 1 else np.dtype(np.uint16)

    def __len__(self) -> int:
        return self.frame_count

    # -- reading ----------------------------------------------------------
    def _read_raw(self, index: int, buf: np.ndarray) -> None:
        h = self.header
        with self._lock:
            self._f.seek(HEADER_SIZE + index * h.frame_bytes)
            n = self._f.readinto(memoryview(buf).cast("B"))
        if n != h.frame_bytes:
            raise IOError(f"Frame {index} is incomplete ({n} of {h.frame_bytes} bytes)")

    def read_frame(self, index: int, out: np.ndarray | None = None) -> np.ndarray:
        """Return frame ``index`` as uint8/uint16, shape (H,W) or (H,W,3) in RGB order."""
        if not 0 <= index < self.frame_count:
            raise IndexError(index)
        h = self.header
        if out is None or out.shape != self.shape or out.dtype != self._dtype:
            out = np.empty(self.shape, dtype=self._dtype)
        self._read_raw(index, out)
        if h.color == "BGR":
            out = out[..., ::-1]
        if self._dtype.byteorder == ">":
            out = out.astype(np.uint16)
        return out

    def _detect_byte_order(self) -> np.dtype:
        """Pick little/big endian for 16-bit data by which looks like a real image."""
        if self.frame_count == 0:
            return np.dtype("<u2")
        buf = np.empty(self.shape, dtype="<u2")
        self._read_raw(min(self.frame_count - 1, self.frame_count // 2), buf)
        sample = buf[:: max(1, buf.shape[0] // 256), :: max(1, buf.shape[1] // 256)].astype(np.float64)
        swapped = buf.byteswap()[:: max(1, buf.shape[0] // 256), :: max(1, buf.shape[1] // 256)].astype(np.float64)

        def roughness(a: np.ndarray) -> float:
            return float(np.mean(np.abs(np.diff(a, axis=1)))) / (float(np.mean(np.abs(a - a.mean()))) + 1e-9)

        return np.dtype("<u2") if roughness(sample) <= roughness(swapped) else np.dtype(">u2")

    def timestamps(self) -> np.ndarray | None:
        """UTC timestamps (100 ns ticks) per frame, if the file has them."""
        if self._timestamps is not None:
            return self._timestamps
        h = self.header
        if not self._has_trailer:
            side = Path(str(self.path) + ".ts")
            if side.exists():
                ts = np.fromfile(side, dtype="<i8")
                self._timestamps = ts[: self.frame_count] if len(ts) >= self.frame_count else None
            return self._timestamps
        with self._lock:
            self._f.seek(HEADER_SIZE + h.frame_count * h.frame_bytes)
            raw = self._f.read(8 * h.frame_count)
        ts = np.frombuffer(raw, dtype="<i8")
        if len(ts) == h.frame_count and np.any(ts > 0):
            self._timestamps = ts[: self.frame_count].copy()
        return self._timestamps

    def frame_time(self, index: int) -> dt.datetime | None:
        ts = self.timestamps()
        if ts is not None and index < len(ts):
            return ticks_to_datetime(int(ts[index]))
        return ticks_to_datetime(self.header.date_utc)

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SerWriter:
    """Crash-safe incremental SER writer."""

    SYNC_EVERY_S = 1.0

    def __init__(self, path: str | os.PathLike, width: int, height: int, bit_depth: int, color: str = "MONO",
                 observer: str = "", instrument: str = "", telescope: str = "",
                 start_time: dt.datetime | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        start_time = start_time or dt.datetime.now(dt.timezone.utc)
        local = start_time.astimezone()
        self.header = SerHeader(width, height, bit_depth, color, 0, observer, instrument, telescope,
                                # "local" time = wall-clock time stored as if it were UTC
                                datetime_to_ticks(local.replace(tzinfo=dt.timezone.utc)),
                                datetime_to_ticks(start_time))
        self._f = open(self.path, "wb")
        self._f.write(self.header.pack())
        self._ts = open(str(self.path) + ".ts", "wb")
        self._last_sync = time.monotonic()
        self.frames = 0
        self.bytes_written = HEADER_SIZE
        self._closed = False
        self._dtype = np.uint8 if self.header.bytes_per_sample == 1 else np.dtype("<u2")

    def write(self, frame: np.ndarray, timestamp: dt.datetime | None = None) -> None:
        h = self.header
        expected = (h.height, h.width, 3) if h.planes == 3 else (h.height, h.width)
        if frame.shape != expected:
            raise ValueError(f"Frame shape {frame.shape} does not match recording {expected}")
        data = np.ascontiguousarray(frame, dtype=self._dtype)
        if h.color == "BGR":
            data = np.ascontiguousarray(data[..., ::-1])
        self._f.write(data.tobytes())
        ts = datetime_to_ticks(timestamp or dt.datetime.now(dt.timezone.utc))
        self._ts.write(struct.pack("<q", ts))
        self.frames += 1
        self.bytes_written += h.frame_bytes
        if time.monotonic() - self._last_sync >= self.SYNC_EVERY_S:
            self.sync()

    def sync(self) -> None:
        """Write the current frame count into the header and push data to disk."""
        self._f.flush()
        pos = self._f.tell()
        self._f.seek(38)  # offset of FrameCount
        self._f.write(struct.pack("<i", self.frames))
        self._f.seek(pos)
        self._f.flush()
        self._ts.flush()
        try:
            os.fsync(self._f.fileno())
            os.fsync(self._ts.fileno())
        except OSError:
            pass
        self._last_sync = time.monotonic()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.sync()
        self._ts.close()
        # Append the timestamp trailer, then drop the side file.
        ts_path = Path(str(self.path) + ".ts")
        ts = np.fromfile(ts_path, dtype="<i8") if ts_path.exists() else np.zeros(0, "<i8")
        if len(ts) == self.frames:
            self._f.write(ts.tobytes())
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()
        if len(ts) == self.frames:
            try:
                ts_path.unlink()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def repair(path: str | os.PathLike) -> dict:
    """Fix a SER file whose recording was interrupted (crash, power cut, unplugged camera).

    Sets the frame count to the number of complete frames on disk, drops a partial
    last frame, and appends the timestamp trailer if the side file survived.
    The original file is only modified after a backup header copy is made.
    """
    path = Path(path)
    with open(path, "r+b") as f:
        h = SerHeader.unpack(f.read(HEADER_SIZE))
        size = path.stat().st_size
        ts_path = Path(str(path) + ".ts")
        complete = (size - HEADER_SIZE) // h.frame_bytes
        has_trailer = h.frame_count > 0 and size == HEADER_SIZE + h.frame_count * (h.frame_bytes + 8)
        if has_trailer and not ts_path.exists():
            return {"repaired": False, "frames": h.frame_count, "message": "File is already complete."}
        Path(str(path) + ".header-backup").write_bytes(h.pack())
        f.seek(38)
        f.write(struct.pack("<i", complete))
        f.truncate(HEADER_SIZE + complete * h.frame_bytes)
        added_ts = False
        if ts_path.exists():
            ts = np.fromfile(ts_path, dtype="<i8")
            if len(ts) >= complete:
                f.seek(0, 2)
                f.write(ts[:complete].tobytes())
                added_ts = True
        f.flush()
    if ts_path.exists() and added_ts:
        ts_path.unlink()
    return {"repaired": True, "frames": int(complete), "timestamps": added_ts,
            "message": f"Recovered {complete} complete frames."}
