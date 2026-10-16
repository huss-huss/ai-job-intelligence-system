"""Text helpers: turn job-board HTML into plain text for matching and LLM prompts."""

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def fix_mojibake(value: str) -> str:
    """Repair UTF-8 text that was decoded as Latin-1 upstream ("JumpCloudÂ®" -> "JumpCloud®")."""
    if not any(marker in value for marker in ("Â", "â€", "Ã")):
        return value
    raw = bytearray()
    for ch in value:
        try:
            raw += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) > 0xFF:  # a real non-Latin character: the text wasn't mis-decoded
                return value
            raw.append(ord(ch))  # bytes cp1252 leaves undefined (0x81, 0x8D, 0x9D...) came through as-is
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return value


def html_to_text(value: str | None) -> str:
    """Strip tags, decode entities, and collapse whitespace while keeping paragraph breaks.

    Greenhouse returns HTML that is itself entity-escaped, so unescape before parsing.
    """
    if not value:
        return ""
    raw = fix_mojibake(html.unescape(value) if "&lt;" in value else value)
    parser = _TextExtractor()
    parser.feed(raw)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
