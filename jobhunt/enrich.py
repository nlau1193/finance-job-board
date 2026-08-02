"""Per-job enrichment — the local decision-support sidebar (no scraping).

Everything here is derived from data we already fetched (the JD text + the full
per-company board) or from the applicant's own official LinkedIn Connections.csv export.
No network, no ToS/ban surface. Each enricher graceful-degrades to empty.

Company-keyed by design: ~90 postings collapse to ~40 companies, so momentum +
warm-path are computed once per company. Attaches a dict to `opp.enrichment`.
"""

from __future__ import annotations

import csv
import html
import re
from datetime import datetime, timezone
from pathlib import Path

from .application import application_summary
from .model import Opportunity

ROOT = Path(__file__).resolve().parents[1]
CONNECTIONS_CSV = ROOT / "data" / "connections.csv"
MOMENTUM_SNAPSHOT = ROOT / "data" / ".momentum.json"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TENURE_RE = re.compile(r"\b(\d{2})\+?\s*(?:years|yrs)\b", re.IGNORECASE)
# "$120,000 - $160,000", "$120K – $160K", "$120K-$160K/yr"
_COMP_RANGE_RE = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})?(?:\.\d+)?\s?[kK]?)\s?(?:-|–|—|to)\s?\$?\s?(\d{2,3}(?:,\d{3})?(?:\.\d+)?\s?[kK]?)"
)
_COMPANY_SUFFIX_RE = re.compile(r"\b(inc|llc|l\.l\.c|corp|corporation|co|ltd|the|labs|technologies|inc\.)\b", re.IGNORECASE)


def _text_of(html_str: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", html_str or ""))).strip()


def _norm_company(name: str) -> str:
    n = _COMPANY_SUFFIX_RE.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", n)


# --- Fit & Triage ----------------------------------------------------------

def _has_word(text: str, term: str) -> bool:
    """Word-boundary match so 'analyst i' doesn't hit 'analyst ii' and 'arr' doesn't hit 'narrow'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text) is not None


def fit_assessment(opp: Opportunity, fit: dict) -> dict:
    if not any(fit.get(key) for key in ("skills", "too_junior", "too_senior", "gatekeepers")):
        return {}
    title = (opp.title or "").lower()
    body = _text_of(opp.description_html).lower()
    hay = f"{title} {body}"

    why = [s for s in fit.get("skills", []) if _has_word(hay, s)][:4]
    gatekeepers = [g for g in fit.get("gatekeepers", []) if _has_word(hay, g)]
    red_flags = []
    for m in _TENURE_RE.finditer(body):
        if int(m.group(1)) >= 10:
            red_flags.append(f"asks for {m.group(1)}+ years"); break

    too_junior = any(_has_word(title, t) for t in fit.get("too_junior", []))
    too_senior = any(_has_word(title, t) for t in fit.get("too_senior", []))
    if too_junior:
        red_flags.append("below your level")
    if too_senior:
        red_flags.append("a level up (stretch)")

    if too_junior:
        bucket = "SKIP"
    elif too_senior or red_flags or len(gatekeepers) >= 2 or len(why) < 2:
        bucket = "STRETCH"
    else:
        bucket = "APPLY"

    return {
        "bucket": bucket,
        "why": why,
        "missing": [g for g in gatekeepers],   # honest: "mentions CPA" etc.
        "red_flags": red_flags,
    }


# --- Comp ------------------------------------------------------------------

def _fmt_amt(raw: str) -> str:
    raw = raw.strip().replace(",", "").lower()
    try:
        if raw.endswith("k"):
            return f"${int(float(raw[:-1]))}K"
        n = float(raw)
        return f"${int(round(n/1000))}K" if n >= 1000 else f"${int(n)}"
    except ValueError:
        return f"${raw}"


def parse_comp(opp: Opportunity) -> str | None:
    if opp.compensation:
        return opp.compensation
    body = _text_of(opp.description_html)
    m = _COMP_RANGE_RE.search(body)
    if not m:
        return None
    lo, hi = _fmt_amt(m.group(1)), _fmt_amt(m.group(2))
    return f"{lo}–{hi}"


# --- Company momentum ------------------------------------------------------

def company_momentum(filtered: list[Opportunity], raw_all: list[Opportunity],
                     prev: dict) -> dict:
    total, matching = {}, {}
    for o in raw_all:
        total[o.company] = total.get(o.company, 0) + 1
    for o in filtered:
        matching[o.company] = matching.get(o.company, 0) + 1
    out = {}
    for company in matching:
        prior_data = prev.get(company) or {}
        # The board is category-neutral. A legacy finance-only baseline cannot
        # truthfully describe a new all-role search, so it must not seed a delta.
        prior = prior_data.get("matching")
        delta = (matching[company] - prior) if isinstance(prior, int) else None
        out[company] = {
            "total_roles": total.get(company, matching[company]),
            "matching_roles": matching[company],
            "matching_delta": delta,
        }
    return out


# --- Warm path (Connections.csv) ------------------------------------------

def load_connections(path: Path = CONNECTIONS_CSV) -> dict[str, list[dict]]:
    """company-normalized -> [{name, position}]. Empty if no CSV."""
    if not Path(path).exists():
        return {}
    by_company: dict[str, list[dict]] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        # LinkedIn prepends a "Notes:" preamble; skip to the header row.
        lines = text.splitlines()
        start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("first name")), 0)
        reader = csv.DictReader(lines[start:])
        for row in reader:
            company = (row.get("Company") or "").strip()
            if not company:
                continue
            first = (row.get("First Name") or "").strip()
            last = (row.get("Last Name") or "").strip()
            position = (row.get("Position") or "").strip()
            by_company.setdefault(_norm_company(company), []).append(
                {"name": f"{first} {last}".strip(), "position": position}
            )
    except (OSError, csv.Error):
        return {}
    return by_company


def warm_path(opp: Opportunity, connections: dict[str, list[dict]]) -> dict:
    people = connections.get(_norm_company(opp.company), [])
    if not people:
        return {}

    # Put people whose public position overlaps the role near the top. This is
    # category-neutral and gives a useful warm path for engineering, design,
    # sales, operations, or any other search.
    role_words = {
        word for word in re.findall(r"[a-z][a-z0-9+#&.-]{2,}", (opp.title or "").lower())
        if word not in {"senior", "staff", "principal", "lead", "manager", "the", "and"}
    }

    def relevance(person: dict) -> int:
        position = (person.get("position") or "").lower()
        return sum(1 for word in role_words if _has_word(position, word))

    people = sorted(people, key=lambda p: (-relevance(p), p["name"].lower()))
    return {"count": len(people), "people": [{"name": p["name"], "position": p["position"]} for p in people[:3]]}


# --- Freshness -------------------------------------------------------------

def freshness(opp: Opportunity, now: datetime | None = None) -> dict:
    if not isinstance(opp.posted_at, str) or not opp.posted_at:
        return {}
    try:
        dt = datetime.fromisoformat(opp.posted_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {}
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (now or datetime.now(timezone.utc)) - dt
    d = max(0, days.days)
    if d <= 1:
        return {"days": d, "badge": "apply today", "hot": True}
    if d <= 7:
        return {"days": d, "badge": "this week", "hot": True}
    return {"days": d, "badge": f"{d}d ago", "hot": False}


# --- Orchestrator ----------------------------------------------------------

def momentum_snapshot(momentum: dict) -> dict:
    """Return the small durable snapshot used for the next refresh delta."""
    return {
        company: {
            "matching": details["matching_roles"],
            "total": details["total_roles"],
        }
        for company, details in momentum.items()
    }


def write_momentum_snapshot(snapshot: dict) -> None:
    """Persist a snapshot after the caller has accepted the refresh.

    Refresh publication is fail-closed. Keeping this write separate from
    enrichment prevents a failed/partial refresh from advancing the baseline
    used to describe next week's momentum.
    """
    import json

    try:
        from .store import atomic_write_text
        atomic_write_text(MOMENTUM_SNAPSHOT, json.dumps(snapshot))
    except OSError:
        pass


def enrich_all(filtered: list[Opportunity], raw_all: list[Opportunity], *,
               fit: dict, connections_path: Path = CONNECTIONS_CSV,
               now: datetime | None = None,
               persist_snapshot: bool = True) -> dict:
    import json
    prev = {}
    if Path(MOMENTUM_SNAPSHOT).exists():
        try:
            prev = json.loads(Path(MOMENTUM_SNAPSHOT).read_text(encoding="utf-8"))
            if not isinstance(prev, dict):
                prev = {}
        except (ValueError, OSError):
            prev = {}

    from .linkedin import linkedin_links

    connections = load_connections(connections_path)
    momentum = company_momentum(filtered, raw_all, prev)

    for opp in filtered:
        comp = parse_comp(opp)
        if comp and not opp.compensation:
            opp.compensation = comp
        opp.enrichment = {
            "fit": fit_assessment(opp, fit),
            "momentum": momentum.get(opp.company, {}),
            "warm": warm_path(opp, connections),
            "freshness": freshness(opp, now=now),
            "linkedin": linkedin_links(opp.company),
            "application": application_summary(opp.application),
        }

    snap = momentum_snapshot(momentum)
    if persist_snapshot:
        write_momentum_snapshot(snap)

    return {"connections_loaded": bool(connections),
            "warm_companies": sum(1 for o in filtered if o.enrichment.get("warm")),
            "momentum_snapshot": snap}
