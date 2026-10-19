import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_set_resume_imports_the_backend_without_pythonpath(tmp_path):
    """Run as the README shows (no PYTHONPATH): it must get past the backend import."""
    resume = tmp_path / "cv.md"
    resume.write_text("# Jane Doe\\nBackend engineer. Python, FastAPI, PostgreSQL. " * 20)
    db = tmp_path / "x.db"
    proc = subprocess.run(
        [sys.executable, "-m", "worker.cli", "set-resume", str(resume), "--database-url", f"sqlite:///{db}"],
        cwd=ROOT, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    )
    assert "No module named 'app'" not in proc.stderr
