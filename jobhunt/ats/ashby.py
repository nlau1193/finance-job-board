"""Ashby board fetcher.

Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{slug}
Records carry a clean `jobUrl` (jobs.ashbyhq.com/{slug}/{uuid}) plus structured
`department`, `location`, and `isRemote` — so department is available for
filtering here at no extra cost.
"""

from __future__ import annotations

from ..model import Opportunity, normalize_employment, normalize_workplace
from ..sanitize import clean_description
from ._http import FetchError, get_json

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch(company: dict, *, session=None, ttl: int = 3600, use_cache: bool = True):
    slug = company["slug"]
    url = API.format(slug=slug)
    receipt = {"company": company.get("name", slug), "slug": slug, "ats": "ashby"}
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
    # Skip explicitly unlisted postings if the flag is present.
    if job.get("isListed") is False:
        return None
    job_id = str(job.get("id") or "").strip()
    raw_url = job.get("jobUrl") or job.get("applyUrl") or ""
    raw_title = job.get("title") or ""
    if not isinstance(raw_url, str) or not isinstance(raw_title, str):
        return None
    url = raw_url.strip()
    title = raw_title.strip()
    if not (job_id and url and title):
        return None

    raw_location = job.get("location") or ""
    raw_department = job.get("department") or ""
    raw_team = job.get("team") or ""
    raw_compensation = job.get("compensationTierSummary") or ""
    if not all(isinstance(value, str)
               for value in (raw_location, raw_department, raw_team, raw_compensation)):
        return None
    location = raw_location.strip()
    department = raw_department.strip() or None
    posted_at = job.get("publishedAt")
    if posted_at is not None and not isinstance(posted_at, str):
        return None
    posted_at = posted_at.strip() or None if posted_at else None
    return Opportunity(
        id=Opportunity.make_id("ashby", slug, job_id),
        company=company.get("name", slug),
        title=title,
        location=location,
        url=url,
        ats="ashby",
        company_slug=slug,
        job_id=job_id,
        department=department,
        remote=bool(job.get("isRemote")),
        posted_at=posted_at,
        tags=list(company.get("tags", [])),
        description_html=clean_description(job.get("descriptionHtml")),
        employment_type=normalize_employment(job.get("employmentType")),
        workplace_type=normalize_workplace(job.get("workplaceType")),
        team=raw_team.strip() or None,
        compensation=raw_compensation.strip() or None,
    )
