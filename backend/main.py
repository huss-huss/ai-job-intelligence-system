"""AI Job Intelligence API - FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routes import analyze, cron, health, jobs, runs, scrape, sources


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

app.include_router(health.router)
app.include_router(sources.router)
app.include_router(jobs.router)
app.include_router(scrape.router)
app.include_router(runs.router)
app.include_router(analyze.router)
app.include_router(cron.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.debug)
