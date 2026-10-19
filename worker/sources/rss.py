"""A small RSS 2.0 reader for job feeds (Djinni, We Work Remotely).

Uses the standard library's expat-based parser. Feeds are untrusted input, so documents that
declare a DTD or entities are refused before parsing: job feeds never need them, and refusing
them rules out entity-expansion ("billion laughs") and external-entity tricks without adding a
dependency.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from worker.sources.base import SourceError

_DTD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


@dataclass
class RssItem:
    """One <item>: child tag -> text (namespaces stripped). Repeated tags such as <category> are in `lists`."""

    fields: dict[str, str] = field(default_factory=dict)
    lists: dict[str, list[str]] = field(default_factory=dict)

    def get(self, tag: str, default: str | None = None) -> str | None:
        value = self.fields.get(tag)
        return value if value not in (None, "") else default

    def all(self, tag: str) -> list[str]:
        return [v for v in self.lists.get(tag, []) if v]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_rss(text: str, *, source: str = "rss") -> list[RssItem]:
    """Items of an RSS 2.0 document. Raises SourceError on malformed or DTD-bearing XML."""
    if _DTD.search(text[:4096]):
        raise SourceError(f"{source}: feed declares a DTD/entities; refusing to parse")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SourceError(f"{source}: malformed RSS: {exc}") from exc
    items = []
    for node in root.iter():
        if _local(node.tag) != "item":
            continue
        item = RssItem()
        for child in node:
            tag = _local(child.tag)
            value = (child.text or "").strip()
            item.lists.setdefault(tag, []).append(value)
            item.fields.setdefault(tag, value)
        items.append(item)
    return items
