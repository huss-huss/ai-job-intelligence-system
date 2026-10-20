"""AI Job Intelligence API - FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from app.auth import CSRF_MISSING, admin_token_ok, csrf_ok, has_credentials
from app.config import settings
from app.routes import analyze, applications, auth, cron, health, jobs, me, runs, scrape, sources, talent, telegram
from app.routes.telegram import WEBHOOK_PATH
from app.web_sessions import COOKIE_NAME
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ensure pgvector extension. Shutdown: nothing special."""
    import logging

    from app.database import SERVERLESS, engine
    from sqlalchemy import text

    if not SERVERLESS:  # on Vercel the extension is created by `alembic upgrade head` at deploy time
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception:
            logging.getLogger(__name__).exception("could not ensure pgvector extension")
    yield
    await engine.dispose()


def docs_enabled() -> bool:
    """API_DOCS=true/false; by default on for local development and off on Vercel (production)."""
    if settings.api_docs is not None:
        return settings.api_docs
    from app.database import SERVERLESS

    return not SERVERLESS


DOCS = docs_enabled()

app = FastAPI(
    title=settings.app_name,
    description="Backend API for AI Job Intelligence Automation System",
    version="0.1.0",
    lifespan=lifespan,
    # The schema holds no data (the routes are in the public repo), but a private deployment doesn't need
    # to advertise them: off in production unless API_DOCS=true. When on, these three paths are public.
    docs_url="/api/docs" if DOCS else None,
    redoc_url="/api/redoc" if DOCS else None,
    openapi_url="/api/openapi.json" if DOCS else None,
)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# The only API routes a logged-out visitor can reach. Everything else under /api/ answers 401 (reads) or
# 403 (writes) here before any route runs, and every data route also checks the caller itself
# (require_viewer / require_owner in app/auth.py), so a route missing from one layer is still closed.
PUBLIC_PATHS = frozenset(
    {
        "/api/health",
        "/api/auth/session",  # {"authenticated": false} for the public
        "/api/auth/telegram",  # login: the widget's signature and the CSRF header
        "/api/auth/token-session",  # a /dashboard link to a session: the token and the CSRF header
        "/api/auth/logout",
        WEBHOOK_PATH,  # Telegram: its own secret header
        "/api/cron/discover",  # Vercel Cron: CRON_SECRET
    }
)
DOCS_PATHS = frozenset({"/api/docs", "/api/docs/oauth2-redirect", "/api/redoc", "/api/openapi.json"})
# Each user's own tracker. These routes authenticate every request themselves (app/auth.py), so a
# member's personal dashboard token (X-User-Token) or login session (ff_session cookie, with the CSRF
# header) can write there.
USER_WRITE_PREFIXES = ("/api/applications/", "/api/talent-networks/")
UNAUTHENTICATED = {"detail": "Log in to see this", "authenticated": False}


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or (DOCS and path in DOCS_PATHS)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """The outer lock: no credentials, no data. The routes then check that the credentials are valid."""
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api/") or is_public(path):
        return await call_next(request)  # each public route checks its own secret (PUBLIC_PATHS)
    if not has_credentials(request):
        return JSONResponse(UNAUTHENTICATED, status_code=401, headers={"WWW-Authenticate": "Token"})
    if request.method in SAFE_METHODS:
        return await call_next(request)  # the route checks the credentials (require_viewer)
    if admin_token_ok(request.headers.get("x-admin-token")):
        return await call_next(request)
    if path.startswith(USER_WRITE_PREFIXES) and request.headers.get("x-user-token"):
        return await call_next(request)
    # A login session with the CSRF header: the route decides (its own tracker for anyone, admin
    # operations for the owner's session only, via require_owner).
    if request.cookies.get(COOKIE_NAME):
        if csrf_ok(request):
            return await call_next(request)
        return JSONResponse({"detail": CSRF_MISSING}, status_code=403)
    return JSONResponse({"detail": "Log in as the owner, or send X-Admin-Token"}, status_code=403)


# Added after the lock, so CORS wraps it: preflights are answered and even a 401 carries the CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        *[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(health.router)
app.include_router(sources.router)
app.include_router(jobs.router)
app.include_router(scrape.router)
app.include_router(runs.router)
app.include_router(analyze.router)
app.include_router(cron.router)
app.include_router(applications.router)
app.include_router(talent.router)
app.include_router(telegram.router)
app.include_router(me.router)
app.include_router(auth.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.debug)
