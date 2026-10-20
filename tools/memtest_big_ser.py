"""Large-file test: write a multi-gigabyte simulated SER video, then preview, score and stack it while
measuring the program's memory use. Shows that memory stays flat no matter how big the recording is.

    python tools/memtest_big_ser.py --gb 6 --out D:\\temp\\big.ser
    python tools/memtest_big_ser.py --gb 1 --gb 6        (compare two sizes)

The video is deleted afterwards unless --keep is given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astroflow.imaging import lucky, quality  # noqa: E402
from astroflow.io.ser import SerWriter  # noqa: E402
from astroflow.io.source import open_source  # noqa: E402
from astroflow.library.index import preview_image  # noqa: E402
from astroflow.sim.scenes import PlanetScene, Turbulence, sensor_readout  # noqa: E402


class PeakMemory:
    """Samples this process's resident memory (RSS) every 50 ms."""

    def __init__(self):
        self.proc = psutil.Process()
        self.peak = 0
        self._stop = threading.Event()

    def __enter__(self):
        self.base = self.proc.memory_info().rss
        self.peak = self.base
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, self.proc.memory_info().rss)
            time.sleep(0.05)

    def __exit__(self, *a):
        self._stop.set()
        self._t.join()


def write_big_ser(path: Path, gb: float, size=(600, 800), seed: int = 7) -> dict:
    """Simulated Jupiter at 12-bit, Bayer RGGB: a few dozen distinct turbulent frames, drifted and repeated."""
    rng = np.random.default_rng(seed)
    H, W = size
    scene = PlanetScene("jupiter", (H, W), 0.3 * min(H, W))
    truth = scene.render(0.0)
    turb = Turbulence((H, W), 1.2, seed)
    base = []
    for _ in range(48):
        frame, _ = turb.apply(truth)
        base.append(sensor_readout(frame * 0.8, 12, 1.0, "RGGB", rng=rng))
    frame_bytes = H * W * 2
    n = int(gb * 1024**3 // frame_bytes)
    t0 = time.time()
    with SerWriter(path, W, H, 12, "RGGB", observer="AstroFlow memory test", telescope="simulated") as w:
        for i in range(n):
            f = base[i % len(base)]
            dy, dx = (int(v) for v in rng.integers(-3, 4, 2) * 2)   # keep the Bayer phase: even shifts
            w.write(np.roll(f, (dy, dx), axis=(0, 1)))
    return {"frames": n, "bytes": path.stat().st_size, "write_s": round(time.time() - t0, 1)}


def run(gb: float, out: Path | None, keep: bool) -> dict:
    folder = Path(tempfile.mkdtemp(prefix="af-bigser-")) if out is None else out.parent
    path = out or folder / f"big_{gb:g}GB.ser"
    res = {"target_gb": gb}
    res["write"] = write_big_ser(path, gb)
    print(f"Wrote {res['write']['bytes'] / 1e9:.2f} GB ({res['write']['frames']:,} frames) in {res['write']['write_s']} s", flush=True)
    try:
        with PeakMemory() as m:
            t0 = time.time()
            preview_image(str(path), 1200)
            res["preview"] = {"seconds": round(time.time() - t0, 2), "peak_extra_mb": round((m.peak - m.base) / 1e6)}
        print("preview", res["preview"], flush=True)
        src = open_source(path)
        try:
            with PeakMemory() as m:
                t0 = time.time()
                rep = quality.analyze(src, "planetary", threads=4)
                dt = time.time() - t0
                res["analyze"] = {"seconds": round(dt, 1), "frames_per_min": round(len(rep["quality"]) / dt * 60),
                                  "peak_extra_mb": round((m.peak - m.base) / 1e6)}
            print("analyze", res["analyze"], flush=True)
            frames = quality.select_frames(rep, "percent", 10)
            with PeakMemory() as m:
                t0 = time.time()
                r = lucky.stack(src, frames, {"threads": 4, "memory_budget": 2 * 1024**3})
                res["stack"] = {"frames": r.frames_used, "seconds": round(time.time() - t0, 1), "aps": len(r.aps),
                                "peak_extra_mb": round((m.peak - m.base) / 1e6)}
            print("stack", res["stack"], flush=True)
        finally:
            src.close()
        res["process_peak_rss_mb"] = round(psutil.Process().memory_info().rss / 1e6)
    finally:
        if not keep:
            path.unlink(missing_ok=True)
            for side in (path.with_name(path.name + ".ts"), path.with_name(path.name + ".json")):
                side.unlink(missing_ok=True)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gb", type=float, action="append", help="video size in GB (repeat to compare sizes)")
    ap.add_argument("--out", type=Path, help="where to write the video (default: a temporary folder)")
    ap.add_argument("--keep", action="store_true", help="keep the video afterwards")
    ap.add_argument("--json", type=Path, help="also save the results as JSON")
    a = ap.parse_args()
    sizes = a.gb or [2.0]
    free = psutil.disk_usage(str(a.out.parent if a.out else tempfile.gettempdir())).free
    if max(sizes) * 1024**3 * 1.1 > free:
        sys.exit(f"Not enough free disk space for a {max(sizes)} GB test ({free / 1e9:.1f} GB free).")
    results = [run(g, a.out, a.keep) for g in sizes]
    print(json.dumps(results, indent=1))
    if a.json:
        a.json.write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
