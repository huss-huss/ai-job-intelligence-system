"""Remote-job boards with official feeds: Djinni (RSS), We Work Remotely (RSS), Jobicy and Working Nomads (JSON).

Terms and limits (checked October 2026):
- Djinni: public RSS at /jobs/rss/. It reads `primary_keyword`, `exp_level`, `english_level`
  and `employment`, returns up to 100 items, and silently ignores anything else (`region`,
  `keywords`, `page`), so unknown keys are refused here rather than sent. Items carry no
  company or location.
- We Work Remotely: one RSS feed per category. Their terms ask for a link back to the listing,
  so the original weworkremotely.com URL is kept as the job URL.
- Jobicy: /api/v2/remote-jobs with `count`, `geo`, `industry`, `tag`. Their docs ask that
  automated sync passes run at most once per hour, that Jobicy is credited, and that apply
  links go to the listing's own URL. `appliedFilters` echoes the filters the API used; one that
  was sent but not applied raises SourceError.
- Working Nomads: /api/exposed_jobs/ returns the latest listings (no parameters), so filtering
  by category and keyword happens here.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from worker.salary import normalize_period
from worker.sources.base import HttpClient, JobPosting, SourceError, parse_datetime, parse_rfc822, to_number
from worker.sources.normalize import normalize_employment_type, normalize_seniority
from worker.sources.rss import RssItem, parse_rss
from worker.text import fix_mojibake, html_to_text

RSS_ACCEPT = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8"
_FLAG = re.compile("[\U0001f1e6-\U0001f1ff]")  # regional-indicator letters (flag emoji)


def matches_keywords(posting: JobPosting, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{posting.title} {' '.join(posting.tags)} {posting.description or ''}".lower()
    return any(k.lower() in haystack for k in keywords)


# Config keys every adapter understands (see worker.sources.fetch_postings); never sent upstream.
GENERIC_KEYS = {"type", "name", "queries", "keywords", "max_age_days", "max_results", "full_descriptions"}


def _queries(config: dict[str, Any], keys: set[str], source: str) -> list[dict[str, Any]]:
    """Top-level filter keys merged under each entry of config['queries'] (or one query if absent).

    A key that is neither a filter the feed reads nor a generic option raises SourceError, because
    these feeds drop unknown filters silently and the result would look like a filtered one.
    """
    unknown = set(config) - keys - GENERIC_KEYS
    for query in config.get("queries") or []:
        unknown |= set(query) - keys
    if unknown:
        raise SourceError(f"{source} ignores {sorted(unknown)}; supported filters: {sorted(keys)}")
    top = {k: v for k, v in config.items() if k in keys}
    return [{**top, **q} for q in config.get("queries") or [{}]]


# --- Djinni ------------------------------------------------------------------------------------


class DjinniAdapter:
    """config {"type": "djinni", "queries": [{"primary_keyword": "Python"}], "employment": "remote"}."""

    name = "djinni"
    url = "https://djinni.co/jobs/rss/"
    PARAMS = {"primary_keyword", "exp_level", "english_level", "employment"}

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for params in _queries(config, self.PARAMS, "djinni RSS"):
            text = self.http.get_text(self.url, params=params, accept=RSS_ACCEPT)
            remote = True if str(params.get("employment", "")).lower() == "remote" else None
            postings.extend(self.parse(item, remote=remote) for item in parse_rss(text, source="djinni"))
        return [p for p in postings if matches_keywords(p, config.get("keywords") or [])]

    @staticmethod
    def parse(item: RssItem, *, remote: bool | None = None) -> JobPosting:
        link = item.get("link") or item.get("guid") or ""
        job_id = re.search(r"/jobs/(\d+)", link)
        return JobPosting(
            source="djinni",
            external_id=job_id.group(1) if job_id else link,
            url=link,
            title=fix_mojibake((item.get("title") or "").strip()),
            description=html_to_text(item.get("description")),
            posted_at=parse_rfc822(item.get("pubdate")),
            remote=remote,
            tags=item.all("category"),
        )


# --- We Work Remotely ---------------------------------------------------------------------------


def _wwr_location(item: RssItem) -> str | None:
    """The country list when the feed has one ("Germany, Poland"), else the region ("Anywhere in the World").

    WWR fills <region> with "Anywhere in the World" even when <country> lists the only countries
    allowed, so the country list is the stricter, truer statement.
    """
    country = _FLAG.sub("", item.get("country") or "")
    country = re.sub(r"\s+", " ", country).strip(" ,")
    return country or item.get("region")


class WeWorkRemotelyAdapter:
    """config {"type": "weworkremotely", "categories": ["back-end-programming", "full-stack-programming"]}."""

    name = "weworkremotely"
    feed = "https://weworkremotely.com/categories/remote-{category}-jobs.rss"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for category in config.get("categories") or ["back-end-programming"]:
            if not re.fullmatch(r"[a-z0-9-]+", category):
                raise SourceError(f"weworkremotely: invalid category slug {category!r}")
            text = self.http.get_text(self.feed.format(category=category), accept=RSS_ACCEPT)
            postings.extend(self.parse(item) for item in parse_rss(text, source="weworkremotely"))
        return [p for p in postings if matches_keywords(p, config.get("keywords") or [])]

    @staticmethod
    def parse(item: RssItem) -> JobPosting:
        link = item.get("link") or item.get("guid") or ""
        raw_title = fix_mojibake((item.get("title") or "").strip())
        company, sep, title = raw_title.partition(": ")
        if not sep:
            company, title = "", raw_title
        tags = [t.strip() for t in re.split(r",| and ", item.get("skills") or "") if t.strip()]
        if item.get("category"):
            tags.append(item.get("category"))
        return JobPosting(
            source="weworkremotely",
            external_id=urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1] or link,
            url=link,
            title=title.strip(),
            company=company.strip() or None,
            location=_wwr_location(item),
            description=html_to_text(item.get("description")),
            posted_at=parse_rfc822(item.get("pubdate")),
            remote=True,
            tags=tags,
            employment_type=normalize_employment_type(item.get("type")),
            extra={"expires_at": item.get("expires_at")} if item.get("expires_at") else {},
        )


# --- Jobicy -------------------------------------------------------------------------------------


class JobicyAdapter:
    """config {"type": "jobicy", "count": 50, "queries": [{"geo": "anywhere", "industry": "engineering"}]}."""

    name = "jobicy"
    url = "https://jobicy.com/api/v2/remote-jobs"
    PARAMS = {"count", "geo", "industry", "tag"}

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for params in _queries(config, self.PARAMS, "jobicy"):
            params = {"count": 50, **params}
            data = self.http.get_json(self.url, params=params)
            if data.get("success") is False:
                raise SourceError(f"jobicy: {data.get('error') or 'request failed'} ({params})")
            check_applied_filters(data.get("appliedFilters"), params)
            postings.extend(self.parse(j) for j in data.get("jobs") or [] if j.get("id") and j.get("url"))
        return [p for p in postings if matches_keywords(p, config.get("keywords") or [])]

    @staticmethod
    def parse(j: dict[str, Any]) -> JobPosting:
        levels = j.get("jobLevel")
        return JobPosting(
            source="jobicy",
            external_id=str(j["id"]),
            url=j["url"],
            title=fix_mojibake(html_to_text(j.get("jobTitle")) or "").strip(),
            company=fix_mojibake(j.get("companyName") or "") or None,
            location=re.sub(r"\s*,\s*", ", ", j.get("jobGeo") or "").strip() or None,
            description=html_to_text(j.get("jobDescription")) or j.get("jobExcerpt"),
            posted_at=parse_datetime(j.get("pubDate")),
            remote=True,
            tags=[t for t in j.get("jobIndustry") or [] if isinstance(t, str)],
            salary_min=to_number(j.get("salaryMin")),
            salary_max=to_number(j.get("salaryMax")),
            salary_currency=j.get("salaryCurrency") or None,
            salary_period=normalize_period(j.get("salaryPeriod")),
            employment_type=normalize_employment_type(j.get("jobType")),
            seniority=normalize_seniority(levels if isinstance(levels, str) else None),
        )


def check_applied_filters(applied: dict[str, Any] | None, sent: dict[str, Any]) -> None:
    """Jobicy echoes the filters it applied; a filter we sent that is missing there was dropped.

    Values may be normalized (industry "dev" comes back as "engineering"), so only presence is checked.
    """
    if applied is None:
        return
    missing = [k for k in sent if k not in applied]
    if missing:
        raise SourceError(f"jobicy ignored filter(s) {missing}; applied: {applied}")


# --- Working Nomads -----------------------------------------------------------------------------


class WorkingNomadsAdapter:
    """config {"type": "workingnomads", "categories": ["Development"], "keywords": ["python"]}."""

    name = "workingnomads"
    url = "https://www.workingnomads.com/api/exposed_jobs/"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        data = self.http.get_json(self.url)
        if not isinstance(data, list):
            raise SourceError("workingnomads: expected a JSON list")
        categories = {c.lower() for c in config.get("categories") or []}
        rows = [j for j in data if isinstance(j, dict) and j.get("url") and j.get("title")]
        if categories:
            rows = [j for j in rows if (j.get("category_name") or "").lower() in categories]
        postings = [self.parse(j) for j in rows]
        return [p for p in postings if matches_keywords(p, config.get("keywords") or [])]

    @staticmethod
    def parse(j: dict[str, Any]) -> JobPosting:
        job_id = re.search(r"/job/go/(\d+)", j["url"])
        tags = [t.strip() for t in (j.get("tags") or "").split(",") if t.strip()]
        if j.get("category_name"):
            tags.append(j["category_name"])
        title = fix_mojibake(j["title"].strip())
        return JobPosting(
            source="workingnomads",
            external_id=job_id.group(1) if job_id else j["url"],
            url=j["url"],
            title=title,
            company=fix_mojibake(j.get("company_name") or "") or None,
            location=(j.get("location") or "").strip() or None,
            description=html_to_text(j.get("description")),
            posted_at=parse_datetime(j.get("pub_date")),
            remote=True,
            tags=tags,
        )
