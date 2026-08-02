"""Workday fetcher unit tests — no network.

Exercises the pure pieces: relative-date parsing ("Posted 17 Days Ago"), the
deep-link URL shape (must pass is_actionable_url), and the requires-browser
receipt when a company lacks host/tenant/site config.
"""

from datetime import datetime, timezone

import pytest

from jobhunt.ats import workday
from jobhunt.model import is_actionable_url


NOW = datetime(2026, 7, 7, tzinfo=timezone.utc)


def test_posted_on_relative_dates():
    assert workday._posted_on_to_iso("Posted Today", NOW) == "2026-07-07"
    assert workday._posted_on_to_iso("Posted Yesterday", NOW) == "2026-07-06"
    assert workday._posted_on_to_iso("Posted 17 Days Ago", NOW) == "2026-06-20"
    assert workday._posted_on_to_iso("", NOW) is None
    assert workday._posted_on_to_iso(None, NOW) is None
    assert workday._posted_on_to_iso("Posted Recently", NOW) is None


def test_posted_on_30_plus_is_a_floor_not_a_date():
    # "Posted 30+ Days Ago" is a floor — the req could be months old. Resolving
    # it to now-30d made stale postings look permanently 30 days old, so they
    # never aged out of the freshness cutoff (and wore a fake "30d ago" badge).
    assert workday._posted_on_to_iso("Posted 30+ Days Ago", NOW) is None
    assert workday._posted_on_is_floor("Posted 30+ Days Ago") is True
    assert workday._posted_on_is_floor("Posted 17 Days Ago") is False
    assert workday._posted_on_is_floor("Posted Today") is False
    assert workday._posted_on_is_floor("") is False
    assert workday._posted_on_is_floor(None) is False


def test_to_opportunity_marks_posted_floor():
    post = {
        "title": "FP&A Manager",
        "externalPath": "/job/US-NY-New-York/FP-A-Manager_R123",
        "locationsText": "New York, NY",
        "postedOn": "Posted 30+ Days Ago",
        "bulletFields": ["R123"],
    }
    o = workday._to_opportunity(post, {"name": "Acme"}, "acme",
                                "acme.wd1.myworkdayjobs.com", "External")
    assert o.posted_at is None and o.posted_is_floor is True
    fresh = workday._to_opportunity(dict(post, postedOn="Posted 3 Days Ago"),
                                    {"name": "Acme"}, "acme",
                                    "acme.wd1.myworkdayjobs.com", "External")
    assert fresh.posted_at is not None and fresh.posted_is_floor is False


def test_to_opportunity_builds_actionable_deeplink():
    post = {
        "title": "Senior Financial Analyst - Sales Finance",
        "externalPath": "/job/US-CA-Santa-Clara/Senior-Financial-Analyst---Sales-Finance_JR2018414",
        "locationsText": "US, CA, Santa Clara",
        "postedOn": "Posted 17 Days Ago",
        "bulletFields": ["JR2018414"],
    }
    o = workday._to_opportunity(post, {"name": "NVIDIA"}, "nvidia",
                               "nvidia.wd5.myworkdayjobs.com", "NVIDIAExternalCareerSite")
    assert o is not None
    assert o.job_id == "JR2018414"
    assert o.url == ("https://nvidia.wd5.myworkdayjobs.com/en-US/"
                     "NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/"
                     "Senior-Financial-Analyst---Sales-Finance_JR2018414")
    assert is_actionable_url(o.url)                 # deep-link recognized
    # relative date resolved to a real ISO date (exact value depends on today)
    assert o.posted_at and len(o.posted_at) == 10 and o.posted_at.count("-") == 2
    assert o.ats == "workday"


def test_to_opportunity_skips_incomplete():
    assert workday._to_opportunity({"title": "x"}, {}, "s", "h", "site") is None  # no path/id
    assert workday._to_opportunity({"externalPath": "/job/x_JR1"}, {}, "s", "h", "site") is None  # no title


def test_location_from_path_fallback():
    # Workday's search endpoint often returns an EMPTY locationsText; the real
    # location lives in the path and must be recovered so foreign/onsite roles
    # get filtered (a Cape Town FP&A role was leaking onto the board).
    post = {
        "title": "Financial Planning & Analysis Manager",
        "externalPath": "/job/South-Africa-Cape-Town---Office/FP-A-Manager_R-0153682-1",
        "locationsText": "",
        "bulletFields": ["R-0153682-1"],
    }
    o = workday._to_opportunity(post, {"name": "Levi Strauss"}, "levi",
                               "levistraussandco.wd5.myworkdayjobs.com", "External")
    assert o.location == "South Africa Cape Town Office"
    # A populated locationsText still wins over the path.
    post2 = dict(post, locationsText="New York, NY")
    o2 = workday._to_opportunity(post2, {"name": "x"}, "x", "h.myworkdayjobs.com", "S")
    assert o2.location == "New York, NY"
    # No location segment (only the title) → empty, not a crash.
    assert workday._location_from_path("/job/Some-Title_R1") == ""


def test_fetch_without_config_requires_browser():
    opps, receipt = workday.fetch({"name": "Acme", "slug": "acme", "ats": "workday"})
    assert opps == []
    assert receipt["result"] == "requires_browser"


WORKDAY = {
    "name": "Acme", "ats": "workday", "slug": "acme",
    "workday_host": "acme.wd1.myworkdayjobs.com",
    "workday_tenant": "acme",
    "workday_site": "External",
}


def test_fetch_uses_broad_search_by_default(monkeypatch):
    seen = []

    def fake_post_json(url, body, **kwargs):
        seen.append(body)
        return {
            "total": 1,
            "jobPostings": [{
                "title": "Product Designer",
                "externalPath": "/job/US-NY-New-York/Product-Designer_R1",
                "locationsText": "New York, NY",
                "bulletFields": ["R1"],
            }],
        }

    monkeypatch.setattr(workday, "post_json", fake_post_json)
    opps, receipt = workday.fetch(WORKDAY, use_cache=False)

    assert len(opps) == 1
    assert receipt["result"] == "ok"
    assert seen[0]["searchText"] == ""


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("bulletFields", {"bad": "shape"}),
        ("externalPath", 123),
        ("title", 123),
        ("locationsText", {"bad": "shape"}),
    ],
)
def test_fetch_skips_malformed_posting_without_poisoning_feed(
    monkeypatch, field, bad_value
):
    bad = {
        "title": "Bad",
        "externalPath": "/job/US-NY/Bad_R1",
        "locationsText": "New York, NY",
        "bulletFields": ["R1"],
    }
    bad[field] = bad_value
    good = {
        "title": "Good",
        "externalPath": "/job/US-NY/Good_R2",
        "locationsText": "New York, NY",
        "bulletFields": ["R2"],
    }

    monkeypatch.setattr(
        workday,
        "post_json",
        lambda url, body, **kwargs: {"total": 2, "jobPostings": [bad, good]},
    )
    opps, receipt = workday.fetch(WORKDAY, use_cache=False)

    assert [opp.title for opp in opps] == ["Good"]
    assert receipt["raw"] == 2
    assert receipt["dropped_malformed"] == 1


def test_fetch_accepts_deduplicated_custom_search_terms(monkeypatch):
    seen = []

    def fake_post_json(url, body, **kwargs):
        seen.append(body["searchText"])
        return {"total": 0, "jobPostings": []}

    monkeypatch.setattr(workday, "post_json", fake_post_json)
    workday.fetch(WORKDAY, use_cache=False,
                  search_terms=["software", "software", " design "])

    assert seen == ["software", "design"]


def test_fetch_reports_broad_search_truncation(monkeypatch):
    monkeypatch.setattr(workday, "_MAX_PAGES", 1)
    post = {
        "title": "Product Designer",
        "externalPath": "/job/US-NY-New-York/Product-Designer_R1",
        "locationsText": "New York, NY",
        "bulletFields": ["R1"],
    }

    monkeypatch.setattr(
        workday,
        "post_json",
        lambda url, body, **kwargs: {"total": 100, "jobPostings": [post] * workday._PAGE},
    )
    _, receipt = workday.fetch(WORKDAY, use_cache=False)

    assert receipt["truncated"] is True
    assert "capped at 20 newest roles" in receipt["warning"]
    assert receipt["pages"] == 1
    assert receipt["page_budget"] == 1


def test_broad_search_uses_the_smaller_any_role_budget(monkeypatch):
    seen_offsets = []
    post = {
        "title": "Product Designer",
        "externalPath": "/job/US-NY-New-York/Product-Designer_R1",
        "locationsText": "New York, NY",
        "bulletFields": ["R1"],
    }

    def fake_post_json(url, body, **kwargs):
        seen_offsets.append(body["offset"])
        return {"total": 1000, "jobPostings": [post] * workday._PAGE}

    monkeypatch.setattr(workday, "post_json", fake_post_json)
    _, receipt = workday.fetch(WORKDAY, use_cache=False)

    assert seen_offsets == list(range(0, 200, workday._PAGE))
    assert receipt["pages"] == workday._BROAD_MAX_PAGES
    assert receipt["page_budget"] == workday._BROAD_MAX_PAGES
    assert receipt["truncated"] is True
    assert "broad search capped at 200 newest roles" in receipt["warning"]


def test_fetch_shares_a_page_budget_across_search_terms(monkeypatch):
    monkeypatch.setattr(workday, "_MAX_PAGES", 2)
    seen = []

    def fake_post_json(url, body, **kwargs):
        seen.append((body["searchText"], body["offset"]))
        return {"total": 100, "jobPostings": [{
            "title": "Role",
            "externalPath": f"/job/US-NY/Role_R{body['offset']}",
            "locationsText": "New York, NY",
            "bulletFields": [f"R{body['offset']}"],
        }] * workday._PAGE}

    monkeypatch.setattr(workday, "post_json", fake_post_json)
    _, receipt = workday.fetch(WORKDAY, use_cache=False,
                               search_terms=["first", "second", "third"])

    assert len(seen) == 2
    assert seen == [("first", 0), ("second", 0)]
    assert receipt["pages"] == 2
    assert receipt["page_budget"] == 2
    assert receipt["terms_completed"] == 0
    assert receipt["truncated"] is True
    assert "searches capped at 40 newest roles across 3 term(s)" in receipt["warning"]


def test_fetch_resets_offset_for_each_search_term(monkeypatch):
    seen = []

    def fake_post_json(url, body, **kwargs):
        seen.append((body["searchText"], body["offset"]))
        if body["searchText"] == "first":
            return {"total": 1, "jobPostings": [{
                "title": "First role",
                "externalPath": "/job/US-NY/First_R1",
                "locationsText": "New York, NY",
                "bulletFields": ["R1"],
            }]}
        return {"total": 0, "jobPostings": []}

    monkeypatch.setattr(workday, "post_json", fake_post_json)
    workday.fetch(WORKDAY, use_cache=False, search_terms=["first", "second"])

    assert seen == [("first", 0), ("second", 0)]


# --- "N Locations" resolution -----------------------------------------------
# Workday's list API reports multi-location postings as an opaque "3 Locations";
# the real list only exists on the job-detail endpoint. Unresolved, the location
# filter can only drop it — hiding NYC roles inside multi-location reqs.

ADOBE = {
    "name": "Adobe", "ats": "workday", "slug": "adobe",
    "workday_host": "adobe.wd5.myworkdayjobs.com",
    "workday_tenant": "adobe",
    "workday_site": "external_experienced",
}


def _multi_loc_opp():
    post = {
        "title": "Finance Lead - GTM Finance",
        "externalPath": "/job/New-York/Finance-Lead---GTM-Finance_R157099",
        "locationsText": "3 Locations",
        "postedOn": "Posted 8 Days Ago",
        "bulletFields": ["R157099"],
    }
    return workday._to_opportunity(
        post, ADOBE, "adobe", ADOBE["workday_host"], ADOBE["workday_site"])


def test_is_multi_location():
    assert workday.is_multi_location("3 Locations")
    assert workday.is_multi_location("11 locations")
    assert workday.is_multi_location(" 2 Locations ")
    assert not workday.is_multi_location("New York, NY")
    assert not workday.is_multi_location("3 Locations in EMEA")  # must be the whole string
    assert not workday.is_multi_location("")
    assert not workday.is_multi_location(None)


def test_resolve_locations_joins_the_real_list(monkeypatch, load_fixture):
    detail = load_fixture("workday_job_detail.json")
    seen_urls = []

    def fake_get_json(url, **kwargs):
        seen_urls.append(url)
        return detail

    monkeypatch.setattr(workday, "get_json", fake_get_json)
    opp = _multi_loc_opp()
    resolved = workday.resolve_locations([opp], [ADOBE])

    assert resolved == 1
    assert opp.location == "New York; Seattle; San Jose"
    # detail endpoint: /wday/cxs/{tenant}/{site}{externalPath}
    assert seen_urls == [
        "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced"
        "/job/New-York/Finance-Lead---GTM-Finance_R157099"
    ]


def test_resolve_locations_fetch_error_keeps_original(monkeypatch):
    def boom(url, **kwargs):
        raise workday.FetchError("HTTP 500")

    monkeypatch.setattr(workday, "get_json", boom)
    opp = _multi_loc_opp()
    resolved = workday.resolve_locations([opp], [ADOBE])
    assert resolved == 0
    assert opp.location == "3 Locations"  # unchanged → original filter outcome


def test_resolve_locations_only_touches_multi_location_workday(monkeypatch):
    calls = []
    monkeypatch.setattr(workday, "get_json",
                        lambda url, **kw: calls.append(url) or {})
    single = workday._to_opportunity(
        {"title": "FP&A Manager",
         "externalPath": "/job/US-NY-New-York/FP-A-Manager_R1",
         "locationsText": "New York, NY", "bulletFields": ["R1"]},
        ADOBE, "adobe", ADOBE["workday_host"], ADOBE["workday_site"])
    unknown_company = _multi_loc_opp()
    unknown_company.company_slug = "not-in-universe"
    assert workday.resolve_locations([single, unknown_company], [ADOBE]) == 0
    assert calls == []  # no detail fetch for either


def test_resolve_locations_empty_detail_is_not_resolved(monkeypatch):
    monkeypatch.setattr(workday, "get_json",
                        lambda url, **kw: {"jobPostingInfo": {"location": ""}})
    opp = _multi_loc_opp()
    assert workday.resolve_locations([opp], [ADOBE]) == 0
    assert opp.location == "3 Locations"
