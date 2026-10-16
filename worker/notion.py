"""Sync scored jobs into a Notion database.

Pages are keyed by the job URL, so re-running a discovery updates scores instead of
creating duplicates. "Status" is only set when a page is created, so anything you
change in Notion (Applied, Rejected...) is never overwritten.
"""

from __future__ import annotations

import logging
from typing import Any

from worker.pipeline import ScoredJob
from worker.sources.base import HttpClient

logger = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TEXT_LIMIT = 2000  # Notion rich_text content limit

# Properties FirstFetch writes. `setup_database` adds any that are missing.
SCHEMA: dict[str, dict[str, Any]] = {
    "Company": {"rich_text": {}},
    "Score": {"number": {"format": "number"}},
    "URL": {"url": {}},
    "Source": {"select": {}},
    "Location": {"rich_text": {}},
    "Eligible": {"select": {"options": [{"name": "Yes"}, {"name": "No"}, {"name": "Unknown"}]}},
    "Why": {"rich_text": {}},
    "Posted": {"date": {}},
    "Status": {"select": {"options": [{"name": "New"}, {"name": "Applied"}, {"name": "Skipped"}]}},
}


def _text(value: str | None) -> dict:
    return {"rich_text": [{"text": {"content": (value or "")[:TEXT_LIMIT]}}]}


def page_properties(job: ScoredJob, *, include_status: bool) -> dict[str, Any]:
    p, m = job.posting, job.match
    eligible = {True: "Yes", False: "No", None: "Unknown"}[m.eligible]
    props: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": p.title[:TEXT_LIMIT]}}]},
        "Company": _text(p.company),
        "Score": {"number": m.score},
        "URL": {"url": p.url},
        "Source": {"select": {"name": p.source}},
        "Location": _text(p.location),
        "Eligible": {"select": {"name": eligible}},
        "Why": _text(" · ".join(m.reasons)),
        "Posted": {"date": {"start": p.posted_at.isoformat()} if p.posted_at else None},
    }
    if include_status:
        props["Status"] = {"select": {"name": "New"}}
    return props


class NotionSync:
    def __init__(self, token: str, database_id: str, http: HttpClient | None = None) -> None:
        self.database_id = database_id
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}
        # Notion allows ~3 requests/second per integration.
        self.http = http or HttpClient(min_interval=0.35)

    def setup_database(self) -> list[str]:
        """Add missing FirstFetch properties to the database. Returns the names added."""
        db = self.http.request_json("GET", f"{API}/databases/{self.database_id}", headers=self.headers)
        existing = db.get("properties", {})
        missing = {name: spec for name, spec in SCHEMA.items() if name not in existing}
        if missing:
            self.http.request_json(
                "PATCH", f"{API}/databases/{self.database_id}", json={"properties": missing}, headers=self.headers
            )
        return sorted(missing)

    def find_page(self, url: str) -> str | None:
        body = {"filter": {"property": "URL", "url": {"equals": url}}, "page_size": 1}
        data = self.http.request_json(
            "POST", f"{API}/databases/{self.database_id}/query", json=body, headers=self.headers
        )
        results = data.get("results") or []
        return results[0]["id"] if results else None

    def upsert(self, job: ScoredJob) -> tuple[str, bool]:
        """Create or update the page for this job. Returns (page_id, created)."""
        page_id = self.find_page(job.posting.url)
        if page_id:
            self.http.request_json(
                "PATCH",
                f"{API}/pages/{page_id}",
                json={"properties": page_properties(job, include_status=False)},
                headers=self.headers,
            )
            return page_id, False
        page = self.http.request_json(
            "POST",
            f"{API}/pages",
            json={
                "parent": {"database_id": self.database_id},
                "properties": page_properties(job, include_status=True),
            },
            headers=self.headers,
        )
        return page["id"], True

    def sync(self, jobs: list[ScoredJob], min_score: int) -> dict[str, int]:
        stats = {"created": 0, "updated": 0, "skipped": 0}
        for job in jobs:
            if job.score < min_score or job.match.eligible is False:
                stats["skipped"] += 1
                continue
            _, created = self.upsert(job)
            stats["created" if created else "updated"] += 1
        return stats
