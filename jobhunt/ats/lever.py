"""Lever board fetcher.

Endpoint: GET https://api.lever.co/v0/postings/{slug}?mode=json
Records carry a `hostedUrl` (jobs.lever.co/{slug}/{uuid}) and a `categories`
object with `location`/`team`/`commitment`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..model import Opportunity, normalize_employment, normalize_workplace
from ..sanitize import clean_description
from ._http import FetchError, get_json


def _epoch_ms_to_iso(ms: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _fmt_salary(rng: object) -> str | None:
    if not isinstance(rng, dict):
        return None
    lo, hi = rng.get("min"), rng.get("max")
    cur = {"USD": "$"}.get(rng.get("currency"), rng.get("currency") or "")
    def k(n):
        try:
            n = float(n)
            return f"{cur}{int(round(n/1000))}K" if n >= 1000 else f"{cur}{int(n)}"
        except (TypeError, ValueError):
            return None
    a, b = k(lo), k(hi)
    if a and b:
        return f"{a}–{b}"
    return a or b or None

API = "https://api.lever.co/v0/postings/{slug}?mode=json"


def fetch(company: dict, *, session=None, ttl: int = 3600, use_cache: bool = True):
    slug = company["slug"]
    url = API.format(slug=slug)
    receipt = {"company": company.get("name", slug), "slug": slug, "ats": "lever"}
    try:
        data = get_json(url, session=session, ttl=ttl, use_cache=use_cache)
    except FetchError as exc:
        receipt.update(result="error", error=str(exc), count=0)
        return [], receipt

    jobs = data if isinstance(data, list) else []
    opportunities: list[Opportunity] = []
    malformed = 0
    for job in jobs:
        opp = _to_opportunity(job, company, slug)
        if opp is not None:
            opportunities.append(opp)
        else:
            malformed += 1

    receipt.update(result="ok", count=len(opportunities), raw=len(jobs))
    if malformed:
        receipt["dropped_malformed"] = malformed
    return opportunities, receipt


def _to_opportunity(job: dict, company: dict, slug: str) -> Opportunity | None:
    if not isinstance(job, dict):
        return None
    job_id = str(job.get("id") or "").strip()
    raw_url = job.get("hostedUrl") or ""
    raw_title = job.get("text") or ""
    if not isinstance(raw_url, str) or not isinstance(raw_title, str):
        return None
    url = raw_url.strip()
    title = raw_title.strip()
    if not (job_id and url and title):
        return None

    categories = job.get("categories") or {}
    if not isinstance(categories, dict):
        return None
    raw_location = categories.get("location") or ""
    raw_department = categories.get("team") or categories.get("department") or ""
    raw_workplace = job.get("workplaceType") or ""
    if not all(isinstance(value, str)
               for value in (raw_location, raw_department, raw_workplace)):
        return None
    location = raw_location.strip()
    department = raw_department.strip() or None
    workplace = raw_workplace.lower()

    # Lever splits the body across `description` (intro HTML) + `lists`
    # (structured sections like Responsibilities). Reassemble both.
    body = job.get("description") or job.get("descriptionPlain") or ""
    if not isinstance(body, str):
        body = ""
    parts = [body]
    sections = job.get("lists") or []
    if not isinstance(sections, list):
        sections = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        # NOTE: use a distinct name — reusing `title` here clobbered the job
        # title with the last section heading (e.g. "Qualifications:").
        raw_heading = section.get("text") or ""
        raw_content = section.get("content") or ""
        heading = raw_heading.strip() if isinstance(raw_heading, str) else ""
        content = raw_content if isinstance(raw_content, str) else ""
        if heading:
            parts.append(f"<h4>{heading}</h4>")
        if content:
            parts.append(f"<ul>{content}</ul>")
    description_html = clean_description("".join(parts))
    return Opportunity(
        id=Opportunity.make_id("lever", slug, job_id),
        company=company.get("name", slug),
        title=title,
        location=location,
        url=url,
        ats="lever",
        company_slug=slug,
        job_id=job_id,
        department=department,
        remote=workplace == "remote" or "remote" in location.lower(),
        posted_at=_epoch_ms_to_iso(job.get("createdAt")),
        tags=list(company.get("tags", [])),
        description_html=description_html,
        employment_type=normalize_employment(categories.get("commitment")),
        workplace_type=normalize_workplace(job.get("workplaceType")),
        team=(categories.get("team") or "").strip() or None,
        compensation=_fmt_salary(job.get("salaryRange")),
    )
