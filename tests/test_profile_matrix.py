"""Category-neutral search contracts for ten different technology profiles.

These are deliberately synthetic and contain no resume, account, or private
connection data. They prove that a fresh install can be configured for very
different kinds of work without a hidden category filter.
"""

import pytest

from jobhunt.filter import Profile, matches
from jobhunt.model import Opportunity


def _opportunity(title: str, *, location: str = "Remote - US", department: str = "") -> Opportunity:
    return Opportunity(
        id=f"test:{title.lower().replace(' ', '-')}",
        company="Example Co",
        title=title,
        location=location,
        url="https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789012",
        ats="ashby",
        company_slug="example",
        job_id="12345678-1234-1234-1234-123456789012",
        department=department,
    )


CASES = [
    ("ios_mobile", ["ios", "swift", "swiftui", "mobile engineer", "mobile developer"],
     "Senior iOS Engineer", "Frontend Engineer", "Mobile Engineering"),
    ("backend", ["backend", "back-end", "server-side", "api engineer", "platform engineer"],
     "Senior Backend Engineer", "Frontend Engineer", "Backend Engineering"),
    ("product_management", ["product manager", "product management", "product owner"],
     "Senior Product Manager", "Project Manager", "Product Management"),
    ("data", ["data scientist", "data analyst", "analytics engineer", "business intelligence", "data engineer"],
     "Senior Data Analyst", "Product Marketing Manager", "Data & Analytics"),
    ("security", ["security engineer", "application security", "appsec", "cybersecurity", "security analyst"],
     "Application Security Engineer", "Account Executive", "Application Security"),
    ("devops_sre", ["devops", "site reliability", "sre", "platform engineer", "cloud engineer", "infrastructure engineer"],
     "Site Reliability Engineer", "Product Designer", "SRE"),
    ("quality", ["qa", "quality assurance", "quality engineer", "test engineer", "software test", "automation test"],
     "QA Automation Engineer", "Product Manager", "Quality Assurance"),
    ("product_design", ["product designer", "ux designer", "ui designer", "user experience", "interaction designer", "design systems"],
     "Senior Product Designer", "Backend Engineer", "Product Design"),
    ("applied_ai", ["machine learning", "ml engineer", "applied scientist", "applied ai", "ai engineer", "research engineer"],
     "Applied AI Engineer", "Financial Analyst", "Applied AI"),
    ("solutions_architecture", ["solutions architect", "technical architect", "cloud architect", "customer architect", "solutions engineering"],
     "Solutions Architect", "Recruiter", "Solutions Architecture"),
]


@pytest.mark.parametrize("name,keywords,positive,negative,department", CASES, ids=[case[0] for case in CASES])
def test_ten_technology_profiles_are_independent(
    name, keywords, positive, negative, department
):
    profile = Profile(
        title_keywords=keywords,
        locations=["new york", "remote"],
        remote_ok=True,
        max_age_days=0,
    )

    assert matches(_opportunity(positive, department=department), profile), name
    # Keep the unrelated role in a neutral department so this assertion tests
    # title isolation; department-only matching is covered separately below.
    assert not matches(_opportunity(negative, department="Other"), profile), name
    assert not matches(_opportunity(positive, location="London, UK", department=department), profile), name


def test_all_roles_profile_keeps_every_category_when_location_is_unrestricted():
    profile = Profile(title_keywords=[], locations=["all"], remote_ok=False, max_age_days=0)
    for title in ("iOS Engineer", "Product Manager", "Data Scientist", "Solutions Architect"):
        assert matches(_opportunity(title, location="Tokyo, Japan"), profile)


def test_department_fallback_supports_ats_roles_without_a_specific_title():
    profile = Profile(
        title_keywords=["applied ai"],
        locations=["remote"],
        remote_ok=True,
        max_age_days=0,
    )
    assert matches(_opportunity("Researcher", department="Applied AI"), profile)
    assert not matches(_opportunity("Researcher", department="Finance"), profile)
