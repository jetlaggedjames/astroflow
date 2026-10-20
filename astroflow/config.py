"""User settings and well-known folders.

Settings live in one JSON file in the user's app-data folder
(Windows: %APPDATA%\\AstroFlow, elsewhere: ~/.astroflow). The environment
variable ASTROFLOW_HOME overrides that location (the tests use it).
Writes are atomic (write temp file, then rename) so a crash never leaves a
half-written settings file.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()


def app_home() -> Path:
    env = os.environ.get("ASTROFLOW_HOME")
    if env:
        p = Path(env)
    elif sys.platform.startswith("win"):
        p = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "AstroFlow"
    else:
        p = Path.home() / ".astroflow"
    p.mkdir(parents=True, exist_ok=True)
    return p


def documents_dir() -> Path:
    env = os.environ.get("ASTROFLOW_HOME")
    if env:
        return Path(env) / "Documents"
    docs = Path.home() / "Documents"
    return docs if docs.exists() else Path.home()


def sub(name: str) -> Path:
    """A sub-folder of the app home, created on demand."""
    p = app_home() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path: return sub("logs")
def cache_dir() -> Path: return sub("cache")
def temp_dir() -> Path: return sub("cache/tmp")
def presets_dir() -> Path: return sub("presets")
def calibration_dir() -> Path: return sub("calibration")
def data_dir() -> Path: return sub("data")       # downloaded catalogs / star databases
def uploads_dir() -> Path: return sub("uploads")


def bundled_data_dir() -> Path:
    """Read-only data shipped with the app (catalogs, cities)."""
    return Path(__file__).resolve().parent.parent / "data"


def _default_settings() -> dict:
    base = documents_dir() / "AstroFlow"
    return {
        "first_run_done": False,
        "mode": "beginner",            # "beginner" | "advanced"
        "theme": "dark",               # "dark" | "night" (red) | "day" (high contrast)
        "touch_mode": False,           # bigger controls for outdoor use
        "folders": {
            "captures": str(base / "Captures"),
            "projects": str(base / "Projects"),
            "exports": str(base / "Exports"),
            "library_roots": [str(base)],
        },
        "location": {"name": "", "lat": None, "lon": None, "elevation_m": 0.0, "tz": "UTC"},
        "equipment": {
            "telescope": "Sky-Watcher Flextube 300P",
            "aperture_mm": 305.0,
            "focal_length_mm": 1500.0,
            "barlows": [{"name": "None", "factor": 1.0}, {"name": "SVBONY 3x Barlow", "factor": 3.0}],
            "active_barlow": 0,
            "camera": "Player One Uranus-C",
            "pixel_um": 2.9,
            "sensor_w_px": 3856,
            "sensor_h_px": 2180,
            "filter": "UV/IR cut",
            "mount_type": "altaz",      # "altaz" | "eq" (future)
        },
        "imaging": {"targets": "both"},  # "planetary" | "deepsky" | "both"
        "camera": {"driver": "simulator", "playerone_dll": "", "ascom_progid": ""},
        "mount": {
            "driver": "none",            # none | simulator | synscan_wifi | synscan_hc | ascom | alpaca
            "host": "192.168.4.1",
            "port": 11880,
            "serial_port": "",
            "ascom_progid": "",
            "alpaca_url": "http://127.0.0.1:11111",
            "alpaca_device": 0,
            "alt_limit_deg": 10.0,
            "confirm_slew_deg": 30.0,
        },
        "solver": {
            "engine": "auto",            # auto | astap | builtin | online
            "astap_path": "",
            "astap_db_dir": "",
            "allow_online": False,
            "astrometry_api_key": "",
        },
        "network": {"allow_lan": False, "port": 8765, "access_code": f"{secrets.randbelow(10**6):06d}"},
        "gpu": {"enabled": True},
        "catalogs": {"enabled": ["messier", "caldwell", "ngc", "ic", "stars", "planets"]},
        "performance": {"memory_budget_gb": 0, "threads": 0},  # 0 = automatic
        "last_project": "",
    }


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (app_home() / "settings.json")
        self._data = _default_settings()
        self.load()

    def load(self) -> None:
        with _lock:
            if self.path.exists():
                try:
                    stored = json.loads(self.path.read_text(encoding="utf-8"))
                    self._data = _deep_merge(_default_settings(), stored)
                except Exception:
                    # A broken settings file must never stop the app from starting.
                    backup = self.path.with_suffix(".broken.json")
                    try:
                        os.replace(self.path, backup)
                    except OSError:
                        pass
                    self._data = _default_settings()
            self.save()

    def save(self) -> None:
        with _lock:
            atomic_write_text(self.path, json.dumps(self._data, indent=2))

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any, save: bool = True) -> None:
        with _lock:
            parts = dotted.split(".")
            cur = self._data
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = value
            if save:
                self.save()

    def update(self, patch: dict) -> dict:
        with _lock:
            self._data = _deep_merge(self._data, patch)
            self.save()
            return self.all()

    def all(self) -> dict:
        with _lock:
            return copy.deepcopy(self._data)

    # --- convenience -----------------------------------------------------
    def folder(self, name: str) -> Path:
        p = Path(self.get(f"folders.{name}"))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def effective_focal_length(self) -> float:
        eq = self.get("equipment")
        barlows = eq.get("barlows") or [{"factor": 1.0}]
        idx = min(max(int(eq.get("active_barlow", 0)), 0), len(barlows) - 1)
        return float(eq.get("focal_length_mm", 1500)) * float(barlows[idx].get("factor", 1.0))

    def pixel_scale_arcsec(self, binning: int = 1) -> float:
        """Arc-seconds of sky per pixel: 206.265 * pixel(um) / focal length(mm)."""
        return 206.265 * float(self.get("equipment.pixel_um", 2.9)) * binning / self.effective_focal_length()

    def memory_budget_bytes(self) -> int:
        gb = float(self.get("performance.memory_budget_gb", 0) or 0)
        if gb <= 0:
            try:
                import psutil
                total = psutil.virtual_memory().total
            except Exception:
                total = 8 * 1024**3
            # Use at most 40% of RAM for working buffers, capped at 24 GB.
            return int(min(total * 0.4, 24 * 1024**3))
        return int(gb * 1024**3)

    def threads(self) -> int:
        n = int(self.get("performance.threads", 0) or 0)
        return n if n > 0 else max(1, os.cpu_count() or 2)


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    with _lock:
        if _settings is None:
            _settings = Settings()
        return _settings


def reset_for_tests() -> None:
    global _settings
    with _lock:
        _settings = None
