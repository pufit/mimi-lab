"""Mimi Lab FastAPI entrypoint.

Run:  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
"""
from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import settings
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# APScheduler emits repetitive INFO lines for frequent periodic ticks; keep its
# warnings while preventing routine scheduler chatter from dominating logs.
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("mimi_lab")

# modules that expose an APIRouter at app.<name>.router:router
MODULES = ["catalog", "match", "subs", "learn", "known", "acquire", "media", "mal", "watch", "connector", "events", "analyze", "jobs", "srs"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Import every module's service so its job handlers register with the queue.
    # A failed import silently drops ALL of that module's job handlers, which
    # previously stranded queued jobs in terminal 'no handler' errors — so it
    # must be loud (WARNING, not DEBUG).
    for _m in MODULES:
        try:
            importlib.import_module(f"app.{_m}.service")
        except Exception as e:
            log.warning("service %s not imported (its job handlers are missing!): %s", _m, e)
    try:
        from .jobs.service import start_scheduler
        start_scheduler()
        from .jobs.periodics import register_periodics
        register_periodics()
    except Exception as e:  # pragma: no cover
        log.warning("jobs scheduler/periodics not started: %s", e)
    yield
    try:
        from .jobs.service import stop_scheduler
        stop_scheduler()
    except Exception:
        pass
    # keep SQLite's query planner stats fresh across restarts (cheap)
    try:
        from .db import connect
        with connect() as cx:
            cx.execute("PRAGMA optimize")
    except Exception:
        pass


app = FastAPI(title="Mimi Lab", version="0.2.0", lifespan=lifespan)

# --- browser-attack hardening (see security.py + config.trusted_hosts) -------
# 1) TrustedHost: reject any request whose Host isn't in our allowlist. This is
#    the canonical defense against DNS-rebinding — a page that rebinds its domain
#    to 127.0.0.1 to drive this API from the victim's browser arrives with
#    Host: <attacker-domain>, which is refused here.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts())
# 2) CORS: an explicit origin allowlist (never '*'), so a foreign page can't read
#    API responses (e.g. the token from /connector/setup) cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # so Migaku's fetch can read the real media filename + range metadata.
    expose_headers=["Content-Disposition", "Content-Range", "Accept-Ranges"],
)

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_CORS_ORIGINS = set(settings.cors_origins())


@app.middleware("http")
async def _origin_guard(request: Request, call_next):
    """Refuse cross-site state-changing requests (CSRF / rebinding-driven POSTs).

    A state-changing request with an Origin that is neither same-origin (Origin
    host:port == Host header) nor in the allowlist is rejected. Browsers always
    send Origin on such requests; non-browser clients (the Connector, curl) send
    none and are unaffected. Same-origin UI works on any allowed Host.
    """
    if request.method in _MUTATING:
        origin = request.headers.get("origin")
        if origin:
            same_origin = urlparse(origin).netloc == request.headers.get("host", "")
            if not same_origin and origin not in _CORS_ORIGINS:
                return JSONResponse(
                    {"detail": "cross-origin request refused"}, status_code=403
                )
    return await call_next(request)


@app.get("/api/health")
def health(full: bool = False):
    """Liveness by default; `?full=1` aggregates every component probe
    (tokenizer, Transmission, Connector, MAL, jobs, disk — see app/health.py)."""
    if not full:
        return {"ok": True, "service": "mimi-lab", "version": "0.2.0"}
    from . import health as health_mod
    return health_mod.full_report()


# Defensive include: a half-built module must not take down the whole API.
for _m in MODULES:
    try:
        mod = importlib.import_module(f"app.{_m}.router")
        app.include_router(mod.router, prefix="/api")
        log.info("loaded module: %s", _m)
    except Exception as e:
        log.warning("module %s not loaded: %s", _m, e)


_API_CATCH_ALL = "/api/{rest:path}"


@app.api_route(_API_CATCH_ALL, methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
               include_in_schema=False)
def api_not_found(rest: str, request: Request):
    """Any `/api/...` path no router claims is a real 404 — for every method.

    Registered after the module routers (so real endpoints always win) and
    before the SPA mount, whose StaticFiles answers unmatched POSTs with 405
    ("method not allowed" on a route that no longer exists is misleading — a
    retired endpoint is *gone*, SRS_DESIGN §10.2). A path that does exist under
    a different method still gets 405.
    """
    path = request.url.path
    for route in app.router.routes:
        rx = getattr(route, "path_regex", None)
        if rx is None or getattr(route, "path", None) == _API_CATCH_ALL:
            continue
        if getattr(route, "methods", None) and rx.match(path):
            raise StarletteHTTPException(status_code=405, detail="method not allowed")
    raise StarletteHTTPException(status_code=404, detail=f"no such endpoint: {path}")


# static: extracted clips + the built web SPA (mounted last so /api wins)
settings.ensure_dirs()
app.mount("/clips", StaticFiles(directory=str(settings.clips_dir)), name="clips")


class SPAStaticFiles(StaticFiles):
    """Serve the built SPA, falling back to index.html for client-side routes.

    Never falls back for API/asset namespaces — a missing /api route must return a
    real 404, NOT index.html. (Otherwise an unknown endpoint returns HTML 200 and
    the client parses it as data → crashes. Bit us when the UI shipped before the
    matching backend route existed.)
    """

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as ex:
            if ex.status_code == 404 and not path.startswith(("api/", "api", "clips/")):
                return await super().get_response("index.html", scope)
            raise


_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
if _dist.exists():
    app.mount("/", SPAStaticFiles(directory=str(_dist), html=True), name="web")
