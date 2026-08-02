"""Greenhouse board fetcher.

Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
Each record's `absolute_url` is the canonical, deep-linkable posting URL. It may
route through a host's own `/jobs/search?gh_jid=<id>` path (Stripe) — that is a
valid deep-link, handled by `is_actionable_url`.

`content=true` is the only board-level call that returns `departments`, and the
filter needs them: a role whose useful search term appears only in its team can
otherwise be invisible on title alone. The payload is heavier (it also carries
every JD), but it's one call per company and disk-cached like everything else;
descriptions still come from the per-job hydration pass, which the filtered set
needs anyway for the application-form preview.
"""

from __future__ import annotations

from ..model import Opportunity
from ._http import FetchError, get_json

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def fetch(company: dict, *, session=None, ttl: int = 3600, use_cache: bool = True):
    slug = company["slug"]
    url = API.format(slug=slug)
    receipt = {"company": company.get("name", slug), "slug": slug, "ats": "greenhouse"}
    try:
        data = get_json(url, session=session, ttl=ttl, use_cache=use_cache)
    except FetchError as exc:
        receipt.update(result="error", error=str(exc), count=0)
        return [], receipt

    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    if not isinstance(jobs, list):
        jobs = []
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
    job_id = str(job.get("id") or job.get("internal_job_id") or "").strip()
    raw_url = job.get("absolute_url") or ""
    raw_title = job.get("title") or ""
    if not isinstance(raw_url, str) or not isinstance(raw_title, str):
        return None
    url = raw_url.strip()
    title = raw_title.strip()
    if not (job_id and url and title):
        return None

    location_data = job.get("location") or {}
    if not isinstance(location_data, dict):
        return None
    raw_location = location_data.get("name") or ""
    if not isinstance(raw_location, str):
        return None
    location = raw_location.strip()
    posted = job.get("first_published") or job.get("updated_at")
    return Opportunity(
        id=Opportunity.make_id("greenhouse", slug, job_id),
        company=company.get("name", slug),
        title=title,
        location=location,
        url=url,
        ats="greenhouse",
        company_slug=slug,
        job_id=job_id,
        department=_department(job),
        remote="remote" in location.lower(),
        posted_at=posted,
        tags=list(company.get("tags", [])),
    )


def _department(job: dict) -> str | None:
    """Join the posting's department names ("Product / Design"), or None."""
    departments = job.get("departments") or []
    if not isinstance(departments, list):
        return None
    names = [d.get("name").strip() for d in departments
             if isinstance(d, dict) and isinstance(d.get("name"), str)
             and d.get("name").strip()]
    return " / ".join(names) or None
