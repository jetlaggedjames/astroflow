"""The local web server: the same user interface works in the desktop window and in a browser
on another computer on your network (when you allow it in Settings)."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import SOFTWARE_ID
from ..config import settings
from .common import is_local
from .routes import hardware, library, system, wizard, work

log = logging.getLogger("astroflow.server")
WEB = Path(__file__).resolve().parent.parent.parent / "web"

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AstroFlow - access code</title><style>body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6e6e6;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}form{background:#161b22;padding:32px;border-radius:12px;max-width:360px}
input,button{font-size:20px;padding:10px;border-radius:8px;border:1px solid #444;width:100%;box-sizing:border-box;margin-top:12px}
button{background:#2f81f7;color:#fff;border:0}p{color:#aaa}</style></head><body><form onsubmit="go(event)">
<h2>AstroFlow</h2><p>Enter the access code shown in AstroFlow on the main computer (Settings &rarr; Network).</p>
<input id=c inputmode=numeric autocomplete=one-time-code placeholder="Access code"><button>Open AstroFlow</button><p id=m></p></form>
<script>async function go(e){e.preventDefault();const r=await fetch('/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({code:document.getElementById('c').value})});if(r.ok)location.reload();else document.getElementById('m').textContent='Wrong code.'}</script>
</body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(title=SOFTWARE_ID, docs_url="/api/docs", redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=2048)

    @app.middleware("http")
    async def access_guard(request: Request, call_next):
        """Other computers need the access code; everything else is open on this computer."""
        if not is_local(request):
            if not settings().get("network.allow_lan"):
                return JSONResponse({"detail": "Network access is switched off on the main computer"}, status_code=403)
            code = str(settings().get("network.access_code"))
            given = request.cookies.get("af_access") or request.headers.get("X-Access-Code")
            if given != code and request.url.path not in ("/api/auth",):
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"detail": "Access code required"}, status_code=401)
                return HTMLResponse(LOGIN_PAGE, status_code=401)
        response = await call_next(request)
        if request.url.path in ("/", "/index.html") or request.url.path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    for r in (system.router, wizard.router, library.router, work.router, hardware.router, hardware.ws_router):
        app.include_router(r)

    @app.exception_handler(Exception)
    async def errors(request: Request, exc: Exception):
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/", StaticFiles(directory=str(WEB)), name="web")

    @app.on_event("startup")
    def warm_up():
        # Load catalogues and the city list in the background so the first search is instant.
        def load():
            try:
                from ..astro.catalog import Catalog, _cities, star_table
                Catalog.default()
                star_table()
                _cities()
            except Exception as e:
                log.warning("Catalogue warm-up failed: %s", e)
        threading.Thread(target=load, name="warm-up", daemon=True).start()

    @app.on_event("shutdown")
    def stop_hardware():
        try:
            from ..hardware.capture import capture
            capture.stop_recording("app closing")
            capture.disconnect()
        except Exception:
            pass
        try:
            from ..hardware.mount.controller import controller
            controller.stop("app closing")
            controller.disconnect()
        except Exception:
            pass

    return app


app = create_app()
