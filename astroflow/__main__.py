"""``python -m astroflow`` starts the app (same as run.py)."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent.parent / "run.py"), run_name="__main__")
