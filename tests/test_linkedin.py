"""The LinkedIn boundary is links only: no automation or credentials."""

import jobs

from jobhunt.linkedin import linkedin_links


def test_linkedin_links_are_read_only_searches():
    links = linkedin_links("Ramp & Co")
    assert set(links) == {"connections", "recruiters", "company_people"}
    assert all(url.startswith("https://www.linkedin.com/search/") for url in links.values())
    assert "Ramp%20%26%20Co" in links["connections"]
    assert "network=%5B%22F%22%5D" in links["connections"]


def test_cli_rejects_stale_setup_or_login_tokens(capsys):
    for token in ("setup", "login"):
        assert jobs.main(["linkedin", token]) == 2
        assert "no LinkedIn setup or login" in capsys.readouterr().out
