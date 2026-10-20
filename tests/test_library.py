"""Library indexing, clean-up safety and projects."""
import os
import time
from pathlib import Path

import numpy as np
import pytest

from astroflow import project
from astroflow.config import settings
from astroflow.io.images import write_fits, write_standard
from astroflow.library import cleanup
from astroflow.library.index import Library, library, thumbnail
from astroflow.sim.scenes import jupiter_ser


@pytest.fixture()
def lib(tmp_path):
    root = tmp_path / "Astro"
    (root / "2026-10-20" / "M31" / "lights").mkdir(parents=True)
    (root / "2026-10-20" / "darks").mkdir(parents=True)
    for i in range(3):
        write_fits(root / "2026-10-20" / "M31" / "lights" / f"light_{i}.fits", np.full((32, 48), 1000 * (i + 1), np.uint16),
                   {"EXPTIME": 10.0, "GAIN": 120, "DATE-OBS": "2026-10-21T03:00:0%d" % i, "INSTRUME": "Sim"}, bits=16)
    write_fits(root / "2026-10-20" / "darks" / "dark_0.fits", np.zeros((32, 48), np.uint16), {"EXPTIME": 10.0}, bits=16)
    jupiter_ser(root / "2026-10-20" / "Jupiter_221530.ser", n_frames=5, size=(48, 64), radius=15)
    write_standard(root / "export_saturn.png", np.zeros((10, 10, 3), np.float32), "png")
    settings().update({"folders": {"library_roots": [str(root)], "captures": str(tmp_path / "cap"),
                                   "projects": str(tmp_path / "proj"), "exports": str(tmp_path / "exp")}})
    lb = Library(tmp_path / "lib.db")
    return lb, root


def test_scan_targets_types_and_incremental_rescan(lib):
    lb, root = lib
    r = lb.scan([str(root)])
    assert r["files"] == 6 and r["updated"] == 6
    items = {Path(i["path"]).name: i for i in lb.query()["items"]}
    assert items["light_0.fits"]["target"] == "M 31" and items["light_0.fits"]["target_source"] == "folder"
    assert items["Jupiter_221530.ser"]["target"] == "Jupiter" and items["Jupiter_221530.ser"]["frames"] == 5
    assert items["export_saturn.png"]["target"] == "Saturn"
    assert items["dark_0.fits"]["imagetype"] == "dark" and items["light_1.fits"]["imagetype"] == "light"
    assert items["light_0.fits"]["exposure"] == 10.0 and items["light_0.fits"]["gain"] == 120
    # Unchanged files are not re-read.
    assert lb.scan([str(root)])["updated"] == 0
    # A modified file is re-read; a removed one is marked missing (not forgotten).
    p = root / "2026-10-20" / "M31" / "lights" / "light_2.fits"
    write_fits(p, np.full((32, 48), 7, np.uint16), {"OBJECT": "M 33"}, bits=16)
    os.remove(root / "2026-10-20" / "darks" / "dark_0.fits")
    r = lb.scan([str(root)])
    assert r["updated"] == 1 and r["missing"] == 1
    assert lb.query(text="light_2")["items"][0]["target"] == "M 33"
    assert lb.query(text="dark_0")["items"][0]["missing"] == 1


def test_sessions_filters_ratings_tags(lib):
    lb, root = lib
    lb.scan([str(root)])
    f = lb.facets()
    assert any(s["target"] == "M 31" and s["files"] == 3 for s in f["sessions"])
    one = lb.query(target="M 31")["items"][0]
    lb.update(one["id"], rating=5, tags=["best", "andromeda"])
    assert lb.query(min_rating=5)["total"] == 1
    assert lb.query(tag="best")["total"] == 1
    assert lb.query(kind="ser")["total"] == 1
    assert lb.query(text="jupiter")["total"] == 1


def test_thumbnails(lib, monkeypatch):
    lb, root = lib
    lb.scan([str(root)])
    import astroflow.library.index as idx
    monkeypatch.setattr(idx, "_lib", lb)
    monkeypatch.setattr(idx, "library", lambda: lb)
    ser = lb.query(kind="ser")["items"][0]
    assert thumbnail(ser["id"]).stat().st_size > 500


def test_cleanup_requires_plan_and_protects_raw(lib, monkeypatch):
    lb, root = lib
    lb.scan([str(root)])
    monkeypatch.setattr(cleanup, "library", lambda: lb)
    trashed = []
    monkeypatch.setattr(cleanup, "_to_trash", lambda p: (trashed.append(p), os.remove(p)))
    lights = [i["id"] for i in lb.query(target="M 31")["items"]]
    with pytest.raises(cleanup.SafetyError, match="RAW"):
        cleanup.plan_trash(lights)
    plan = cleanup.plan_trash(lights, include_raw=True)
    assert plan["count"] == 3 and plan["total_size"] > 0 and plan["warnings"]
    assert all(os.path.exists(f["path"]) for f in plan["files"])    # nothing happened yet
    with pytest.raises(cleanup.SafetyError):
        cleanup.execute("not-a-real-token")
    res = cleanup.execute(plan["token"])
    assert res["done"] == 3 and len(trashed) == 3
    with pytest.raises(cleanup.SafetyError):
        cleanup.execute(plan["token"])                               # one-time code


def test_cleanup_refuses_if_file_changed(lib, monkeypatch):
    lb, root = lib
    lb.scan([str(root)])
    monkeypatch.setattr(cleanup, "library", lambda: lb)
    png = lb.query(kind="png")["items"][0]
    plan = cleanup.plan_trash([png["id"]])
    time.sleep(0.01)
    with open(png["path"], "ab") as f:
        f.write(b"xx")
    with pytest.raises(cleanup.SafetyError, match="changed"):
        cleanup.execute(plan["token"])
    assert os.path.exists(png["path"])


def test_offload_copies_verifies_and_keeps_originals(lib, tmp_path, monkeypatch):
    lb, root = lib
    lb.scan([str(root)])
    monkeypatch.setattr(cleanup, "library", lambda: lb)
    dest = tmp_path / "external"
    dest.mkdir()
    ids = [i["id"] for i in lb.query(kind="ser")["items"]]
    with pytest.raises(cleanup.SafetyError, match="not been offloaded"):
        cleanup.plan_remove_offloaded(ids)
    plan = cleanup.plan_offload(ids, str(dest))
    res = cleanup.execute(plan["token"])
    assert res["verified"] == 1 and not res["failed"]
    copy = Path(res["files"][0]["dest"])
    src = Path(res["files"][0]["path"])
    assert copy.exists() and src.exists() and copy.read_bytes() == src.read_bytes()
    assert lb.get(ids[0])["offloaded_to"] == str(copy)
    removed = []
    monkeypatch.setattr(cleanup, "_to_trash", lambda p: removed.append(p))
    plan2 = cleanup.plan_remove_offloaded(ids)
    cleanup.execute(plan2["token"])
    assert removed == [str(src)]


def test_duplicates(lib, monkeypatch):
    lb, root = lib
    import shutil
    shutil.copy2(root / "export_saturn.png", root / "copy_of_saturn.png")
    lb.scan([str(root)])
    monkeypatch.setattr(cleanup, "library", lambda: lb)
    groups = cleanup.find_duplicates()
    assert len(groups) == 1 and len(groups[0]["files"]) == 2


def test_project_history_and_recovery():
    p = project.new_project("Jupiter test", ["/x/a.ser"], {"name": "Jupiter", "type": "planet"})
    project.commit_process(p, [{"op": "gamma", "params": {"gamma": 1.2}}], {}, "gamma")
    project.commit_process(p, [{"op": "gamma", "params": {"gamma": 1.5}}], {}, "gamma 1.5")
    project.undo(p)
    assert p["process"]["steps"][0]["params"]["gamma"] == 1.2
    project.redo(p)
    assert p["process"]["steps"][0]["params"]["gamma"] == 1.5
    project.undo(p)
    project.commit_process(p, [{"op": "levels", "params": {}}], {}, "levels")   # drops the redo branch
    assert [h["label"] for h in p["process"]["history"]] == ["Original (no edits)", "gamma", "levels"]
    # Even the very first edit can be undone, back to the untouched image.
    project.undo(p)
    project.undo(p)
    assert p["process"]["steps"] == []
    project.redo(p)
    project.redo(p)
    loaded = project.load(p["id"])
    assert loaded["process"]["steps"][0]["op"] == "levels"
    # A corrupted save falls back to the previous version.
    path = project.path_of(p)
    path.write_text("{ broken")
    assert project.load(str(path))["id"] == p["id"]
    assert any(x["id"] == p["id"] for x in project.list_projects())
