"""Offline integration coverage for discover -> filter -> enrich -> store."""

from pathlib import Path

import jobs
from jobhunt import discover, enrich, store
from jobhunt.model import Opportunity


def _posting(job_id="1", title="Product Designer"):
    return Opportunity(
        id=Opportunity.make_id("greenhouse", "acme", job_id),
        company="Acme",
        title=title,
        location="New York, NY",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        ats="greenhouse",
        company_slug="acme",
        job_id=job_id,
        posted_at="2026-07-30T00:00:00Z",
    )


def _wire_pipeline(monkeypatch, tmp_path: Path, *, receipts, raw):
    board_path = tmp_path / "jobs.local.json"
    dismissed_path = tmp_path / "dismissed.json"
    profile_path = tmp_path / "search.json"
    profile_path.write_text(
        '{"title_keywords": [], "title_exclude": [], "locations": ["all"], '
        '"remote_ok": true, "max_age_days": 0, "fit": {}}',
        encoding="utf-8",
    )
    companies = [{"name": "Acme", "slug": "acme", "ats": "greenhouse"}]
    monkeypatch.setattr(jobs, "_load_companies", lambda: companies)
    monkeypatch.setattr(jobs, "_profile_path", lambda: profile_path)
    monkeypatch.setattr(jobs, "_load_profile_raw", lambda: {"fit": {}})
    monkeypatch.setattr(jobs, "_build_board", lambda payload=None: None)
    monkeypatch.setattr(discover, "discover_all", lambda *args, **kwargs: (raw, receipts))
    monkeypatch.setattr(discover, "hydrate_details", lambda candidates, **kwargs: candidates)
    monkeypatch.setattr(enrich, "enrich_all", lambda filtered, raw, fit: {"connections_loaded": False})

    real_save = store.save
    real_merge = store.merge_read_state
    real_load_dismissed = store.load_dismissed
    monkeypatch.setattr(
        store,
        "save",
        lambda opportunities, **kwargs: real_save(opportunities, path=board_path, **kwargs),
    )
    monkeypatch.setattr(
        store,
        "merge_read_state",
        lambda opportunities, **kwargs: real_merge(opportunities, path=board_path, **kwargs),
    )
    monkeypatch.setattr(store, "load_dismissed", lambda **kwargs: real_load_dismissed(dismissed_path))
    return board_path


def test_refresh_pipeline_publishes_filtered_enriched_board(monkeypatch, tmp_path):
    posting = _posting()
    board_path = _wire_pipeline(
        monkeypatch,
        tmp_path,
        raw=[posting],
        receipts=[{"company": "Acme", "ats": "greenhouse", "result": "ok", "count": 1}],
    )
    phases = []

    payload = jobs.refresh_board(progress=lambda phase, done, total, message: phases.append(phase))

    assert [item["id"] for item in payload["opportunities"]] == [posting.id]
    assert payload["meta"]["companies_resolved"] == 1
    assert payload["meta"]["companies_with_postings"] == 1
    assert phases[0] == "discovering"
    assert "saving" in phases and phases[-1] == "done"
    assert store.load_opportunities(board_path)[0].id == posting.id


def test_refresh_pipeline_keeps_last_good_board_on_feed_outage(monkeypatch, tmp_path):
    prior = _posting("prior")
    board_path = _wire_pipeline(
        monkeypatch,
        tmp_path,
        raw=[],
        receipts=[{"company": "Acme", "ats": "greenhouse", "result": "error", "count": 0}],
    )
    store.save([prior], now="2026-07-30T00:00:00Z")

    try:
        jobs.refresh_board()
    except RuntimeError as exc:
        assert "existing board was kept unchanged" in str(exc)
    else:
        raise AssertionError("an all-feed outage must fail closed")

    assert store.load_opportunities(board_path)[0].id == prior.id
