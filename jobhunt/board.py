"""Render the self-contained board HTML from the stored opportunities.

The board is a single file with inline CSS/JS — `open artifacts/board/index.html`
just works, no server, even offline. Data is injected into a JSON <script> tag.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from pathlib import Path

from .model import Opportunity
from .store import atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "templates" / "board.html"
OUTPUT_PATH = ROOT / "artifacts" / "board" / "index.html"
_COMPACT_THRESHOLD_BYTES = 4_000_000
_COMPACT_THRESHOLD_OPPORTUNITIES = 300
_DESCRIPTION_PREVIEW_CHARS = 700


def _safe_json(data: dict) -> str:
    """JSON safe to embed inside a <script> tag (no </script> breakout).

    `<` / `>` are escaped to their \\uXXXX forms; U+2028/U+2029 are escaped
    because, although valid JSON, they break some legacy parsers.
    """
    text = json.dumps(data, ensure_ascii=False)
    text = text.replace("<", "\\u003c").replace(">", "\\u003e")
    text = text.replace(" ", "\\u2028").replace(" ", "\\u2029")
    return text


def build_board_data(opportunities: list[Opportunity], *, generated_at: str,
                     meta: dict | None = None) -> dict:
    companies = sorted({o.company for o in opportunities})
    full_meta = dict(meta or {})
    full_meta.setdefault("companies_with_postings", len(companies))
    return {
        "version": 1,
        "generated_at": generated_at,
        "meta": full_meta,
        "opportunities": [o.to_dict() for o in opportunities],
    }


def _description_preview(raw: object) -> str:
    """Keep a readable, safe excerpt when the all-role board is very large."""
    text = re.sub(r"<[^>]*>", " ", str(raw or ""))
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _DESCRIPTION_PREVIEW_CHARS:
        text = text[:_DESCRIPTION_PREVIEW_CHARS].rsplit(" ", 1)[0].rstrip() + "…"
    return f"<p>{html_lib.escape(text)}</p>" if text else ""


def _compact_application(raw: object) -> dict:
    """Keep effort/count signals while dropping repeated free-form form text."""
    if not isinstance(raw, dict):
        return {}
    keep = {key: raw[key] for key in ("effort", "prompt_count", "flags", "extractable") if key in raw}
    if "prompt_count" not in keep and isinstance(raw.get("prompts"), list):
        keep["prompt_count"] = len(raw["prompts"])
    return keep


def _browser_payload(board_data: dict) -> dict:
    """Return a light browser view without changing the complete local board file."""
    opportunities = board_data.get("opportunities") or []
    estimated_bytes = sum(
        len(str(item.get("description_html") or ""))
        + len(json.dumps(item.get("application") or {}, ensure_ascii=False))
        for item in opportunities
        if isinstance(item, dict)
    )
    if len(opportunities) <= _COMPACT_THRESHOLD_OPPORTUNITIES and estimated_bytes <= _COMPACT_THRESHOLD_BYTES:
        return board_data

    compact = dict(board_data)
    compact_meta = dict(board_data.get("meta") or {})
    compact_meta["board_compacted"] = True
    compact_meta["board_compacted_postings"] = len(opportunities)
    compact["meta"] = compact_meta
    compact["opportunities"] = []
    for item in opportunities:
        if not isinstance(item, dict):
            continue
        view = dict(item)
        view["description_html"] = _description_preview(item.get("description_html"))
        view["application"] = _compact_application(item.get("application"))
        compact["opportunities"].append(view)
    return compact


def render(board_data: dict, *, template_path: Path = TEMPLATE_PATH,
           out_path: Path = OUTPUT_PATH) -> Path:
    template = Path(template_path).read_text(encoding="utf-8")
    html = template.replace("__BOARD_DATA__", _safe_json(_browser_payload(board_data)))
    # Atomic: the local server can be serving this exact file mid-refresh.
    atomic_write_text(out_path, html)
    return Path(out_path)
