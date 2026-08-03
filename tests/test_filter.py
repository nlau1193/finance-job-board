"""Filter tests: configurable title keywords + NYC/remote location."""

from jobhunt.filter import (
    Profile, matches, title_matches, location_matches, location_verdict,
    jd_allows_remote_or_ny, remote_first_slugs,
)
from jobhunt.model import Opportunity


PROFILE = Profile(
    title_keywords=["fp&a", "financial analyst", "finance manager", "strategic finance",
                    "controller", "revenue operations"],
    title_exclude=["intern", "internship"],
    locations=["new york", "ny", "nyc", "remote"],
    remote_ok=True,
)


def opp(title, location="New York, NY", department=None, remote=False):
    return Opportunity(
        id="x", company="Acme", title=title, location=location, url="https://x/jobs/1",
        ats="greenhouse", company_slug="acme", job_id="1",
        department=department, remote=remote,
    )


def test_finance_titles_match():
    assert title_matches(opp("Senior Financial Analyst"), PROFILE)
    assert title_matches(opp("FP&A Manager"), PROFILE)
    assert title_matches(opp("Strategic Finance Lead"), PROFILE)
    assert title_matches(opp("Manager, Revenue Operations"), PROFILE)


def test_non_finance_titles_rejected():
    assert not title_matches(opp("Senior Software Engineer"), PROFILE)
    assert not title_matches(opp("Product Designer"), PROFILE)
    # 'Financial Crimes' must NOT match (precise multi-word keywords)
    assert not title_matches(opp("Operations Associate, Financial Crimes"), PROFILE)


def test_intern_excluded():
    assert not title_matches(opp("FP&A Intern"), PROFILE)
    assert not title_matches(opp("Finance Manager Internship"), PROFILE)


def test_department_can_satisfy_title():
    # Department text is part of the title haystack.
    assert title_matches(opp("Analyst", department="FP&A"), PROFILE)


def test_location_nyc_and_remote():
    assert location_matches(opp("x", location="New York, NY"), PROFILE)
    assert location_matches(opp("x", location="Remote - US"), PROFILE)
    assert location_matches(opp("x", location="", remote=True), PROFILE)


def test_location_other_city_rejected():
    assert not location_matches(opp("x", location="London, UK"), PROFILE)
    assert not location_matches(opp("x", location="Bengaluru"), PROFILE)


def test_foreign_remote_rejected_even_when_remote_flag_set():
    # The bug the rendered board caught: a Canada/EMEA remote role must NOT show
    # on a NYC + remote-US board, even if the ATS tagged it remote.
    assert not location_matches(
        opp("x", location="Canada - Remote (ON, AB, BC, or NS Only)", remote=True), PROFILE)
    assert not location_matches(
        opp("x", location="London, United Kingdom", remote=True), PROFILE)
    assert not location_matches(
        opp("x", location="Remote - EMEA", remote=True), PROFILE)


def test_remote_us_and_us_plus_foreign_kept():
    assert location_matches(opp("x", location="Remote - US", remote=True), PROFILE)
    assert location_matches(opp("x", location="United States - Remote", remote=True), PROFILE)
    # A multi-region role that includes the US is still relevant.
    assert location_matches(opp("x", location="Remote (US & Canada)", remote=True), PROFILE)
    # NYC always wins, even alongside foreign offices.
    assert location_matches(opp("x", location="New York; London; Remote"), PROFILE)


def test_us_national_kept_without_literal_remote():
    # 2026-07-07 gap: remote-first employers (Airbnb) tag genuinely US-wide roles
    # by country/state, not "Remote - US". "United States" with no literal "remote"
    # was being dropped — these distributed roles must be kept.
    assert location_matches(opp("x", location="United States"), PROFILE)
    assert location_matches(opp("x", location="United States "), PROFILE)
    assert location_matches(opp("x", location="California, United States"), PROFILE)
    assert location_matches(opp("x", location="Nationwide"), PROFILE)
    assert location_matches(opp("x", location="US-based"), PROFILE)
    # Foreign country named the same way is still dropped.
    assert not location_matches(opp("x", location="United Kingdom"), PROFILE)


def test_onsite_us_city_dropped_even_when_country_named():
    # Board is NYC + remote/distributed only. A specifically-named non-NYC US
    # city is an onsite role and drops — even when the string also names the
    # country ("San Francisco, CA, USA"). Only a distributed country/state tag
    # (no city) or an actual "remote" signal keeps a non-NYC role.
    assert not location_matches(opp("x", location="San Francisco, CA, USA"), PROFILE)
    assert not location_matches(opp("x", location="Boston, Massachusetts, USA"), PROFILE)
    assert not location_matches(opp("x", location="Denver, Colorado, USA"), PROFILE)
    # …but the same city with a remote signal is still kept.
    assert location_matches(opp("x", location="San Francisco, CA / Remote", remote=True), PROFILE)


def test_onsite_city_state_country_is_not_distributed():
    # 2026-07-08: onsite roles tagged "City, State, Country" were leaking as
    # "US-national/distributed" because they contain "USA"/"United States" and the
    # city wasn't in the hardcoded list. They must resolve to onsite (maybe/drop),
    # while a state/country-only tag stays distributed (keep).
    onsite = ["New Brunswick, New Jersey, United States of America", "USA, TX, Irving",
              "Marietta, Ohio, USA", "Wilmington NC USA", "Greenwich, Connecticut, United States",
              "Fridley, Minnesota, United States of America", "Waltham, Massachusetts, USA"]
    for loc in onsite:
        assert location_verdict(opp("x", location=loc), PROFILE) == "maybe", loc
    distributed = ["United States", "California, United States", "Nationwide",
                   "US-based", "United States Work at Home"]
    for loc in distributed:
        assert location_verdict(opp("x", location=loc), PROFILE) == "keep", loc
    # NYC in a 3-part string still wins.
    assert location_verdict(opp("x", location="New York, New York, United States"), PROFILE) == "keep"


def test_location_verdict_three_way():
    # keep: NYC / remote / distributed
    assert location_verdict(opp("x", location="New York, NY"), PROFILE) == "keep"
    assert location_verdict(opp("x", location="Remote - US"), PROFILE) == "keep"
    assert location_verdict(opp("x", location="United States"), PROFILE) == "keep"
    # maybe: onsite non-NYC US city (rescuable by JD / remote-first)
    assert location_verdict(opp("x", location="San Francisco, CA, USA"), PROFILE) == "maybe"
    assert location_verdict(opp("x", location="Boston, Massachusetts, USA"), PROFILE) == "maybe"
    # drop: foreign
    assert location_verdict(opp("x", location="London, UK"), PROFILE) == "drop"
    assert location_verdict(opp("x", location="Bengaluru"), PROFILE) == "drop"


def test_all_locations_keeps_any_geography():
    all_locations = Profile(locations=["all"], remote_ok=False)
    assert location_verdict(opp("x", location="London, UK"), all_locations) == "keep"
    assert location_verdict(opp("x", location="Tokyo, Japan"), all_locations) == "keep"


def test_custom_location_preferences_are_honored():
    chicago = Profile(locations=["chicago"], remote_ok=False)
    assert location_verdict(
        opp("x", location="Chicago, Illinois, United States"), chicago
    ) == "keep"
    assert location_verdict(opp("x", location="New York, NY"), chicago) == "maybe"
    assert location_verdict(opp("x", location="Remote - US"), chicago) == "drop"
    assert not jd_allows_remote_or_ny(
        "<p>This role is fully remote in the US.</p>", chicago
    )
    assert not jd_allows_remote_or_ny(
        "<p>Candidates in NYC will also be considered.</p>", chicago
    )


def test_is_fresh_30_day_cutoff():
    from datetime import datetime, timezone
    from jobhunt.filter import is_fresh
    now = datetime(2026, 7, 7, tzinfo=timezone.utc)

    def dated(posted, first_seen=""):
        o = opp("Financial Analyst")
        o.posted_at, o.first_seen_at = posted, first_seen
        return o

    assert is_fresh(dated("2026-07-01T10:00:00+00:00"), 30, now)          # 6 days old
    assert is_fresh(dated("2026-06-10"), 30, now)                          # ~27 days, date-only
    assert not is_fresh(dated("2026-05-01T00:00:00Z"), 30, now)            # ~67 days old
    # No posted_at → fall back to first_seen; still hidden if that's old too.
    assert not is_fresh(dated("", "2026-01-01T00:00:00Z"), 30, now)
    # Undated posting is kept (don't silently hide), and max_age<=0 disables cutoff.
    assert is_fresh(dated("", ""), 30, now)
    assert is_fresh(dated("2000-01-01T00:00:00Z"), 0, now)


def test_posted_floor_is_beyond_the_cutoff():
    # Workday's "Posted 30+ Days Ago" is a floor — the role could be months old.
    # It must be treated as beyond max_age, NOT as an undated (kept) posting,
    # even when we first saw it today.
    from datetime import datetime, timezone
    from jobhunt.filter import is_fresh
    now = datetime(2026, 7, 7, tzinfo=timezone.utc)
    o = opp("Financial Analyst")
    o.posted_at, o.posted_is_floor = None, True
    o.first_seen_at = "2026-07-07T00:00:00Z"
    assert not is_fresh(o, 45, now)
    assert is_fresh(o, 0, now)  # cutoff disabled → still surfaced


def test_hybrid_non_nyc_hub_is_hard_dropped():
    # A hybrid role tied to a non-NYC office can't be done from NYC, so a remote
    # JD signal must NOT rescue it — it's a hard drop, not a "maybe".
    assert location_verdict(opp("x", location="Hybrid - San Francisco, California"), PROFILE) == "drop"
    assert location_verdict(opp("x", location="Hybrid - San Diego; Hybrid - San Francisco"), PROFILE) == "drop"
    # …but a NYC hybrid is fine, and a hybrid role that also offers remote stays open.
    assert location_verdict(opp("x", location="Hybrid - New York, NY"), PROFILE) == "keep"
    assert location_verdict(opp("x", location="Hybrid - San Francisco or Remote"), PROFILE) == "keep"


def test_nyc_alias_keeps_hybrid_for_a_custom_nyc_search():
    # A person who types only "NYC" should still see ATS spellings such as
    # "New York, NY" and "New York City".  This is especially important when
    # remote roles are excluded: a New York hybrid role is still a local role.
    nyc_only = Profile(locations=["NYC"], remote_ok=False)
    assert location_verdict(opp("x", location="Hybrid - New York, NY"), nyc_only) == "keep"
    assert location_verdict(opp("x", location="New York City, US (Hybrid)"), nyc_only) == "keep"
    assert location_verdict(opp("x", location="Hybrid - San Francisco, California"), nyc_only) == "drop"
    labeled_nyc = Profile(locations=["New York, NY"], remote_ok=False)
    assert location_verdict(opp("x", location="New York City, US (Hybrid)"), labeled_nyc) == "keep"
    abbreviated_nyc = Profile(locations=["NY"], remote_ok=False)
    assert location_verdict(opp("x", location="New York City, US (Hybrid)"), abbreviated_nyc) == "keep"


def test_remote_excluded_drops_remote_only_new_york_spellings():
    # The remote toggle is an actual preference, not a hint.  A remote-only ATS
    # label must not be rescued merely because it also names New York.
    onsite_or_hybrid = Profile(locations=["new york", "nyc"], remote_ok=False)
    for loc in (
        "Work At Home-New York",
        "Work At Home-Massachusetts; Work At Home-New York",
        "Remote - New York",
    ):
        assert location_verdict(opp("x", location=loc), onsite_or_hybrid) == "drop", loc
    assert location_verdict(opp("x", location="Hybrid - New York, NY"), onsite_or_hybrid) == "keep"
    assert location_verdict(opp("x", location="New York, NY; Remote - US"), onsite_or_hybrid) == "keep"


def test_jd_rescues_onsite_city_when_remote_or_ny():
    # 2026-07-07: remote-first employers post an HQ city in the location field but
    # the JD says the role is remote/NY-eligible. Scan the JD to rescue it.
    assert jd_allows_remote_or_ny("<p>This role is <strong>fully remote</strong> in the US.</p>")
    assert jd_allows_remote_or_ny("<p>We are open to remote candidates.</p>")
    assert jd_allows_remote_or_ny("<div>#LI-Remote</div>")
    assert jd_allows_remote_or_ny("<p>Cohere is remote-friendly.</p>")
    assert jd_allows_remote_or_ny("<p>Remote (US)</p>")
    # Genuine NY *eligibility* — a location choice or explicit consideration.
    assert jd_allows_remote_or_ny(
        "<p>For roles based in San Francisco or New York City, the base range is…</p>")
    assert jd_allows_remote_or_ny(
        "<p>Exceptional candidates in NYC or Washington, DC will also be considered.</p>")
    # Bare NY *mentions* (boilerplate) do NOT rescue — this was the false-positive
    # class (HQ blurb, office list, NYC Local Law 144 notice).
    assert not jd_allows_remote_or_ny(
        "<p>Headquartered in New York with offices in Austin, Chicago, and London.</p>")
    assert not jd_allows_remote_or_ny(
        "<p>We are primarily an in-person company based in San Francisco, with "
        "growing offices in Atlanta, New York, and London.</p>")
    # A strictly onsite JD with no remote/NY language is NOT rescued.
    assert not jd_allows_remote_or_ny(
        "<p>This position is based onsite in our San Francisco headquarters.</p>")
    assert not jd_allows_remote_or_ny("")
    assert not jd_allows_remote_or_ny(None)


def test_remote_first_slugs():
    companies = [
        {"slug": "airbnb", "remote_first": True},
        {"slug": "gitlab", "tags": ["Remote-First", "devtools"]},
        {"slug": "chime", "tags": ["fintech"]},
    ]
    assert remote_first_slugs(companies) == {"airbnb", "gitlab"}


def test_ny_word_boundary_not_substring():
    # 'ny' must not match inside 'Sunnyvale'.
    assert not location_matches(opp("x", location="Sunnyvale, CA"), PROFILE)


def test_foreign_named_us_cities_not_treated_as_foreign():
    # 2026-07-13: substring foreign markers dropped real US locations — Ontario
    # (the California city) hit the Canada marker, Paris TX hit France, London KY
    # hit the UK. A foreign-named token followed by a US state/USA reads as a US
    # city: onsite non-NYC → "maybe" (JD/remote-first rescuable), not "drop".
    assert location_verdict(opp("x", location="Ontario, California, United States"), PROFILE) == "maybe"
    assert location_verdict(opp("x", location="Paris, Texas, USA"), PROFILE) == "maybe"
    assert location_verdict(opp("x", location="London, KY, USA"), PROFILE) == "maybe"
    # The genuinely foreign versions still drop.
    assert location_verdict(opp("x", location="Ontario, Canada"), PROFILE) == "drop"
    assert location_verdict(opp("x", location="Toronto, Ontario, Canada"), PROFILE) == "drop"
    assert location_verdict(opp("x", location="Paris, France"), PROFILE) == "drop"
    assert location_verdict(opp("x", location="London, United Kingdom"), PROFILE) == "drop"


def test_georgia_country_is_not_a_us_anchor():
    # "Georgia" is also a country. Without a country word it must not anchor the
    # US — otherwise "Tbilisi, Georgia" became a "maybe" that a remote-sounding
    # JD could rescue onto the board.
    assert location_verdict(opp("x", location="Tbilisi, Georgia"), PROFILE) == "drop"
    # A real US-Georgia posting still resolves (Atlanta / the country word anchor it).
    assert location_verdict(opp("x", location="Atlanta, Georgia, USA"), PROFILE) == "maybe"
    assert location_verdict(opp("x", location="Georgia, United States"), PROFILE) == "keep"


def test_sf_remote_flag_only_is_dropped():
    # A specifically-named non-NYC city with only the ATS remote flag (no 'remote'
    # in the location text) must NOT show on an NYC board — it reads as that city.
    assert not location_matches(opp("x", location="San Francisco", remote=True), PROFILE)
    assert not location_matches(opp("x", location="San Francisco HQ", remote=True), PROFILE)
    assert not location_matches(opp("x", location="San Francisco, California", remote=True), PROFILE)
    # But when the text actually says remote, it's kept.
    assert location_matches(opp("x", location="San Francisco / Remote", remote=True), PROFILE)


def test_ny_abbreviation_kept():
    # "NY Office" / "..., NY" use the abbreviation and are real NYC roles.
    assert location_matches(opp("x", location="Durham office or NY Office"), PROFILE)
    assert location_matches(opp("x", location="Brooklyn, NY"), PROFILE)


def test_empty_location_is_surfaced():
    # Missing location metadata should not hide a finance match.
    assert location_matches(opp("x", location=""), PROFILE)


def test_matches_combines_both():
    assert matches(opp("FP&A Manager", location="New York, NY"), PROFILE)
    assert not matches(opp("FP&A Manager", location="London, UK"), PROFILE)
    assert not matches(opp("Software Engineer", location="New York, NY"), PROFILE)


def test_profile_loads_from_config():
    from pathlib import Path
    cfg = Path(__file__).resolve().parents[1] / "config" / "search.example.json"
    p = Profile.load(cfg)
    assert p.title_keywords == []  # the public starter includes every kind of role
    assert "new york" in p.locations
    assert p.remote_ok is True
    assert p.max_age_days == 90


def test_empty_title_keywords_include_any_role_and_honor_excludes():
    broad = Profile(title_exclude=["intern"])
    assert title_matches(opp("Senior Software Engineer"), broad)
    assert title_matches(opp("Product Designer"), broad)
    assert not title_matches(opp("Software Engineering Intern"), broad)


def test_profile_terms_strip_whitespace_and_punctuation_excludes_match():
    from jobhunt.filter import Profile

    config = Profile(title_exclude=[" intern ", "C++", "C#", "Node.js", "R&D"])
    assert not title_matches(opp("Software Intern"), config)
    assert not title_matches(opp("Senior C++ Engineer"), config)
    assert not title_matches(opp("Senior C# Engineer"), config)
    assert not title_matches(opp("Node.js Developer"), config)
    assert not title_matches(opp("R&D Manager"), config)


def test_profile_location_terms_strip_whitespace():
    from jobhunt.filter import Profile

    config = Profile(locations=[" New York "])
    assert location_matches(opp("Analyst", location="New York, NY"), config)


def test_keyword_search_uses_boundaries_for_short_words():
    focused = Profile(title_keywords=["art"])
    assert title_matches(opp("Art Director"), focused)
    assert not title_matches(opp("Cartographer"), focused)


def _config_profile():
    # Keep the focused finance regression corpus explicit. The public starter is
    # intentionally broad now, so these tests should not silently inherit it.
    return Profile(
        title_keywords=[
            "fp&a", "fpa", "financial planning", "financial analyst", "finance analyst",
            "senior financial analyst", "finance manager", "strategic finance",
            "finance business partner", "business partner, finance", "financial reporting",
            "corporate finance", "revenue operations", "revenue strategy", "revenue analyst",
            "business operations analyst", "finops", "treasury", "financial modeling",
            "gtm finance", "sales finance", "g&a finance", "gtm analytics", "gtm strategy",
            "gtm operations", "revenue analytics", "sales operations", "business operations",
            "business analytics", "strategy & operations", "strategy and operations",
            "corporate development", "corporate strategy", "business strategy", "growth strategy",
            "finance & strategy", "finance and strategy", "manager, finance", "senior analyst, finance",
            "finance associate", "finance lead", "finance transformation", "rev ops", "revops",
            "bizops", "biz ops", "commercial finance", "finance operations", "monetization", "pricing",
        ],
        title_exclude=[
            "intern", "internship", "co-op", "apprentice", "engineer", "developer", "designer",
            "scientist", "architect", "product manager", "director", "head of", "vp",
            "vice president", "chief", "cfo", "controller", "accounting manager", "senior accounting",
            "technical accounting", "revenue accounting", "product strategy", "product operations",
            "product analytics", "marketing strategy", "product marketing", "program manager",
            "engineering manager", "procurement", "sourcing", "vendor management", "deal desk",
            "trainer", "enablement", "recruiter", "recruiting", "account executive", "sales manager",
            "product sales", "campaign", "integrated campaigns", "sales development", "customer success",
            "people partner", "executive assistant", "administrative assistant",
        ],
        locations=["new york", "ny", "nyc", "remote"],
        remote_ok=True,
        max_age_days=90,
    )


def test_engineer_and_pm_titles_excluded():
    # Engineering/PM roles on finance-adjacent teams are NOT the applicant's roles.
    p = _config_profile()
    assert not title_matches(opp("Staff AI Engineer - Cloud FinOps"), p)
    assert not title_matches(opp("Software Engineer, Treasury"), p)
    assert not title_matches(opp("Product Manager, Treasury for Platforms"), p)


def test_internal_audit_not_killed_by_intern_exclude():
    # 'Internal' must not trip the word-boundary 'intern' exclude.
    p = _config_profile()
    assert title_matches(opp("Internal Audit Lead - Treasury, Finance"), p)


def test_finance_partner_for_engineering_kept():
    # 'Engineering' (the org) must not trip the 'engineer' exclude.
    p = _config_profile()
    assert title_matches(opp("Finance Business Partner, Engineering"), p)


def test_non_title_strings_dont_false_match():
    # Lever bug 2026-07-07: a JD sentence or section heading in the title field
    # must NOT match a finance keyword just because it contains the words.
    sentence = ("As a part-time licensed DBT psychologist, you will be employed by Lyra "
                "Clinical Associates P.C. We manage the business operations so you can focus:")
    assert not title_matches(opp(sentence), PROFILE)          # contains "business operations"
    assert not title_matches(opp("Qualifications:"), PROFILE)
    # …but a legit department still counts even if that role's title is odd.
    assert title_matches(opp("Qualifications:", department="Revenue Operations"), PROFILE)
    # A normal finance title is unaffected.
    assert title_matches(opp("Strategic Finance Manager"), PROFILE)


# --- 2026-07-13 coverage-gap fixes (real missed titles from the audit) -------


def test_finance_and_strategy_family_matches():
    # The single biggest audit gap: the standard tech-FP&A "Finance & Strategy"
    # title family (Stripe/Coinbase/DoorDash) was invisible to the keyword list.
    p = _config_profile()
    assert title_matches(opp("Senior Finance & Strategy Analyst, Consumer"), p)
    assert title_matches(opp("Finance & Strategy Partner, Central Engineering"), p)
    assert title_matches(opp("Finance and Strategy Partner"), p)
    assert title_matches(opp("Senior Associate, Finance & Strategy - New Verticals"), p)


def test_new_finance_keyword_classes_match():
    p = _config_profile()
    # each of these is a real posting the audit found rejected
    assert title_matches(opp("Senior Manager, Finance - Revenue Cycle Program Management"), p)
    assert title_matches(opp("Senior Analyst, Finance"), p)                    # Rent the Runway
    assert title_matches(opp("Finance Associate"), p)                          # Brex
    assert title_matches(opp("Market Finance Lead - Provider Analytics"), p)   # Humana
    assert title_matches(opp("Finance Transformation Senior Manager"), p)      # Instacart
    assert title_matches(opp("Applied AI Lead, Rev Ops"), p)                   # Headway
    assert title_matches(opp("RevOps Analyst, Post-Sales"), p)                 # Checkr
    assert title_matches(opp("BizOps Senior Manager"), p)                      # Brex
    assert title_matches(opp("Commercial Finance Manager"), p)
    assert title_matches(opp("Finance Operations Analyst"), p)


def test_engineering_manager_excluded_despite_manager_finance():
    # "manager, finance" (comma form) is a keyword now — an Engineering Manager
    # on a Finance Engineering team must NOT ride in through it.
    p = _config_profile()
    assert not title_matches(opp("Engineering Manager, Finance Engineering"), p)
    # …while the org-name form stays kept (existing behavior, re-asserted).
    assert title_matches(opp("Finance Business Partner, Engineering"), p)


def test_pricing_and_monetization_strategy_now_kept():
    # the applicant's call (2026-07-13): pricing/monetization strategy roles are IN.
    # The old blanket "pricing" exclude killed both of these real NYC postings.
    p = _config_profile()
    assert title_matches(opp("Product Pricing & Monetization Strategy - Associate"), p)  # MongoDB
    assert title_matches(opp("Pricing, Yield, & Sales Compensation Manager"), p)         # Lyft
    # …but the marketing/TPM shapes that ride the same words stay out.
    assert not title_matches(opp("Product Marketing Manager, Monetization"), p)          # Figma
    assert not title_matches(opp("Staff Technical Program Manager, Monetization Data Science"), p)
    assert not title_matches(opp("Senior Partner Program Manager - Pricing & Deal Strategy"), p)


def test_seniority_and_focus_excludes_still_hold():
    # The keyword additions must not loosen the applicant's hard NOs.
    p = _config_profile()
    assert not title_matches(opp("Director, Finance & Strategy"), p)
    assert not title_matches(opp("VP of Revenue Operations"), p)
    assert not title_matches(opp("Head of Strategic Finance"), p)
    assert not title_matches(opp("Chief of Staff, Revenue"), p)
    assert not title_matches(opp("Corporate Finance, Strategic Finance - Associate / Assistant Vice President"), p)
    assert not title_matches(opp("Revenue Accounting Manager"), p)


def test_department_match_accepts_any_short_job_title():
    # Department text is a useful structured signal for every kind of job. The
    # title-shape guard only removes sentence/heading junk; it must not encode a
    # finance-specific list of occupations.
    p = _config_profile()
    dept = "Strategic Finance"
    assert title_matches(opp("Deal Operations Associate", department=dept), p)
    assert title_matches(opp("Commissions Analyst", department=dept), p)
    # role-shaped word missing → the department alone is not enough
    assert title_matches(opp("Solutions Consultant", department="Business Operations"), p)
    assert title_matches(opp("Economist", department="Strategic Finance"), p)
    # excludes still fire against department-driven matches
    assert not title_matches(opp("Workday Engineer", department="Strategic Finance"), p)
    assert not title_matches(opp("Head of Deal Strategy", department="Strategy & Operations"), p)
    # a garbage (non-title-shaped) title still rides on a legit department —
    # there is no real title to judge (Lever JD-sentence quirk, 2026-07-07)
    assert title_matches(opp("Qualifications:", department="Revenue Operations"), p)


def test_department_match_is_not_limited_to_finance_anchored_departments():
    # A configured keyword may occur in any department. Do not carry forward the
    # old finance/revenue anchor, which silently hid ordinary jobs in other teams.
    p = _config_profile()
    assert title_matches(opp("Associate, Network Contracting",
                             department="Business Operations"), p)
    assert title_matches(opp("Manager II, Machine Learning",
                             department="Monetization"), p)
    assert title_matches(opp("Manager, IT Operations",
                             department="Business Operations"), p)
    assert title_matches(opp("Office Operations Associate - NYC",
                             department="Business Operations"), p)
    assert title_matches(opp("Manager, Sales Strategy & Planning",
                             department="Revenue Operations"), p)           # Airtable
    assert title_matches(opp("Senior Associate, Commercial Operations & Strategy",
                             department="Revenue Operations"), p)           # Zocdoc


def test_department_exclude_cannot_veto_a_clean_finance_title():
    # Regression from mapping Greenhouse departments: excludes fired on the
    # department text and killed exact-target titles. The department a company
    # files a role under must not override what the role IS.
    p = _config_profile()
    assert title_matches(opp("Senior Manager, GTM Strategy and Operations",
                             department="Account Management/Customer Success"), p)  # Navan
    assert title_matches(opp("Senior Analyst, Retail Revenue Operations: GTM Analytics & Operations",
                             department="Sales : Retail Sales Enablement"), p)      # Toast
    assert title_matches(opp("Sr. Sales Strategy & Operations Manager",
                             department="Sales Strategy, Operations, and Enablement"), p)  # Pinterest
    # A title-level exclude still wins, whatever the department says.
    assert not title_matches(opp("FP&A Intern", department="Strategic Finance"), p)
    # And a department-driven match still honors department excludes.
    assert not title_matches(opp("Commissions Analyst",
                                 department="Revenue Operations Internship Program"), p)


def test_us_region_tags_namer_and_distributed_keep():
    # Regression: remote-first employers tag US-wide roles "NAMER" (Zapier) or
    # "US - Distributed" (Menlo Security) — both were dropped as unrecognized.
    for loc in ("NAMER", "US - Distributed", "North America", "Remote - North America"):
        assert location_verdict(opp("x", location=loc), PROFILE) == "keep", loc
    # The bare-"US" token anchors but doesn't launder a named non-NYC city…
    assert location_verdict(opp("x", location="Austin, TX, US"), PROFILE) == "maybe"
    # …and foreign-only regions still drop.
    for loc in ("Remote - EMEA", "Canada - Remote", "Sydney, Australia", "London, UK"):
        assert location_verdict(opp("x", location=loc), PROFILE) == "drop", loc
