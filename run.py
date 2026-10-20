"""Start AstroFlow: the local server plus a desktop window.

    python run.py              # normal start (desktop window)
    python run.py --browser    # open in your normal web browser instead
    python run.py --no-window  # server only (e.g. a headless PC used from another computer)
    python run.py --lan        # also allow other computers on your network (asks for the access code)
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def is_astroflow(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1.5) as r:
            return b"AstroFlow" in r.read()
    except Exception:
        return False


def free_port(start: int) -> int:
    for p in range(start, start + 50):
        if not port_open("127.0.0.1", p):
            return p
    raise RuntimeError("No free network port found")


def open_window(url: str, mode: str) -> subprocess.Popen | None:
    """Desktop window: pywebview if installed, else Edge/Chrome in 'app' mode, else the default browser."""
    if mode == "browser":
        webbrowser.open(url)
        return None
    try:
        import webview  # type: ignore  # optional
        return "webview"  # type: ignore[return-value]
    except ImportError:
        pass
    candidates = []
    if sys.platform.startswith("win"):
        pf = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramFiles", r"C:\Program Files"),
              os.environ.get("LOCALAPPDATA", "")]
        for base in pf:
            candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe", Path(base) / "Google/Chrome/Application/chrome.exe"]
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            w = shutil.which(name)
            if w:
                candidates.append(Path(w))
    from astroflow.config import app_home
    profile = app_home() / "window-profile"
    for exe in candidates:
        if exe.exists():
            try:
                return subprocess.Popen([str(exe), f"--app={url}", f"--user-data-dir={profile}", "--window-size=1500,950",
                                         "--no-first-run", "--disable-features=Translate"])
            except OSError:
                continue
    webbrowser.open(url)
    return None


def main() -> None:
    # Started by the desktop shortcut (pythonw.exe) there is no console: send stray output nowhere.
    # (Everything important goes to the log file in the AstroFlow settings folder.)
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    ap = argparse.ArgumentParser(description="AstroFlow")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--lan", action="store_true", help="allow other computers on the local network")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    from astroflow.config import settings
    from astroflow.logsetup import setup_logging
    setup_logging(args.verbose)
    s = settings()
    if args.lan:
        s.set("network.allow_lan", True)
    port = args.port or int(s.get("network.port", 8765))
    url = f"http://127.0.0.1:{port}/"
    if is_astroflow(port):
        # Already running: just open another window onto it.
        if not args.no_window:
            open_window(url, "browser" if args.browser else "app")
        return
    if port_open("127.0.0.1", port):
        port = free_port(port + 1)
        url = f"http://127.0.0.1:{port}/"
    host = "0.0.0.0" if s.get("network.allow_lan") else "127.0.0.1"

    import uvicorn
    from astroflow.server.app import app
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", ws_ping_interval=20)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="server", daemon=True)
    t.start()
    for _ in range(100):
        if port_open("127.0.0.1", port):
            break
        time.sleep(0.1)
    print(f"AstroFlow is running at {url}" + (f" (also on your network, port {port})" if host == "0.0.0.0" else ""))

    if args.no_window:
        t.join()
        return
    win = open_window(url, "browser" if args.browser else "app")
    if win == "webview":
        import webview  # type: ignore
        webview.create_window("AstroFlow", url, width=1500, height=950, min_size=(900, 600))
        webview.start()
        _shutdown_when_idle(server, t, wait_for_window=None)
    elif isinstance(win, subprocess.Popen):
        _shutdown_when_idle(server, t, wait_for_window=win)
    else:
        t.join()


def _shutdown_when_idle(server, thread, wait_for_window) -> None:
    """After the window closes, keep running until nothing is recording or processing, then exit."""
    if wait_for_window is not None:
        try:
            wait_for_window.wait()
        except KeyboardInterrupt:
            pass
    from astroflow.hardware.capture import capture
    from astroflow.jobs import jobs
    warned = False
    while capture.recorder is not None or jobs.active():
        if not warned:
            print("Window closed - finishing the running recording/tasks first. Re-open AstroFlow to watch them.")
            warned = True
        time.sleep(2)
    server.should_exit = True
    thread.join(timeout=10)


if __name__ == "__main__":
    main()
