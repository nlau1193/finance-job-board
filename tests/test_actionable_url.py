"""Regression suite for is_actionable_url — the root-cause fix.

The recurring "Stripe screenshot" bug was a search/listing URL being shown as if
it were a posting. The fix: a job-id deep-link parameter (gh_jid) makes a URL
actionable EVEN WHEN its path contains the word "search".
"""

from jobhunt.model import is_actionable_url


# --- the exact bug cases captured live this session -----------------------

def test_stripe_search_gh_jid_is_actionable():
    # Stripe's real posting URLs route through a /jobs/search path but deep-link.
    assert is_actionable_url("https://stripe.com/jobs/search?gh_jid=7978019")


def test_datadog_detail_gh_jid_is_actionable():
    assert is_actionable_url("https://careers.datadoghq.com/detail/7993638/?gh_jid=7993638")


def test_bare_search_page_is_not_actionable():
    # No gh_jid, no posting id — a real search/listing page must be rejected.
    assert not is_actionable_url("https://stripe.com/jobs/search?query=finance")
    assert not is_actionable_url("https://careers.snowflake.com/us/en/search-results?keywords=finance")
    assert not is_actionable_url("https://careers.datadoghq.com/all-jobs?query=fp%26a")


def test_board_root_is_not_actionable():
    assert not is_actionable_url("https://boards.greenhouse.io/mongodb")
    assert not is_actionable_url("https://boards.greenhouse.io/mongodb?search=financial%20analyst")


# --- canonical posting shapes per ATS -------------------------------------

def test_greenhouse_canonical_posting():
    assert is_actionable_url("https://boards.greenhouse.io/databricks/jobs/8030859")
    assert is_actionable_url("https://job-boards.greenhouse.io/figma/jobs/4561234")


def test_ashby_uuid_posting():
    assert is_actionable_url(
        "https://jobs.ashbyhq.com/ramp/0907ae2a-5334-4d64-9a76-cc9428224546")
    # apply variant (…/application) is still a deep-link
    assert is_actionable_url(
        "https://jobs.ashbyhq.com/ramp/0907ae2a-5334-4d64-9a76-cc9428224546/application")


def test_lever_uuid_posting():
    assert is_actionable_url(
        "https://jobs.lever.co/netlify/a1b2c3d4-1111-2222-3333-444455556666")


# --- defensive edges -------------------------------------------------------

def test_rejects_garbage():
    for bad in ("", "   ", None, 123, "not a url", "ftp://x.com/jobs/1",
                "javascript:void(0)", "/jobs/123", "mailto:a@b.com"):
        assert not is_actionable_url(bad)


def test_ashby_board_root_not_actionable():
    assert not is_actionable_url("https://jobs.ashbyhq.com/ramp")


def test_lookalike_ats_hosts_and_generic_numeric_urls_are_not_actionable():
    assert not is_actionable_url(
        "https://eviljobs.ashbyhq.com/ramp/0907ae2a-5334-4d64-9a76-cc9428224546")
    assert not is_actionable_url(
        "https://notjobs.lever.co/netlify/a1b2c3d4-1111-2222-3333-444455556666")
    assert not is_actionable_url(
        "https://evilmyworkdayjobs.com/foo/job/x")
    assert not is_actionable_url("https://evil.example/jobs/12345")


def test_query_id_must_be_numeric_and_workday_host_boundary_is_strict():
    assert not is_actionable_url("https://evil.example/?gh_jid=javascript:alert(1)")
    assert not is_actionable_url("https://evil.example/?gh_jid=not-a-number")
    assert not is_actionable_url("https://evil-myworkdayjobs.com/foo/job/x")


def test_numeric_gh_jid_custom_domain_remains_legacy_compatible():
    # Some official employer feeds (for example Stripe-style careers pages)
    # carry the posting id in a numeric gh_jid query on a non-ATS hostname.
    # This compatibility branch is intentionally broader because this helper
    # has no company catalog context; the feed adapter is the trust boundary.
    assert is_actionable_url("https://careers.example.com/jobs/search?gh_jid=123")


def test_dotted_lookalike_ats_hosts_are_not_actionable():
    # A hostname containing an official ATS name as a subdomain is still an
    # unrelated site.  Apply links must not pass the gate on string-search
    # coincidence alone.
    assert not is_actionable_url(
        "https://evil.jobs.ashbyhq.com/ramp/0907ae2a-5334-4d64-9a76-cc9428224546")
    assert not is_actionable_url(
        "https://evil.jobs.lever.co/netlify/a1b2c3d4-1111-2222-3333-444455556666")
    assert not is_actionable_url(
        "https://evil.boards.greenhouse.io/acme/jobs/123")
    # Workday legitimately uses tenant subdomains; a suffix lookalike without
    # the dot boundary remains rejected.
    assert is_actionable_url(
        "https://acme.wd1.myworkdayjobs.com/en-US/External/job/US-NY/Role_R1")
