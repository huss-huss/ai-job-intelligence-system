"""Adapters for public job-board APIs.

Each adapter maps one API's JSON into JobPosting. Config keys are per-adapter and
come from Source.config, e.g. {"type": "remotive", "search": "python", "limit": 50}.

API terms worth respecting:
- Remotive asks for a few requests per day at most and a link back to the listing.
- RemoteOK requires linking back to the listing URL.
- Arbeitnow asks callers not to abuse the free API.
"""

from __future__ import annotations

from typing import Any

from worker.salary import normalize_period, parse_salary
from worker.sources.base import HttpClient, JobPosting, parse_datetime, to_number
from worker.text import fix_mojibake, html_to_text


def parse_salary_text(text: str | None) -> tuple[float | None, float | None]:
    """Best-effort (min, max) from free text like "$170k - $200k"; see worker.salary.parse_salary."""
    parsed = parse_salary(text)
    return parsed.min, parsed.max


def _matches_keywords(posting: JobPosting, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{posting.title} {' '.join(posting.tags)} {posting.description or ''}".lower()
    return any(k.lower() in haystack for k in keywords)


class RemotiveAdapter:
    name = "remotive"
    url = "https://remotive.com/api/remote-jobs"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        params = {k: config[k] for k in ("search", "category", "limit") if config.get(k)}
        data = self.http.get_json(self.url, params=params)
        return [self.parse(j) for j in data.get("jobs", [])]

    @staticmethod
    def parse(j: dict[str, Any]) -> JobPosting:
        salary = parse_salary(j.get("salary"))
        return JobPosting(
            source="remotive",
            external_id=str(j["id"]),
            url=j["url"],
            title=j["title"].strip(),
            company=j.get("company_name"),
            location=j.get("candidate_required_location") or None,
            description=html_to_text(j.get("description")),
            posted_at=parse_datetime(j.get("publication_date")),
            remote=True,
            tags=list(j.get("tags") or []),
            salary_min=salary.min,
            salary_max=salary.max,
            salary_currency=salary.currency,
            salary_period=salary.period,
            salary_text=j.get("salary") or None,
        )


class RemoteOKAdapter:
    name = "remoteok"
    url = "https://remoteok.com/api"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        data = self.http.get_json(self.url)
        # First element is the API legal notice, not a job.
        postings = [self.parse(j) for j in data if isinstance(j, dict) and j.get("id") and j.get("position")]
        return [p for p in postings if _matches_keywords(p, config.get("keywords") or [])]

    @staticmethod
    def parse(j: dict[str, Any]) -> JobPosting:
        return JobPosting(
            source="remoteok",
            external_id=str(j["id"]),
            url=j.get("url") or j.get("apply_url"),
            title=fix_mojibake(j["position"].strip()),
            company=fix_mojibake(j.get("company") or "") or None,
            location=j.get("location") or None,
            description=html_to_text(j.get("description")),
            posted_at=parse_datetime(j.get("date") or j.get("epoch")),
            remote=True,
            tags=list(j.get("tags") or []),
            salary_min=to_number(j.get("salary_min")),
            salary_max=to_number(j.get("salary_max")),
            salary_currency="USD" if to_number(j.get("salary_max")) else None,
        )


class ArbeitnowAdapter:
    name = "arbeitnow"
    url = "https://www.arbeitnow.com/api/job-board-api"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        pages = int(config.get("pages", 1))
        postings: list[JobPosting] = []
        for page in range(1, pages + 1):
            data = self.http.get_json(self.url, params={"page": page})
            postings.extend(self.parse(j) for j in data.get("data", []))
            if not (data.get("links") or {}).get("next"):
                break
        if config.get("remote_only", True):
            postings = [p for p in postings if p.remote]
        return [p for p in postings if _matches_keywords(p, config.get("keywords") or [])]

    @staticmethod
    def parse(j: dict[str, Any]) -> JobPosting:
        return JobPosting(
            source="arbeitnow",
            external_id=j["slug"],
            url=j["url"],
            title=j["title"].strip(),
            company=j.get("company_name"),
            location=j.get("location") or None,
            description=html_to_text(j.get("description")),
            posted_at=parse_datetime(j.get("created_at")),
            remote=bool(j.get("remote")),
            tags=list(j.get("tags") or []),
        )


class GreenhouseAdapter:
    """Any company that hosts its careers page on Greenhouse: config {"board": "<token>"}."""

    name = "greenhouse"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        board = config["board"]
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
        data = self.http.get_json(url, params={"content": "true"})
        postings = [self.parse(j) for j in data.get("jobs", [])]
        return [p for p in postings if _matches_keywords(p, config.get("keywords") or [])]

    @staticmethod
    def parse(j: dict[str, Any]) -> JobPosting:
        location = (j.get("location") or {}).get("name")
        workplace = next(
            (m.get("value") for m in j.get("metadata") or [] if (m.get("name") or "").lower() == "location type"),
            None,
        )
        remote = None
        if isinstance(workplace, str):
            remote = "remote" in workplace.lower()
        elif location:
            remote = "remote" in location.lower()
        return JobPosting(
            source="greenhouse",
            external_id=str(j["id"]),
            url=j["absolute_url"],
            title=j["title"].strip(),
            company=j.get("company_name"),
            location=location,
            description=html_to_text(j.get("content")),
            # first_published is when the requisition first went live; updated_at moves while the
            # role stays open, so recency scoring uses whichever is newer (see score_job).
            posted_at=parse_datetime(j.get("first_published") or j.get("updated_at")),
            updated_at=parse_datetime(j.get("updated_at")),
            remote=remote,
            tags=[d["name"] for d in j.get("departments") or [] if d.get("name")],
        )


class LeverAdapter:
    """Any company that hosts its careers page on Lever: config {"company": "<slug>"}."""

    name = "lever"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        company = config["company"]
        data = self.http.get_json(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
        postings = [self.parse(j, company) for j in data]
        return [p for p in postings if _matches_keywords(p, config.get("keywords") or [])]

    @staticmethod
    def parse(j: dict[str, Any], company: str | None = None) -> JobPosting:
        cats = j.get("categories") or {}
        salary = j.get("salaryRange") or {}
        workplace = (j.get("workplaceType") or "").lower()
        return JobPosting(
            source="lever",
            external_id=j["id"],
            url=j["hostedUrl"],
            title=j["text"].strip(),
            company=company,
            location=cats.get("location"),
            description=j.get("descriptionPlain") or html_to_text(j.get("description")),
            # Lever's public postings API has no "updated" timestamp, only createdAt.
            posted_at=parse_datetime(j.get("createdAt")),
            remote=(workplace == "remote") if workplace else None,
            tags=[v for v in (cats.get("team"), cats.get("department"), cats.get("commitment")) if v],
            salary_min=to_number(salary.get("min")),
            salary_max=to_number(salary.get("max")),
            salary_currency=salary.get("currency"),
            salary_period=normalize_period(salary.get("interval")) or salary.get("interval"),
        )


class HimalayasAdapter:
    name = "himalayas"
    url = "https://himalayas.app/jobs/api"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        limit = int(config.get("limit", 20))
        data = self.http.get_json(self.url, params={"limit": limit, "offset": int(config.get("offset", 0))})
        postings = [self.parse(j) for j in data.get("jobs", [])]
        return [p for p in postings if _matches_keywords(p, config.get("keywords") or [])]

    @staticmethod
    def parse(j: dict[str, Any]) -> JobPosting:
        restrictions = [r for r in j.get("locationRestrictions") or [] if isinstance(r, str)]
        return JobPosting(
            source="himalayas",
            external_id=j.get("guid") or j["applicationLink"],
            url=j.get("applicationLink") or j.get("guid"),
            title=j["title"].strip(),
            company=j.get("companyName"),
            location=", ".join(restrictions) if restrictions else "Worldwide",
            description=html_to_text(j.get("description")) or j.get("excerpt"),
            posted_at=parse_datetime(j.get("pubDate")),
            remote=True,
            tags=list(j.get("categories") or []) + list(j.get("seniority") or []),
            salary_min=to_number(j.get("minSalary")),
            salary_max=to_number(j.get("maxSalary")),
            salary_currency=j.get("currency"),
            salary_period=normalize_period(j.get("salaryPeriod")) or j.get("salaryPeriod"),
        )
