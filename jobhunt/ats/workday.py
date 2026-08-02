"""Workday fetcher.

Workday is per-tenant: each employer has its own host like
`{tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (a POST search
API). A company config supplies:

    {"name", "slug", "ats": "workday",
     "workday_host": "nvidia.wd5.myworkdayjobs.com",
     "workday_tenant": "nvidia",
     "workday_site": "NVIDIAExternalCareerSite"}

Because a Workday tenant can hold thousands of openings, the optional
`search_terms` are sent to the server and the normal profile filter refines the
result. An empty term asks for the broad board, which is what the public starter
uses. The list API gives `postedOn` as relative text ("Posted 17 Days Ago"),
which we resolve to an approximate date so the freshness cutoff applies to
Workday roles too.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ..model import Opportunity
from ._http import FetchError, get_json, new_session, post_json

# An empty query is Workday's broad-board search. A company can still provide a
# narrower `workday_search` list, and a person's configured title keywords are
# passed in by the discovery layer when they want a focused search. Workday
# returns only ~20 rows per page; keep a bounded page budget across all terms so
# one tenant cannot turn a refresh into hundreds of serial requests.
_SEARCHES = ("",)
_PAGE = 20  # Workday honors ~20 per page regardless of a larger `limit`
_MAX_PAGES = 50  # focused-search budget: 1,000 newest roles per tenant
_BROAD_MAX_PAGES = 10  # any-role budget: 200 newest roles per tenant
_MAX_LOCATION_RESOLVES = 200  # detail GETs per refresh, across all tenants


def fetch(company: dict, *, session=None, ttl: int = 3600, use_cache: bool = True,
          search_terms=None):
    slug = company.get("slug", "")
    receipt = {"company": company.get("name", slug), "slug": slug, "ats": "workday"}

    host = company.get("workday_host")
    tenant = company.get("workday_tenant")
    site = company.get("workday_site")
    if not (host and tenant and site):
        receipt.update(result="requires_browser", count=0,
                       reason="no workday_host/tenant/site configured")
        return [], receipt

    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    http = session or new_session()
    configured = company.get("workday_search")
    if configured:
        searches = configured
    elif search_terms is None:
        searches = _SEARCHES
    else:
        searches = search_terms or _SEARCHES
    searches = tuple(dict.fromkeys(str(term).strip() for term in searches)) or _SEARCHES
    broad_search = not configured and (search_terms is None or not search_terms)
    # Keep the existing `_MAX_PAGES` test seam useful while making the public
    # any-role path much cheaper.  Focused searches also share one total budget;
    # they no longer spend 1,000 pages for every configured keyword.
    page_budget = min(_MAX_PAGES, _BROAD_MAX_PAGES) if broad_search else _MAX_PAGES

    by_id: dict[str, Opportunity] = {}
    raw_total = 0
    malformed = 0
    warnings: list[str] = []
    truncated = False
    pages_used = 0
    term_pages = {term: 0 for term in searches}
    finished_terms: set[str] = set()
    try:
        # Give each term a turn before any term gets a second page. This keeps a
        # long first result set from consuming the entire shared budget and
        # starving later configured keywords. Finished terms drop out, so their
        # unused turns are naturally redistributed to the remaining terms.
        while pages_used < page_budget and len(finished_terms) < len(searches):
            for term in searches:
                if term in finished_terms or pages_used >= page_budget:
                    continue
                page = term_pages[term]
                body = {"appliedFacets": {}, "limit": _PAGE,
                        "offset": page * _PAGE, "searchText": term}
                data = post_json(endpoint, body, session=http, ttl=ttl, use_cache=use_cache)
                pages_used += 1
                term_pages[term] += 1
                postings = data.get("jobPostings", []) if isinstance(data, dict) else []
                if not isinstance(postings, list):
                    postings = []
                total = data.get("total") if isinstance(data, dict) else None
                if not isinstance(total, int):
                    total = None
                raw_total += len(postings)
                for post in postings:
                    opp = _to_opportunity(post, company, slug, host, site)
                    if opp is None:
                        malformed += 1
                        continue
                    by_id[opp.job_id] = opp
                if len(postings) < _PAGE or (total is not None and (page + 1) * _PAGE >= total):
                    finished_terms.add(term)
        if len(finished_terms) < len(searches):
            warnings.append(
                f"{'broad search' if broad_search else 'searches'} capped at "
                f"{page_budget * _PAGE} newest roles across {len(searches)} term(s)"
            )
            truncated = True
    except FetchError as exc:
        if by_id:  # partial success — keep what we got
            warnings.append(str(exc))
            receipt.update(result="ok", count=len(by_id), raw=raw_total,
                           pages=pages_used, page_budget=page_budget,
                           terms_completed=len(finished_terms))
            if warnings:
                receipt["warning"] = "; ".join(warnings)
            if truncated:
                receipt["truncated"] = True
            if malformed:
                receipt["dropped_malformed"] = malformed
            return list(by_id.values()), receipt
        receipt.update(result="error", error=str(exc), count=0,
                       pages=pages_used, page_budget=page_budget,
                       terms_completed=len(finished_terms))
        return [], receipt

    receipt.update(result="ok", count=len(by_id), raw=raw_total,
                   pages=pages_used, page_budget=page_budget,
                   terms_completed=len(finished_terms))
    if warnings:
        receipt["warning"] = "; ".join(warnings)
    if truncated:
        # This is an intentional bounded-budget advisory, not a feed failure.
        receipt["warning_kind"] = "cap"
        receipt["truncated"] = True
    if malformed:
        receipt["dropped_malformed"] = malformed
    return list(by_id.values()), receipt


def _to_opportunity(post: dict, company: dict, slug: str, host: str, site: str):
    if not isinstance(post, dict):
        return None
    raw_path = post.get("externalPath") or ""
    raw_title = post.get("title") or ""
    if not isinstance(raw_path, str) or not isinstance(raw_title, str):
        return None
    path = raw_path.strip()
    title = raw_title.strip()
    bullets = post.get("bulletFields") or []
    if not isinstance(bullets, list):
        return None
    if bullets:
        raw_job_id = bullets[0]
        if (isinstance(raw_job_id, bool)
                or not isinstance(raw_job_id, (str, int))):
            return None
        job_id = str(raw_job_id).strip() or path
    else:
        job_id = path
    if not (path and title and job_id):
        return None

    # Canonical public deep-link: https://{host}/en-US/{site}{externalPath}
    url = f"https://{host}/en-US/{site}{path}"
    # Workday's locationsText is often EMPTY on the search endpoint; the real
    # location is encoded in the path (…/job/{Location-Slug}/{Title}_{id}). Fall
    # back to it so foreign/onsite roles are filtered instead of defaulting to
    # "keep". Without this, a foreign role can leak onto the board.
    raw_location = post.get("locationsText")
    if raw_location is not None and not isinstance(raw_location, str):
        return None
    location = (raw_location or "").strip() or _location_from_path(path)
    return Opportunity(
        id=Opportunity.make_id("workday", slug, str(job_id)),
        company=company.get("name", slug),
        title=title,
        location=location,
        url=url,
        ats="workday",
        company_slug=slug,
        job_id=str(job_id),
        remote="remote" in location.lower(),
        posted_at=_posted_on_to_iso(post.get("postedOn")),
        posted_is_floor=_posted_on_is_floor(post.get("postedOn")),
        tags=list(company.get("tags", [])),
    )

# Workday's list API reports a multi-location posting as an opaque count
# ("3 Locations") — the real list only exists on the job-detail endpoint. A
# multi-location posting can hide a preferred city until this detail call.
_MULTI_LOC_RE = re.compile(r"\d+\s+locations", re.I)
_RESOLVE_MAX_WORKERS = 8


def is_multi_location(location: str | None) -> bool:
    """True for Workday's opaque multi-location form ("2 Locations")."""
    return bool(_MULTI_LOC_RE.fullmatch((location or "").strip()))


def resolve_locations(opportunities, companies: list[dict], *, session=None,
                      ttl: int = 3600, use_cache: bool = True,
                      max_targets: int = _MAX_LOCATION_RESOLVES) -> int:
    """Resolve "N Locations" postings to their real location list, in place.

    One cached detail GET per posting (`/wday/cxs/{tenant}/{site}{externalPath}`
    → `jobPostingInfo.location` + `additionalLocations`). Call it on the small
    title-matched set only — never the whole tenant. A refresh-wide cap keeps a
    broad any-role board from turning multi-location resolution into thousands
    of extra requests; targets are sampled round-robin by company so one tenant
    cannot consume the whole detail budget. A posting whose detail can't be
    fetched keeps its original location (and thus its original filter outcome).
    Returns how many postings were resolved.
    """
    cfg = {c.get("slug"): c for c in companies
           if (c.get("ats") or "").lower() == "workday"}
    candidates = [o for o in opportunities
                  if o.ats == "workday" and is_multi_location(o.location)
                  and cfg.get(o.company_slug)]
    budget = max(0, int(max_targets))
    grouped: dict[str, list[Opportunity]] = {}
    for opp in candidates:
        grouped.setdefault(opp.company_slug, []).append(opp)
    targets = []
    for index in range(max((len(items) for items in grouped.values()), default=0)):
        for items in grouped.values():
            if index < len(items) and len(targets) < budget:
                targets.append(items[index])
        if len(targets) >= budget:
            break
    if not targets:
        return 0

    http = session or new_session()

    def _resolve(opp) -> bool:
        company = cfg[opp.company_slug]
        host = company.get("workday_host")
        tenant = company.get("workday_tenant")
        site = company.get("workday_site")
        if not (host and tenant and site):
            return False
        # The deep-link is https://{host}/en-US/{site}{externalPath}; recover
        # the externalPath (always "/job/…") to hit the CXS detail endpoint.
        marker = f"/{site}/job/"
        idx = (opp.url or "").find(marker)
        if idx < 0:
            return False
        path = opp.url[idx + len(f"/{site}"):]
        try:
            data = get_json(f"https://{host}/wday/cxs/{tenant}/{site}{path}",
                            session=http, ttl=ttl, use_cache=use_cache)
        except FetchError:
            return False
        info = data.get("jobPostingInfo") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            return False
        raw = [info.get("location"), *(info.get("additionalLocations") or [])]
        locations = [str(loc).strip() for loc in raw if loc and str(loc).strip()]
        if not locations:
            return False
        opp.location = "; ".join(dict.fromkeys(locations))
        opp.remote = opp.remote or "remote" in opp.location.lower()
        return True

    with ThreadPoolExecutor(max_workers=_RESOLVE_MAX_WORKERS) as pool:
        return sum(bool(done) for done in pool.map(_resolve, targets))


def _location_from_path(path: str) -> str:
    """Derive a human location from a Workday job path when locationsText is empty.

    "/job/South-Africa-Cape-Town---Office/FP-A-Manager_R-1" → "South Africa Cape
    Town Office". The segment right after "/job/" is the location slug; the last
    segment is the title. Returns "" if the path has no location segment.
    """
    if "/job/" not in path:
        return ""
    tail = path.split("/job/", 1)[1].strip("/").split("/")
    if len(tail) < 2:  # only the title segment — no location encoded
        return ""
    slug = tail[0].replace("-", " ")
    return re.sub(r"\s+", " ", slug).strip()


_DAYS_RE = re.compile(r"(\d+)\s*\+?\s*days?\s*ago", re.I)


def _posted_on_to_iso(posted_on, now: datetime | None = None) -> str | None:
    """Resolve Workday's relative "Posted N Days Ago" to an approximate ISO date.

    "Posted Today" → today, "Posted Yesterday" → -1d, "Posted 17 Days Ago" → -17d.
    Unknown/absent → None (the freshness filter then keeps the role rather than
    guessing). Approximate-by-a-day is fine for a 45-day cutoff.

    "Posted 30+ Days Ago" is a FLOOR, not an age — the posting could be months
    older. Resolving it to -30d made stale reqs look permanently 30 days old
    (they never aged out of the cutoff and wore a fake "30d ago" badge), so it
    resolves to None here; the floor travels separately via _posted_on_is_floor.
    """
    if not posted_on:
        return None
    text = str(posted_on).lower()
    now = now or datetime.now(timezone.utc)
    if "today" in text:
        delta = 0
    elif "yesterday" in text:
        delta = 1
    else:
        m = _DAYS_RE.search(text)
        if not m or "+" in m.group(0):
            return None
        delta = int(m.group(1))
    return (now - timedelta(days=delta)).date().isoformat()


def _posted_on_is_floor(posted_on) -> bool:
    """True for Workday's capped form ("Posted 30+ Days Ago") — a floor, not an age."""
    if not posted_on:
        return False
    m = _DAYS_RE.search(str(posted_on).lower())
    return bool(m and "+" in m.group(0))
