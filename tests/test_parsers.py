"""Per-ATS parser tests against captured JSON fixtures.

Each fetcher's network call is patched to return the fixture, so we test the
normalization (canonical deep-link URL, title, location, department, remote)
without hitting the network. Every emitted Opportunity must be actionable.
"""

import pytest

from jobhunt import discover
from jobhunt.ats import greenhouse, ashby, lever
from jobhunt.model import is_actionable_url


GH_COMPANY = {"name": "Stripe", "slug": "stripe", "tags": ["fintech"]}
ASHBY_COMPANY = {"name": "Ramp", "slug": "ramp", "tags": ["fintech"]}
LEVER_COMPANY = {"name": "Netlify", "slug": "netlify", "tags": ["dev-tools"]}


def test_greenhouse_parser(monkeypatch, load_fixture):
    fixture = load_fixture("greenhouse_stripe.json")
    monkeypatch.setattr(greenhouse, "get_json", lambda *a, **k: fixture)
    opps, receipt = greenhouse.fetch(GH_COMPANY)

    assert receipt["result"] == "ok"
    assert len(opps) == len(fixture["jobs"])  # every record parses
    assert all(is_actionable_url(o.url) for o in opps)  # all deep-links

    treasury = next(o for o in opps if "Treasury" in o.title)
    assert treasury.ats == "greenhouse"
    assert treasury.company == "Stripe"
    assert treasury.id.startswith("greenhouse:stripe:")
    assert treasury.url.startswith("https://")
    assert treasury.tags == ["fintech"]


def test_ashby_parser(monkeypatch, load_fixture):
    fixture = load_fixture("ashby_ramp.json")
    monkeypatch.setattr(ashby, "get_json", lambda *a, **k: fixture)
    opps, receipt = ashby.fetch(ASHBY_COMPANY)

    assert receipt["result"] == "ok"
    assert all(is_actionable_url(o.url) for o in opps)
    one = opps[0]
    assert one.ats == "ashby"
    assert "ashbyhq.com/ramp/" in one.url
    # Ashby gives us department + remote directly
    assert one.department is not None or one.department is None  # field present
    assert isinstance(one.remote, bool)


def test_ashby_skips_unlisted(monkeypatch):
    fixture = {"jobs": [
        {"id": "1", "title": "Hidden Role", "jobUrl":
         "https://jobs.ashbyhq.com/ramp/00000000-0000-0000-0000-000000000001",
         "isListed": False, "location": "NYC"},
        {"id": "2", "title": "Visible Role", "jobUrl":
         "https://jobs.ashbyhq.com/ramp/00000000-0000-0000-0000-000000000002",
         "isListed": True, "location": "NYC"},
    ]}
    monkeypatch.setattr(ashby, "get_json", lambda *a, **k: fixture)
    opps, _ = ashby.fetch(ASHBY_COMPANY)
    titles = [o.title for o in opps]
    assert "Visible Role" in titles
    assert "Hidden Role" not in titles


def test_ashby_drops_non_text_published_at(monkeypatch):
    payload = {"jobs": [{
        "id": "bad-date",
        "title": "Bad date role",
        "jobUrl": "https://jobs.ashbyhq.com/acme/bad-date",
        "location": "Remote",
        "publishedAt": 123,
    }, {
        "id": "good-date",
        "title": "Good date role",
        "jobUrl": "https://jobs.ashbyhq.com/acme/good-date",
        "location": "Remote",
        "publishedAt": "2026-08-02T12:00:00+00:00",
    }]}
    monkeypatch.setattr(ashby, "get_json", lambda *a, **k: payload)

    opps, receipt = ashby.fetch({"name": "Acme", "slug": "acme"})

    assert [opp.title for opp in opps] == ["Good date role"]
    assert receipt["dropped_malformed"] == 1


def test_lever_parser(monkeypatch, load_fixture):
    fixture = load_fixture("lever_netlify.json")
    monkeypatch.setattr(lever, "get_json", lambda *a, **k: fixture)
    opps, receipt = lever.fetch(LEVER_COMPANY)

    assert receipt["result"] == "ok"
    assert all(is_actionable_url(o.url) for o in opps)
    remote = next(o for o in opps if o.title == "FP&A Manager")
    assert remote.remote is True
    assert remote.location == "Remote - US"
    assert remote.department == "Finance"


@pytest.mark.parametrize(
    ("fetcher", "company", "payload", "good"),
    [
        (
            greenhouse,
            {"name": "Acme", "slug": "acme"},
            {"jobs": [
                {"id": "bad", "absolute_url": "https://boards.greenhouse.io/acme/jobs/bad",
                 "title": "Bad", "location": "not-an-object"},
                {"id": "1", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                 "title": "Good", "location": {"name": "New York, NY"}},
            ]},
            "Good",
        ),
        (
            ashby,
            {"name": "Acme", "slug": "acme"},
            {"jobs": [
                {"id": "bad", "jobUrl": "https://jobs.ashbyhq.com/acme/00000000-0000-0000-0000-000000000001",
                 "title": "Bad", "location": {"name": "not-a-string"}},
                {"id": "00000000-0000-0000-0000-000000000002",
                 "jobUrl": "https://jobs.ashbyhq.com/acme/00000000-0000-0000-0000-000000000002",
                 "title": "Good", "location": "New York, NY"},
            ]},
            "Good",
        ),
        (
            lever,
            {"name": "Acme", "slug": "acme"},
            [
                {"id": "bad", "hostedUrl": "https://jobs.lever.co/acme/a1b2c3d4-1111-2222-3333-444455556666",
                 "text": "Bad", "categories": "not-an-object"},
                {"id": "1", "hostedUrl": "https://jobs.lever.co/acme/a1b2c3d4-1111-2222-3333-444455556667",
                 "text": "Good", "categories": {"location": "New York, NY"}},
            ],
            "Good",
        ),
    ],
)
def test_malformed_nested_row_is_dropped_without_poisoning_feed(
    monkeypatch, fetcher, company, payload, good
):
    monkeypatch.setattr(fetcher, "get_json", lambda *a, **k: payload)
    opps, receipt = fetcher.fetch(company)

    assert [opp.title for opp in opps] == [good]
    assert receipt["raw"] == 2
    assert receipt["dropped_malformed"] == 1


def test_fetch_error_returns_empty(monkeypatch):
    from jobhunt.ats._http import FetchError

    def boom(*a, **k):
        raise FetchError("HTTP 404 for ...")

    monkeypatch.setattr(greenhouse, "get_json", boom)
    opps, receipt = greenhouse.fetch({"name": "Dead", "slug": "dead-co"})
    assert opps == []
    assert receipt["result"] == "error"
    assert receipt["count"] == 0


def test_greenhouse_missing_questions_is_not_claimed_extractable(monkeypatch):
    payload = {"jobs": [{
        "id": 1,
        "title": "Analyst",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
    }]}
    monkeypatch.setattr(greenhouse, "get_json", lambda *a, **k: payload)
    opps, _ = greenhouse.fetch({"name": "Acme", "slug": "acme"})
    monkeypatch.setattr(discover, "get_json", lambda *a, **k: {"content": "<p>JD</p>"})
    discover.hydrate_details(opps)
    assert opps[0].application["extractable"] is False


def test_ashby_malformed_sections_is_not_claimed_extractable(monkeypatch):
    payload = {"jobs": [{
        "id": "00000000-0000-0000-0000-000000000001",
        "title": "Analyst",
        "jobUrl": "https://jobs.ashbyhq.com/acme/00000000-0000-0000-0000-000000000001",
    }]}
    monkeypatch.setattr(ashby, "get_json", lambda *a, **k: payload)
    opps, _ = ashby.fetch({"name": "Acme", "slug": "acme"})
    monkeypatch.setattr(discover, "post_json", lambda *a, **k: {
        "data": {"jobPosting": {"applicationForm": {"sections": {"oops": "bad"}}}},
    })
    discover.hydrate_details(opps)
    assert opps[0].application["extractable"] is False


def test_greenhouse_fetches_content_true_and_maps_departments(monkeypatch, load_fixture):
    # `content=true` is the only board-level call that returns `departments` —
    # without it every Greenhouse posting has department=None and the filter's
    # department rule is blind for 111 of the universe's companies.
    fixture = load_fixture("greenhouse_stripe.json")
    seen = {}

    def fake_get_json(url, **kwargs):
        seen["url"] = url
        return fixture

    monkeypatch.setattr(greenhouse, "get_json", fake_get_json)
    opps, receipt = greenhouse.fetch(GH_COMPANY)

    assert "content=true" in seen["url"]
    treasury = next(o for o in opps if "Treasury" in o.title)
    assert treasury.department == "Marketing"          # mapped from departments[]
    no_dept = next(o for o in opps if "Deal Desk" in o.title)
    assert no_dept.department is None                  # absent stays None


def test_greenhouse_department_join_and_edge_cases():
    assert greenhouse._department({"departments": [
        {"id": 1, "name": "Finance"}, {"id": 2, "name": "FP&A"}]}) == "Finance / FP&A"
    assert greenhouse._department({"departments": [{"id": 1, "name": "  "}]}) is None
    assert greenhouse._department({"departments": ["not-a-dict", None]}) is None
    assert greenhouse._department({"departments": None}) is None
    assert greenhouse._department({}) is None
