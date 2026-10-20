"""Error logging to a file the user can attach to a bug report."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

from . import SOFTWARE_ID
from .config import logs_dir

LOG_FILE_NAME = "astroflow.log"
_configured = False


def log_file() -> Path:
    return logs_dir() / LOG_FILE_NAME


def setup_logging(verbose: bool = False) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_file(), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    root.addHandler(sh)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    def _excepthook(exc_type, exc, tb):
        logging.getLogger("crash").critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args):
        logging.getLogger("crash").critical(
            "Unhandled exception in thread %s", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    logging.getLogger("astroflow").info("Starting %s on Python %s (%s)", SOFTWARE_ID, sys.version.split()[0], sys.platform)


def tail(lines: int = 400) -> str:
    p = log_file()
    if not p.exists():
        return ""
    with open(p, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 200_000))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])
