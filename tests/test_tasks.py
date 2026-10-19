import json
from pathlib import Path

from app.models import JobRaw

from worker.sources.adapters import GreenhouseAdapter
from worker.tasks import _meta, _to_posting

FIXTURES = Path(__file__).parent / "fixtures"


def test_updated_at_round_trips_through_jobs_raw_meta():
    """updated_at has no column; it's stored in jobs_raw.meta as an ISO string (JSON-safe)."""
    posting = GreenhouseAdapter.parse(json.loads((FIXTURES / "greenhouse.json").read_text())["jobs"][0])
    meta = _meta(posting)
    assert meta["updated_at"] == posting.updated_at.isoformat()
    json.dumps(meta)  # JSONB needs plain values
    row = JobRaw(
        url=posting.url,
        title=posting.title,
        company=posting.company,
        location=posting.location,
        description=posting.description,
        posted_at=posting.posted_at,
        meta=meta,
    )
    restored = _to_posting(row)
    assert restored.posted_at == posting.posted_at
    assert restored.updated_at == posting.updated_at


def test_rows_stored_before_updated_at_existed_still_load():
    row = JobRaw(url="https://example.com/1", title="Engineer", meta={"source": "greenhouse"})
    assert _to_posting(row).updated_at is None
