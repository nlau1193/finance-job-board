"""Sanitize third-party job-description HTML for safe in-board rendering.

ATS descriptions are HTML authored by employers. We allowlist a small set of
formatting tags, drop everything else (keeping text), strip all attributes
except safe `href`s on links, and remove `<script>`/`<style>` entirely. The
result is clean, safe formatting HTML suitable to inject into the board.

Greenhouse returns the description HTML-entity-encoded (`&lt;p&gt;`); Ashby and
Lever return raw HTML. `clean_description` handles both.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Formatting tags we keep. Everything else is unwrapped (text kept, tag dropped).
_ALLOWED = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "a", "hr",
}
# Tags whose entire contents we drop (not just the tag).
_DROP_CONTENT = {"script", "style", "head", "title", "noscript", "iframe", "svg"}
_VOID = {"br", "hr"}
_SAFE_HREF = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag not in _ALLOWED:
            return
        if tag == "a":
            href = next((v for k, v in attrs if k == "href" and v), None)
            if href and _SAFE_HREF.match(href.strip()):
                self.out.append(f'<a href="{html.escape(href.strip())}" target="_blank" rel="noopener noreferrer">')
            else:
                self.out.append("<a>")
        elif tag in _VOID:
            self.out.append(f"<{tag}>")
        else:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in _ALLOWED and tag not in _VOID:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        return "".join(self.out)


def _looks_entity_encoded(text: str) -> bool:
    # Greenhouse content arrives as "&lt;p&gt;…"; raw HTML has actual "<p>".
    return "&lt;" in text and "<" not in text.split("&lt;", 1)[0][-40:]


def clean_description(raw: str | None, *, max_chars: int = 60_000) -> str:
    """Return safe formatting HTML, or '' for empty input."""
    if not raw or not isinstance(raw, str):
        return ""
    text = raw
    # Unescape entity-encoded HTML (Greenhouse) so the parser sees real tags.
    if "&lt;" in text or "&gt;" in text:
        text = html.unescape(text)
    parser = _Sanitizer()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 — never let a malformed description break a refresh
        return ""
    cleaned = parser.result().strip()
    # Collapse runs of empty paragraphs / excess whitespace.
    cleaned = re.sub(r"(?:<p>\s*</p>\s*){2,}", "<p></p>", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "<p>…</p>"
    return cleaned
