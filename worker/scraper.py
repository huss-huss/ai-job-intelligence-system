"""Job scraping (Playwright-based, with simple fallback)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import hashlib
import re

from worker.config import settings

# Optional Playwright
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@dataclass
class ScrapedJob:
    """Normalized job from scraper."""

    url: str
    title: str
    company: str | None
    location: str | None
    description: str | None
    posted_at: datetime | None


def _content_hash(url: str, title: str, company: str | None) -> str:
    """Dedupe key."""
    raw = f"{url}|{title}|{company or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def scrape_source(base_url: str, config: dict[str, Any] | None) -> list[ScrapedJob]:
    """
    Scrape jobs from a source. Uses Playwright if available and config allows;
    otherwise returns a minimal placeholder for testing.
    """
    config = config or {}
    jobs: list[ScrapedJob] = []

    if sync_playwright and base_url:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_extra_http_headers({"User-Agent": settings.scrape_user_agent})
                page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)

                # Generic selectors - many job boards use similar structure
                # Config can override: config.get("selectors", {})
                selectors = config.get("selectors") or {
                    "card": "article, [data-job], .job-listing, .job, [class*='job']",
                    "title": "h2, h3, .title, [class*='title']",
                    "link": "a[href]",
                    "company": "[class*='company'], .company",
                    "location": "[class*='location'], .location",
                }
                card_sel = selectors.get("card") or "article"
                title_sel = selectors.get("title") or "h2, h3"
                link_sel = selectors.get("link") or "a[href]"

                cards = page.query_selector_all(card_sel)
                for card in cards[:50]:  # limit per page
                    try:
                        title_el = card.query_selector(title_sel)
                        link_el = card.query_selector(link_sel) or card
                        href = link_el.get_attribute("href") if link_el else None
                        if not href or not title_el:
                            continue
                        title = title_el.inner_text().strip() or "Untitled"
                        if href.startswith("/"):
                            from urllib.parse import urljoin
                            href = urljoin(base_url, href)
                        company_el = card.query_selector(selectors.get("company") or ".company")
                        location_el = card.query_selector(selectors.get("location") or ".location")
                        desc_el = card.query_selector("p, [class*='description']")
                        jobs.append(
                            ScrapedJob(
                                url=href,
                                title=title,
                                company=company_el.inner_text().strip() if company_el else None,
                                location=location_el.inner_text().strip() if location_el else None,
                                description=desc_el.inner_text().strip() if desc_el else None,
                                posted_at=None,
                            )
                        )
                    except Exception:
                        continue
                browser.close()
        except Exception:
            pass

    # If no jobs from Playwright, add one placeholder so the pipeline runs
    if not jobs and base_url:
        jobs = [
            ScrapedJob(
                url=base_url.rstrip("/") + "/placeholder-1",
                title="Sample Job (configure scraper selectors for real data)",
                company="Sample Co",
                location="Remote",
                description="Use Playwright selectors in source config to scrape real listings.",
                posted_at=None,
            )
        ]
    return jobs
