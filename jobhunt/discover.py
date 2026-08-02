"""Company -> right ATS fetcher -> normalized, actionable Opportunity[].

This is where the actionable-URL gate is enforced as a hard invariant: any
record whose URL is not a specific posting deep-link is dropped here and counted
in the receipt, so the board can never show a search page even if a fetcher
emits one.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from urllib.parse import urlparse

from . import application
from .model import Opportunity, is_actionable_url, normalize_employment
from .sanitize import clean_description
from .ats import greenhouse, ashby, lever, workday
from .ats._http import new_session, get_json, post_json, FetchError

FETCHERS = {
    "greenhouse": greenhouse.fetch,
    "ashby": ashby.fetch,
    "lever": lever.fetch,
    "workday": workday.fetch,
}

DEFAULT_MAX_WORKERS = 8

_EMPLOYER_HOST_ALIASES = {
    # Exact exceptions for branded careers/vendor hosts. Never approve an
    # arbitrary host just because it contains a company name.
    "abnormalsecurity": {"abnormal.ai"},
    "alloy": {"www.alloy.com"},
    "airbnb": {"careers.airbnb.com"},
    "betterment": {"www.betterment.com"},
    "block": {"block.xyz"},
    "brex": {"www.brex.com"},
    "carvana": {"www.carvana.com"},
    "classpass": {"www.playlist.com"},
    "coinbase": {"www.coinbase.com"},
    "cockroachlabs": {"www.cockroachlabs.com"},
    "collectivehealth": {"jobs.collectivehealth.com"},
    "cribl": {"cribl.io"},
    "disco": {"www.csdisco.com"},
    "databricks": {"databricks.com"},
    "datadog": {"careers.datadoghq.com"},
    "dropbox": {"jobs.dropbox.com"},
    "fanduel": {"www.fanduel.careers"},
    "fivetran": {"www.fivetran.com"},
    "flatironhealth": {"flatiron.com"},
    "instacart": {"instacart.careers"},
    "klaviyo": {"www.klaviyo.com"},
    "lattice": {"lattice.com"},
    "lyft": {"app.careerpuck.com"},
    "movableink": {"movableink.com"},
    "mongodb": {"www.mongodb.com"},
    "motive": {"wearemotive.com"},
    "nextdoor": {"about.nextdoor.com"},
    "oscar": {"www.hioscar.com"},
    "peloton": {"careers.onepeloton.com"},
    "pinterest": {"www.pinterestcareers.com"},
    "samsara": {"www.samsara.com"},
    "seatgeek": {"seatgeek.com"},
    "sofi": {"sofi.com"},
    "squarespace": {"www.squarespace.com"},
    "stashinvest": {"ats.comparably.com"},
    "stripe": {"stripe.com"},
    "sweetgreen": {"careers.sweetgreen.com"},
    "taboola": {"www.taboola.com"},
    "toast": {"careers.toasttab.com"},
    "tripactions": {"navan.com"},
    "thrivemarket": {"thrivemarketjobs.com"},
    "twochairs": {"www.twochairs.com"},
    "voxmedia": {"boards.greenhouse.io"},
    "yotpo": {"www.yotpo.com"},
    "zola": {"www.zola.com"},
    "roblox": {"careers.roblox.com"},
}


def _official_posting_host(url: str, company: dict) -> bool:
    """Keep Apply links on the company's configured careers surface.

    ``is_actionable_url`` deliberately accepts numeric ``gh_jid`` links on
    custom employer domains because the generic helper has no company context.
    Once a feed is attached to a catalog entry, that context is the trust
    boundary: the link must use an exact configured/allowlisted host or the
    canonical host for that company's ATS. A few legacy official feeds still
    return HTTP; exact-host matching preserves those links without trusting
    arbitrary HTTP domains.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not hostname:
        return False

    allowed = set()
    for value in (company.get("careers_url"), company.get("workday_host")):
        if not value:
            continue
        candidate = value if "://" in str(value) else f"https://{value}"
        try:
            host = (urlparse(candidate).hostname or "").lower().rstrip(".")
        except ValueError:
            host = ""
        if host:
            allowed.add(host)

    ats = (company.get("ats") or "").lower()
    canonical_hosts = {
        "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io"},
        "ashby": {"jobs.ashbyhq.com"},
        "lever": {"jobs.lever.co"},
    }.get(ats, set())
    allowed.update(canonical_hosts)
    if hostname in canonical_hosts:
        # Shared ATS hosts must still point at this catalog entry's board. A
        # valid job id on another company's board is not an official link for
        # the current row.
        slug = str(company.get("slug") or "").strip().casefold()
        parts = [part.casefold() for part in parsed.path.split("/") if part]
        if not slug or not parts or parts[0] != slug:
            return False
        if ats == "greenhouse":
            return len(parts) > 1 and parts[1] == "jobs"
        return True
    if hostname in allowed:
        return True
    aliases = {
        str(host).lower().rstrip(".")
        for host in company.get("official_hosts", [])
        if isinstance(host, str) and host.strip()
    }
    aliases.update(_EMPLOYER_HOST_ALIASES.get(str(company.get("slug", "")).lower(), set()))
    if hostname in aliases:
        return True
    return False


def discover_company(company: dict, *, session=None, ttl: int = 3600,
                     use_cache: bool = True, search_terms=None) -> tuple[list[Opportunity], dict]:
    """Fetch one company and return only actionable opportunities + a receipt."""
    ats = (company.get("ats") or "").lower()
    fetcher = FETCHERS.get(ats)
    if fetcher is None:
        return [], {"company": company.get("name"), "slug": company.get("slug"),
                    "ats": ats, "result": "unknown_ats", "count": 0}

    kwargs = {"session": session, "ttl": ttl, "use_cache": use_cache}
    if ats == "workday":
        kwargs["search_terms"] = search_terms
    opportunities, receipt = fetcher(company, **kwargs)

    actionable, dropped, untrusted = [], 0, 0
    for opp in opportunities:
        if not is_actionable_url(opp.url):
            dropped += 1
        elif not _official_posting_host(opp.url, company):
            untrusted += 1
        else:
            actionable.append(opp)
    if dropped:
        receipt["dropped_non_actionable"] = dropped
    if untrusted:
        receipt["dropped_untrusted_url"] = untrusted
    receipt["count"] = len(actionable)
    return actionable, receipt


def discover_all(companies: list[dict], *, ttl: int = 3600, use_cache: bool = True,
                 max_workers: int = DEFAULT_MAX_WORKERS,
                 progress=None, search_terms=None) -> tuple[list[Opportunity], list[dict]]:
    """Fetch the whole structured-ATS universe concurrently."""
    session = new_session()
    all_opps: list[Opportunity] = []
    receipts: list[dict] = []

    def run(company):
        return discover_company(company, session=session, ttl=ttl, use_cache=use_cache,
                                search_terms=search_terms)

    work = companies
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(run, c): c for c in work}
        done = 0
        for future in as_completed(futures):
            company = futures[future]
            try:
                opps, receipt = future.result()
            except Exception as exc:  # noqa: BLE001 — one bad company never kills the run
                opps, receipt = [], {"company": company.get("name"),
                                     "slug": company.get("slug"),
                                     "ats": company.get("ats"),
                                     "result": "error", "error": str(exc), "count": 0}
            all_opps.extend(opps)
            receipts.append(receipt)
            done += 1
            if progress:
                progress(done, len(work), receipt)

    return all_opps, receipts


_GH_JOB = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}"
_COMP_RE = re.compile(r"salary|compensation|pay range|pay band", re.IGNORECASE)
_ASHBY_ORG_RE = re.compile(r"ashbyhq\.com/([^/]+)/")

# Ashby serves the application form through its public posting GraphQL. `field`
# comes back as a JSON scalar (a whole object), so it takes no sub-selection.
_ASHBY_GQL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
_ASHBY_FORM_QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {"
    " jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName,"
    " jobPostingId: $jobPostingId) { applicationForm { sections { fieldEntries {"
    " field isRequired } } } } }"
)


def _hydrate_greenhouse(opp: Opportunity, session, *, use_cache: bool, forms: bool) -> None:
    """One per-job Greenhouse call fills description (+ metadata) and, when
    `forms`, the application preview — `?questions=true` returns both."""
    url = _GH_JOB.format(slug=opp.company_slug, jid=opp.job_id)
    if forms:
        url += "?questions=true"
    data = get_json(url, session=session, use_cache=use_cache)
    if not isinstance(data, dict):
        return
    if not opp.description_html:
        opp.description_html = clean_description(data.get("content"))
        # Greenhouse stuffs structured bits into metadata[{name,value}].
        for item in data.get("metadata") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = item.get("value")
            if not isinstance(value, str) or not value.strip():
                continue
            if not opp.employment_type and name.strip().lower() in ("time type", "employment type"):
                opp.employment_type = normalize_employment(value)
            elif not opp.compensation and _COMP_RE.search(name) and any(c.isdigit() for c in value):
                opp.compensation = value.strip()
        if not opp.workplace_type and "remote" in (opp.location or "").lower():
            opp.workplace_type = "Remote"
    if forms:
        questions = data.get("questions")
        opp.application = (
            application.extract_greenhouse(questions)
            if isinstance(questions, list)
            else application.not_extractable()
        )


def _hydrate_ashby_form(opp: Opportunity, session, *, use_cache: bool) -> None:
    """Fetch one Ashby posting's application form via the public GraphQL API.
    Description is already present from the bulk list call."""
    match = _ASHBY_ORG_RE.search(opp.url or "")
    org = match.group(1) if match else opp.company_slug
    body = {
        "operationName": "ApiJobPosting",
        "variables": {"organizationHostedJobsPageName": org, "jobPostingId": opp.job_id},
        "query": _ASHBY_FORM_QUERY,
    }
    data = post_json(_ASHBY_GQL, body, session=session, use_cache=use_cache)
    # Ashby's error envelopes vary (list/str payloads seen) — guard every level.
    inner = data.get("data") if isinstance(data, dict) else None
    posting = inner.get("jobPosting") if isinstance(inner, dict) else None
    form = posting.get("applicationForm") if isinstance(posting, dict) else None
    sections = form.get("sections") if isinstance(form, dict) else None
    if isinstance(sections, list):
        opp.application = application.extract_ashby(sections)
    else:
        opp.application = application.not_extractable()


def hydrate_details(opportunities: list[Opportunity], *, session=None,
                    use_cache: bool = True, max_workers: int = 10,
                    forms: bool = True) -> dict:
    """Fill each posting's `description_html` and (when `forms`) `application`.

    Runs on the small, already-filtered set, concurrently:
    - **Greenhouse**: one per-job call (`?questions=true`) fills both description
      and the application form — no extra requests.
    - **Ashby**: description came with the bulk list; one GraphQL call adds the
      form.
    - **Lever / other**: form isn't exposed by the API → marked non-extractable.

    Returns a small summary `{descriptions, forms_extractable, forms_total,
    hydrate_errors}`.
    """
    session = session or new_session()

    for opp in opportunities:
        if opp.ats not in ("greenhouse", "ashby"):
            opp.application = application.not_extractable()

    errors: list[dict] = []

    def fetch_one(opp: Opportunity) -> tuple[bool, bool]:
        try:
            if opp.ats == "greenhouse":
                _hydrate_greenhouse(opp, session, use_cache=use_cache, forms=forms)
            elif opp.ats == "ashby" and forms:
                _hydrate_ashby_form(opp, session, use_cache=use_cache)
        except FetchError:
            errors.append({"id": opp.id, "error": "application form fetch failed"})
            if forms and not opp.application:
                opp.application = application.not_extractable()
        except Exception as exc:  # noqa: BLE001 — one malformed payload never kills the refresh
            errors.append({"id": opp.id, "error": str(exc)})
            if forms and not opp.application:
                opp.application = application.not_extractable()
        return (bool(opp.description_html), bool(opp.application.get("extractable")))

    targets = [o for o in opportunities
               if o.ats == "greenhouse" or (o.ats == "ashby" and forms)]
    if targets:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(fetch_one, targets))

    return {
        "descriptions": sum(1 for o in opportunities if o.description_html),
        "forms_extractable": sum(1 for o in opportunities if o.application.get("extractable")),
        "forms_total": len(opportunities),
        "hydrate_errors": errors,
    }


# Back-compat alias (older callers imported the description-only name).
hydrate_descriptions = hydrate_details
