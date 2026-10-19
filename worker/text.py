"""Text helpers: turn job-board HTML into plain text for matching and LLM prompts."""

import html
import re
import unicodedata
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


# Characters a UTF-8 continuation byte (0x80-0xBF) turns into when the bytes are mis-decoded as
# cp1252 (0x80-0x9F -> "€", "™", "”"...) or Latin-1 (0x80-0x9F -> C1 controls U+0080-U+009F).
_CP1252_HIGH = bytes(range(0x80, 0xA0)).decode("cp1252", errors="ignore")
_CONT = f"[\u0080-\u00bf{re.escape(_CP1252_HIGH)}]"
# A mis-decoded UTF-8 sequence: a lead byte rendered as a Latin-1 letter followed by exactly the
# number of continuation bytes it announces (C2-DF: 1, E0-EF: 2, F0-F4: 3).
_MOJIBAKE_SEQ = re.compile(
    f"[\u00c2-\u00df]{_CONT}|[\u00e0-\u00ef]{_CONT}{{2}}|[\u00f0-\u00f4]{_CONT}{{3}}"
)
# "â€" / "â\x80" (E2 80 = start of ’ “ ” – — …) whose third byte was dropped or turned into a space.
# Only two cases are unambiguous:
#   - letter + truncated sequence + optional space + contraction suffix ("Weâ€ re") -> ’
#   - word + truncated sequence + whitespace/punctuation/end ("greatâ€ and") -> ” (0x9D is undefined
#     in cp1252, so a cp1252 decoder drops exactly the last byte of ”)
_TRUNC = "\u00e2[\u20ac\u0080]"
_TRUNC_APOSTROPHE = re.compile(f"(?<=[^\\W\\d_]){_TRUNC}[ \u00a0]?(?=(?:s|t|d|m|re|ll|ve)\\b)")
_TRUNC_CLOSE_QUOTE = re.compile(f"(?<=\\S){_TRUNC}(?=\\s|[.,;:!?)\\]]|$)")


def _decode_seq(match: re.Match) -> str:
    seq = match.group()
    raw = bytearray()
    for ch in seq:
        try:
            raw += ch.encode("cp1252")
        except UnicodeEncodeError:
            raw.append(ord(ch))  # Latin-1 / bytes cp1252 leaves undefined (0x81, 0x8D, 0x9D...)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:  # not a real UTF-8 sequence: legitimate Latin-1 text, keep it
        return seq
    return seq if unicodedata.category(decoded) == "Cn" else decoded  # unassigned: a coincidence


def fix_mojibake(value: str) -> str:
    """Repair UTF-8 text that was decoded as cp1252/Latin-1 upstream.

    "JumpCloudÂ®" -> "JumpCloud®", "Weâ\x80\x99re" -> "We’re", "Weâ€ re" -> "We’re".
    Each mis-decoded sequence is repaired on its own, so text that also contains correct
    non-Latin characters is still fixed, and legitimate words with "â"/"Ã" ("pâte", "SÃO") are
    left alone. Idempotent: running it on already-repaired text changes nothing.
    """
    if not value or not any(marker in value for marker in ("\u00c2", "\u00c3", "\u00e2", "\u00f0")):
        return value
    for _ in range(3):  # text that was mis-decoded twice needs a second pass
        fixed = _MOJIBAKE_SEQ.sub(_decode_seq, value)
        if fixed == value:
            break
        value = fixed
    value = _TRUNC_APOSTROPHE.sub("\u2019", value)
    return _TRUNC_CLOSE_QUOTE.sub("\u201d", value)


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
