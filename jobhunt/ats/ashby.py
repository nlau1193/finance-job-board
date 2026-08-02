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
    opportunities: list[Opportunity] = []
    for job in jobs:
        opp = _to_opportunity(job, company, slug)
        if opp is not None:
            opportunities.append(opp)

    receipt.update(result="ok", count=len(opportunities), raw=len(jobs))
    return opportunities, receipt


def _to_opportunity(job: dict, company: dict, slug: str) -> Opportunity | None:
    if not isinstance(job, dict):
        return None
    # Skip explicitly unlisted postings if the flag is present.
    if job.get("isListed") is False:
        return None
    job_id = str(job.get("id") or "").strip()
    url = (job.get("jobUrl") or job.get("applyUrl") or "").strip()
    title = (job.get("title") or "").strip()
    if not (job_id and url and title):
        return None

    location = (job.get("location") or "").strip()
    department = (job.get("department") or "").strip() or None
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
        posted_at=job.get("publishedAt"),
        tags=list(company.get("tags", [])),
        description_html=clean_description(job.get("descriptionHtml")),
        employment_type=normalize_employment(job.get("employmentType")),
        workplace_type=normalize_workplace(job.get("workplaceType")),
        team=(job.get("team") or "").strip() or None,
        compensation=(job.get("compensationTierSummary") or "").strip() or None,
    )
