"""Offline integration coverage for discover -> filter -> enrich -> store."""

from pathlib import Path

import jobs
from jobhunt import discover, enrich, store
from jobhunt.ats import workday
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


def _wire_pipeline(monkeypatch, tmp_path: Path, *, receipts, raw, mock_enrich=True):
    board_path = tmp_path / "jobs.local.json"
    dismissed_path = tmp_path / "dismissed.json"
    profile_path = tmp_path / "search.json"
    profile_path.write_text(
        '{"title_keywords": [], "title_exclude": [], "locations": ["all"], '
        '"remote_ok": true, "max_age_days": 0, "fit": {}}',
        encoding="utf-8",
    )
    companies = [{"name": "Acme", "slug": "acme", "ats": "greenhouse"}]
    monkeypatch.setattr(jobs, "BOARD_FILE", board_path)
    monkeypatch.setattr(jobs, "_load_companies", lambda: companies)
    monkeypatch.setattr(jobs, "_profile_path", lambda: profile_path)
    monkeypatch.setattr(jobs, "_load_profile_raw", lambda: {"fit": {}})
    monkeypatch.setattr(jobs, "_build_board", lambda payload=None: None)
    monkeypatch.setattr(discover, "discover_all", lambda *args, **kwargs: (raw, receipts))
    monkeypatch.setattr(discover, "hydrate_details", lambda candidates, **kwargs: candidates)
    if mock_enrich:
        monkeypatch.setattr(enrich, "enrich_all", lambda filtered, raw, fit, **kwargs: {"connections_loaded": False})
        monkeypatch.setattr(enrich, "write_momentum_snapshot", lambda snapshot: None)

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


def test_refresh_pipeline_surfaces_malformed_feed_rows_without_dropping_good_rows(monkeypatch, tmp_path):
    posting = _posting("2", "Financial Analyst")
    _wire_pipeline(
        monkeypatch,
        tmp_path,
        raw=[posting],
        receipts=[{
            "company": "Acme", "ats": "greenhouse", "result": "ok", "count": 1,
            "raw": 2, "dropped_malformed": 1,
        }],
    )

    payload = jobs.refresh_board()

    assert [item["id"] for item in payload["opportunities"]] == [posting.id]
    assert payload["meta"]["dropped_malformed"] == 1
    assert payload["meta"]["warnings"] == [{
        "company": "Acme",
        "warning": "Skipped 1 malformed posting row(s); other rows were kept",
    }]


def test_refresh_pipeline_marks_bounded_feed_caps_as_advisories(monkeypatch, tmp_path):
    posting = _posting("3", "Financial Analyst")
    _wire_pipeline(
        monkeypatch,
        tmp_path,
        raw=[posting],
        receipts=[{
            "company": "Acme", "ats": "workday", "result": "ok", "count": 1,
            "warning": "broad search capped at 200 newest roles across 1 term(s)",
            "warning_kind": "cap",
        }],
    )

    payload = jobs.refresh_board()

    assert payload["meta"]["warnings"] == [{
        "company": "Acme",
        "warning": "broad search capped at 200 newest roles across 1 term(s)",
        "kind": "cap",
    }]


def test_refresh_skips_workday_location_details_for_unrestricted_locations(monkeypatch, tmp_path):
    company = {
        "name": "Acme", "slug": "acme", "ats": "workday",
        "workday_host": "acme.wd1.myworkdayjobs.com",
        "workday_tenant": "acme", "workday_site": "External",
    }
    posting = workday._to_opportunity(
        {
            "title": "Finance Lead",
            "externalPath": "/job/New-York/Finance-Lead_R1",
            "locationsText": "3 Locations",
            "bulletFields": ["R1"],
        }, company, "acme", company["workday_host"], company["workday_site"])
    board_path = _wire_pipeline(
        monkeypatch,
        tmp_path,
        raw=[posting],
        receipts=[{"company": "Acme", "ats": "workday", "result": "ok", "count": 1}],
    )

    def should_not_fetch(*args, **kwargs):
        raise AssertionError("unrestricted locations should not resolve Workday details")

    monkeypatch.setattr(workday, "resolve_locations", should_not_fetch)
    payload = jobs.refresh_board()

    assert [item["id"] for item in payload["opportunities"]] == [posting.id]
    assert store.load_opportunities(board_path)[0].location == "3 Locations"


def test_refresh_pipeline_keeps_last_good_board_on_feed_outage(monkeypatch, tmp_path):
    prior = _posting("999999")
    board_path = _wire_pipeline(
        monkeypatch,
        tmp_path,
        raw=[],
        receipts=[{"company": "Acme", "ats": "greenhouse", "result": "error", "count": 0}],
        mock_enrich=False,
    )
    store.save([prior], now="2026-07-30T00:00:00Z")

    try:
        jobs.refresh_board()
    except RuntimeError as exc:
        assert "existing board was kept unchanged" in str(exc)
    else:
        raise AssertionError("an all-feed outage must fail closed")

    assert store.load_opportunities(board_path)[0].id == prior.id


def test_refresh_pipeline_does_not_advance_momentum_on_feed_outage(monkeypatch, tmp_path):
    """A failed refresh keeps both the board and its previous delta baseline."""
    import json

    _wire_pipeline(
        monkeypatch,
        tmp_path,
        raw=[],
        receipts=[{"company": "Acme", "ats": "greenhouse", "result": "error", "count": 0}],
    )
    momentum_path = tmp_path / ".momentum.json"
    momentum_path.write_text(json.dumps({"Acme": {"matching": 3, "total": 3}}), encoding="utf-8")
    monkeypatch.setattr(enrich, "MOMENTUM_SNAPSHOT", momentum_path)
    try:
        jobs.refresh_board()
    except RuntimeError as exc:
        assert "existing board was kept unchanged" in str(exc)
    else:
        raise AssertionError("an all-feed outage must fail closed")

    assert json.loads(momentum_path.read_text(encoding="utf-8")) == {
        "Acme": {"matching": 3, "total": 3},
    }


def test_refresh_pipeline_keeps_last_good_board_on_successful_empty_feed(monkeypatch, tmp_path):
    prior = _posting("888888")
    board_path = _wire_pipeline(
        monkeypatch,
        tmp_path,
        raw=[],
        receipts=[{"company": "Acme", "ats": "greenhouse", "result": "ok", "count": 0, "raw": 0}],
    )
    store.save([prior], now="2026-07-30T00:00:00Z")

    try:
        jobs.refresh_board()
    except RuntimeError as exc:
        assert "No actionable postings" in str(exc)
    else:
        raise AssertionError("a successful empty feed must not erase the last good board")

    assert store.load_opportunities(board_path)[0].id == prior.id


def test_hydration_fetch_errors_are_visible_in_refresh_summary(monkeypatch):
    from jobhunt.ats._http import FetchError

    posting = _posting()
    def fail(*args, **kwargs):
        raise FetchError("HTTP 500")

    monkeypatch.setattr(discover, "_hydrate_greenhouse", fail)
    summary = discover.hydrate_details([posting])
    assert summary["hydrate_errors"] == [{"id": posting.id, "error": "application form fetch failed"}]
