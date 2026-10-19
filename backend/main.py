"""AI Job Intelligence API - FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth import admin_token_ok
from app.config import settings
from app.routes import analyze, applications, cron, health, jobs, me, runs, scrape, sources, talent, telegram
from app.routes.telegram import SECRET_HEADER, WEBHOOK_PATH, webhook_secret_ok


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ensure pgvector extension. Shutdown: nothing special."""
    import logging

    from sqlalchemy import text

    from app.database import SERVERLESS, engine

    if not SERVERLESS:  # on Vercel the extension is created by `alembic upgrade head` at deploy time
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception:
            logging.getLogger(__name__).exception("could not ensure pgvector extension")
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    description="Backend API for AI Job Intelligence Automation System",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

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

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Each user's own tracker. These routes authenticate every request themselves (app/auth.py), so a
# member's personal dashboard token (X-User-Token) can write there; nowhere else.
USER_WRITE_PREFIXES = ("/api/applications/", "/api/talent-networks/")


@app.middleware("http")
async def require_admin_for_writes(request: Request, call_next):
    """With ADMIN_TOKEN set, the deployment is read-only for anyone without the token."""
    if settings.admin_token and request.method not in SAFE_METHODS:
        # Telegram can't send the admin token; its webhook is authenticated by its own secret instead.
        if request.url.path == WEBHOOK_PATH and webhook_secret_ok(request.headers.get(SECRET_HEADER)):
            return await call_next(request)
        if request.url.path.startswith(USER_WRITE_PREFIXES) and request.headers.get("x-user-token"):
            return await call_next(request)
        if not admin_token_ok(request.headers.get("x-admin-token")):
            return JSONResponse({"detail": "This demo is read-only"}, status_code=403)
    return await call_next(request)


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.debug)
