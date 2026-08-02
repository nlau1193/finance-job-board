"""Enrichment tests: fit, comp parse, warm-path, momentum, freshness."""

from datetime import datetime, timezone

from jobhunt import enrich
from jobhunt.model import Opportunity


FIT = {
    "skills": ["fp&a", "financial planning", "arr", "strategic finance", "excel"],
    "too_junior": ["analyst i", "analyst 1", "junior", "intern"],
    "too_senior": ["vp", "vice president", "director", "head of", "chief"],
    "gatekeepers": ["cpa", "series 7", "big 4 audit"],
}


def test_empty_fit_preferences_do_not_invent_a_label():
    assert enrich.fit_assessment(opp("Product Designer"), {}) == {}


def opp(title, desc="", company="Acme", comp=None, posted=None, ats="greenhouse"):
    return Opportunity(
        id="x", company=company, title=title, location="New York, NY",
        url="https://x/jobs/1", ats=ats, company_slug="acme", job_id="1",
        description_html=desc, compensation=comp, posted_at=posted,
    )


# --- fit word-boundary (the bug the rendered board caught) -----------------

def test_analyst_two_is_not_junior():
    a = enrich.fit_assessment(opp("Strategic Finance Analyst II", "strategic finance fp&a arr"), FIT)
    assert a["bucket"] in ("APPLY", "STRETCH")
    assert "below your level" not in a["red_flags"]


def test_analyst_one_is_junior_skip():
    a = enrich.fit_assessment(opp("Financial Analyst I"), FIT)
    assert a["bucket"] == "SKIP"


def test_director_is_stretch():
    a = enrich.fit_assessment(opp("Director, Strategic Finance", "strategic finance fp&a"), FIT)
    assert a["bucket"] == "STRETCH"


def test_skill_word_boundary_no_false_pill():
    # 'arr' must not match inside 'narrow'/'warranty'
    a = enrich.fit_assessment(opp("Finance Manager", "narrow warranty arrangements"), FIT)
    assert "arr" not in a["why"]


def test_tenure_red_flag():
    a = enrich.fit_assessment(opp("Finance Manager", "requires 12+ years of experience fp&a"), FIT)
    assert any("12+ years" in f for f in a["red_flags"])


# --- comp parse ------------------------------------------------------------

def test_comp_parsed_from_jd_body():
    o = opp("Finance Manager", "The salary range is $120,000 - $160,000 USD per year.")
    assert enrich.parse_comp(o) == "$120K–$160K"


def test_comp_k_format():
    o = opp("Finance Manager", "Comp: $130K – $175K plus equity")
    assert enrich.parse_comp(o) == "$130K–$175K"


def test_comp_passthrough_when_already_set():
    o = opp("Finance Manager", "no numbers here", comp="$150K–$180K")
    assert enrich.parse_comp(o) == "$150K–$180K"


def test_comp_none_when_absent():
    assert enrich.parse_comp(opp("Finance Manager", "competitive compensation")) is None


# --- warm path -------------------------------------------------------------

def test_warm_path_company_join_and_role_overlap_first(tmp_path):
    csv = tmp_path / "connections.csv"
    csv.write_text(
        "First Name,Last Name,Company,Position,Connected On\n"
        "Mike,Eng,Ramp Inc.,Software Engineer,01 Jan 2024\n"
        "Sara,Fin,Ramp,Senior FP&A Manager,01 Jan 2024\n",
        encoding="utf-8",
    )
    conns = enrich.load_connections(csv)
    w = enrich.warm_path(opp("Software Engineer", company="Ramp"), conns)
    assert w["count"] == 2
    assert w["people"][0]["name"] == "Mike Eng"  # role overlap first


def test_warm_path_empty_without_match(tmp_path):
    csv = tmp_path / "connections.csv"
    csv.write_text("First Name,Last Name,Company,Position,Connected On\nA,B,Other Co,X,01 Jan 2024\n", encoding="utf-8")
    conns = enrich.load_connections(csv)
    assert enrich.warm_path(opp("FM", company="Ramp"), conns) == {}


def test_no_csv_is_graceful(tmp_path):
    assert enrich.load_connections(tmp_path / "missing.csv") == {}


# --- momentum + freshness --------------------------------------------------

def test_momentum_counts():
    raw = [opp("Eng", company="Acme"), opp("FM", company="Acme"), opp("Sales", company="Acme")]
    filt = [opp("FM", company="Acme")]
    m = enrich.company_momentum(filt, raw, prev={})
    assert m["Acme"]["total_roles"] == 3
    assert m["Acme"]["matching_roles"] == 1


def test_momentum_delta():
    raw = [opp("FM", company="Acme"), opp("FM2", company="Acme")]
    filt = raw
    m = enrich.company_momentum(filt, raw, prev={"Acme": {"finance": 1}})
    assert m["Acme"]["matching_delta"] == 1


def test_freshness_badges():
    now = datetime(2026, 6, 30, tzinfo=timezone.utc)
    assert enrich.freshness(opp("x", posted="2026-06-30"), now=now)["badge"] == "apply today"
    assert enrich.freshness(opp("x", posted="2026-06-26"), now=now)["badge"] == "this week"
    assert enrich.freshness(opp("x", posted="2026-06-01"), now=now)["hot"] is False
