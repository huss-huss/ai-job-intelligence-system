"""Vercel entrypoint: the FastAPI app from backend/, with the worker package importable for inline jobs."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "backend", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from main import app  # noqa: E402

__all__ = ["app"]
