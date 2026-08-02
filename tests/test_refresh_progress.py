"""CLI refresh heartbeat stays visible during a large network run."""

import types

import jobs


def test_refresh_prints_heartbeat_without_verbose_flag(monkeypatch, capsys):
    companies = [{"name": f"Company {i}"} for i in range(20)]
    monkeypatch.setattr(jobs, "_need_requests", lambda: True)
    monkeypatch.setattr(jobs, "_load_companies", lambda: companies)

    def fake_refresh_board(*, no_cache=False, no_forms=False, progress=None):
        assert progress is not None
        progress("discovering", 0, 20, "Resolving companies…")
        progress("discovering", 10, 20, "Company 10…")
        progress("discovering", 20, 20, "Company 20…")
        progress("filtering", 0, 0, "Matching your search…")
        progress("done", 1, 1, "1 posting from 1 company")
        return {
            "opportunities": [{"id": "one"}],
            "meta": {
                "companies_resolved": 20,
                "raw_matches": 1,
                "companies_with_postings": 1,
                "dropped_non_actionable": 0,
                "errors": [],
                "warnings": [],
            },
        }

    monkeypatch.setattr(jobs, "refresh_board", fake_refresh_board)

    assert jobs.cmd_refresh(types.SimpleNamespace(no_cache=False, no_forms=True, verbose=False)) == 0
    output = capsys.readouterr().out
    assert "Checking official company job feeds… 0/20" in output
    assert "Checking official company job feeds… 10/20" in output
    assert "Checking official company job feeds… 20/20" in output
    assert "Refresh complete" in output
