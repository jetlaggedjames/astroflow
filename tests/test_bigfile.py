"""Memory stays flat with big recordings (set ASTROFLOW_SKIP_BIG=1 to skip; writes ~1.2 GB to a temp folder)."""
import os

import pytest

from tools.memtest_big_ser import PeakMemory, write_big_ser


@pytest.mark.skipif(bool(os.environ.get("ASTROFLOW_SKIP_BIG")), reason="big-file test skipped by request")
def test_big_ser_memory_is_flat(tmp_path):
    from astroflow.imaging import lucky, quality
    from astroflow.io.source import open_source
    from astroflow.library.index import preview_image
    path = tmp_path / "big.ser"
    info = write_big_ser(path, 1.2)
    assert info["bytes"] > 1.2e9
    with PeakMemory() as m:
        preview_image(str(path), 800, frame=info["frames"] - 1)
    assert m.peak - m.base < 100e6
    src = open_source(path)
    try:
        with PeakMemory() as m:
            rep = quality.analyze(src, "planetary", threads=4)
        assert len(rep["quality"]) == info["frames"]
        assert m.peak - m.base < 300e6, f"scoring used {(m.peak - m.base) / 1e6:.0f} MB extra"
        frames = quality.select_frames(rep, "percent", 10)
        budget = 256 * 1024**2
        with PeakMemory() as m:
            r = lucky.stack(src, frames, {"threads": 4, "memory_budget": budget})
        assert r.frames_used == len(frames)
        assert m.peak - m.base < budget * 2.5, f"stacking used {(m.peak - m.base) / 1e6:.0f} MB extra"
    finally:
        src.close()
