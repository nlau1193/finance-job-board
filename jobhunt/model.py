"""Core data model for the local job-hunt board.

The `Opportunity` is the normalized record every ATS fetcher emits. The single
most important function here is `is_actionable_url`: it decides whether a URL
deep-links to a *specific* job posting (good) versus a search/listing page
(useless — the recurring "Stripe screenshot" bug in the original tool).

The root-cause fix: a URL that carries a job-id deep-link parameter (Greenhouse
`gh_jid`, an Ashby/Lever posting UUID, a numeric job id) is actionable *even if
its path literally contains the word "search"*. Stripe's real posting URLs look
like `https://stripe.com/jobs/search?gh_jid=7954688` — that IS a deep-link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs


# --- time helpers ----------------------------------------------------------

def utc_now() -> str:
    """ISO-8601 UTC timestamp, second precision, `Z` suffix."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# --- actionable-URL gate (the correctness fix) -----------------------------

# Query parameters that, when present with a value, pin the URL to one posting.
_JOB_ID_QUERY_PARAMS = ("gh_jid",)

# A UUID, as used in Ashby/Lever hosted posting paths.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Path/host patterns that identify a *specific* posting (not a board or search).
_POSTING_PATTERNS = (
    re.compile(rf"(?:^|\.)jobs\.ashbyhq\.com/[^/]+/{_UUID}(?:/|$)"),
    re.compile(rf"(?:^|\.)jobs\.lever\.co/[^/]+/{_UUID}(?:/|$)"),
    re.compile(r"(?:^|\.)(?:job-boards|boards)\.greenhouse\.io/[^/]+/jobs/\d+(?:/|$)"),
    re.compile(r"(?:^|[.\-])myworkdayjobs\.com/.+/job/.+"),
)

# Hosts where a bare path (no id) is always a board/search surface, never a posting.
_NON_POSTING_PATH = re.compile(r"^/?(search|search-results|all-jobs|openings)?/?$")


def is_actionable_url(url: object) -> bool:
    """True iff `url` deep-links to a single, specific job posting.

    Order matters: a job-id query parameter is checked *before* any "search"
    rejection, because real ATS deep-links (Stripe's `…/jobs/search?gh_jid=…`)
    route through a search path but still point at one posting.
    """
    if not isinstance(url, str) or not url.strip():
        return False

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    # 1) A job-id deep-link parameter wins outright — even over "search" in path.
    query = parse_qs(parsed.query)
    for key in _JOB_ID_QUERY_PARAMS:
        values = query.get(key)
        if values and any(v.strip() for v in values):
            return True

    # 2) Known posting host/path shapes for the structured ATS platforms.
    host_path = f"{parsed.netloc}{parsed.path}"
    for pattern in _POSTING_PATTERNS:
        if pattern.search(host_path):
            return True

    return False


# --- Opportunity record ----------------------------------------------------

ATS_PLATFORMS = ("greenhouse", "ashby", "lever", "workday")


def normalize_employment(value: object) -> str | None:
    if not value:
        return None
    key = str(value).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    return {
        "fulltime": "Full-time", "parttime": "Part-time", "contract": "Contract",
        "intern": "Internship", "internship": "Internship", "temporary": "Temporary",
        "freelance": "Freelance", "apprenticeship": "Apprenticeship",
    }.get(key, str(value).strip())


def normalize_workplace(value: object) -> str | None:
    if not value:
        return None
    key = str(value).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    return {"onsite": "On-site", "remote": "Remote", "hybrid": "Hybrid"}.get(key, str(value).strip().title())


@dataclass
class Opportunity:
    """One normalized, deep-linkable job posting."""

    id: str                       # stable dedupe key: "{ats}:{slug}:{job_id}"
    company: str
    title: str
    location: str
    url: str                      # canonical actionable posting URL
    ats: str                      # one of ATS_PLATFORMS
    company_slug: str
    job_id: str
    department: str | None = None
    remote: bool = False
    posted_at: str | None = None  # ISO date/datetime if the ATS provides one
    # True when the ATS only gave a floor, not an age ("Posted 30+ Days Ago"):
    # the posting is AT LEAST that old and could be months older, so the
    # freshness cutoff treats it as beyond max_age instead of trusting a date.
    posted_is_floor: bool = False
    tags: list[str] = field(default_factory=list)
    description_html: str = ""     # sanitized formatting HTML of the posting body
    employment_type: str | None = None   # Full-time / Part-time / Contract …
    workplace_type: str | None = None    # Remote / Hybrid / On-site
    team: str | None = None              # more specific than department when present
    compensation: str | None = None      # only when the employer publishes it
    application: dict = field(default_factory=dict)  # form preview: free-form prompts, gates, effort
    enrichment: dict = field(default_factory=dict)  # sidebar: fit, comp, momentum, warm, freshness

    # Read-state, preserved across refreshes by the store.
    first_seen_at: str = ""
    last_seen_at: str = ""
    read: bool = False
    dismissed: bool = False

    @staticmethod
    def make_id(ats: str, slug: str, job_id: str) -> str:
        return f"{ats}:{slug}:{job_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Opportunity":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def is_actionable(self) -> bool:
        return is_actionable_url(self.url)
