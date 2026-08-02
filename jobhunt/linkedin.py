"""Safe LinkedIn links.

This module deliberately contains no login, browser automation, scraping,
agent, API-key, or paid-provider path. It only builds ordinary search URLs that
the user may choose to open in their own browser.
"""

from __future__ import annotations

from urllib.parse import quote

_RECRUITER_TERMS = 'recruiter OR "talent acquisition" OR "people team" OR hiring'


def linkedin_links(company: str) -> dict[str, str]:
    """Return read-only LinkedIn search links for one company."""
    base = "https://www.linkedin.com/search/results/people/?keywords="
    return {
        "connections": (
            base + quote(company)
            + "&network=%5B%22F%22%5D&origin=FACETED_SEARCH"
        ),
        "recruiters": (
            base + quote(f"{company} {_RECRUITER_TERMS}")
            + "&origin=SWITCH_SEARCH_VERTICAL"
        ),
        "company_people": (
            "https://www.linkedin.com/search/results/companies/?keywords="
            + quote(company)
        ),
    }
