"""Generic Playwright scraper for career pages without an API.

Config: {"type": "html", "url": "...", "selectors": {"card": ..., "title": ..., ...}}.
Prefer an API adapter whenever the site offers one.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urljoin

from worker.sources.base import DEFAULT_USER_AGENT, JobPosting, SourceError

DEFAULT_SELECTORS = {
    "card": "article, [data-job], .job-listing, .job, [class*='job']",
    "title": "h2, h3, .title, [class*='title']",
    "link": "a[href]",
    "company": "[class*='company'], .company",
    "location": "[class*='location'], .location",
    "description": "p, [class*='description']",
}


def _inner_text(card, selector: str) -> str | None:
    el = card.query_selector(selector)
    return el.inner_text().strip() if el else None


class HtmlAdapter:
    name = "html"

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, max_cards: int = 50) -> None:
        self.user_agent = user_agent
        self.max_cards = max_cards

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SourceError(
                "Playwright is not installed: pip install playwright && playwright install chromium"
            ) from exc

        base_url = config["url"]
        selectors = {**DEFAULT_SELECTORS, **(config.get("selectors") or {})}
        postings: list[JobPosting] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=self.user_agent)
                page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1500)
                for card in page.query_selector_all(selectors["card"])[: self.max_cards]:
                    title_el = card.query_selector(selectors["title"])
                    link_el = card.query_selector(selectors["link"])
                    href = link_el.get_attribute("href") if link_el else None
                    if not title_el or not href:
                        continue
                    url = urljoin(base_url, href)
                    postings.append(
                        JobPosting(
                            source="html",
                            external_id=hashlib.sha1(url.encode()).hexdigest()[:16],
                            url=url,
                            title=title_el.inner_text().strip(),
                            company=_inner_text(card, selectors["company"]) or config.get("company"),
                            location=_inner_text(card, selectors["location"]),
                            description=_inner_text(card, selectors["description"]),
                        )
                    )
            finally:
                browser.close()
        return postings
