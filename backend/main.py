"""AI Job Intelligence API - FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routes import health, jobs, runs, scrape, sources, analyze


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ensure pgvector extension. Shutdown: nothing special."""
    from sqlalchemy import text
    from app.database import engine
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.debug)
