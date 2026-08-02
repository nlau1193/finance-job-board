"""Dismissal validation and local-state recovery tests."""

import json
import types

import jobs
from jobhunt import store
from jobhunt.model import Opportunity


def _opportunity(job_id="123"):
    return Opportunity(
        id=Opportunity.make_id("greenhouse", "acme", job_id),
        company="Acme",
        title="Product Engineer",
        location="Remote - US",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        ats="greenhouse",
        company_slug="acme",
        job_id=job_id,
    )


def _isolate_store(monkeypatch, tmp_path, opportunities):
    board_path = tmp_path / "jobs.json"
    dismissed_path = tmp_path / "dismissed.json"
    real_load = store.load
    real_save = store.save
    real_set_dismissed = store.set_dismissed
    real_save(opportunities, path=board_path)

    def load(path=None):
        return real_load(board_path if path is None else path)

    def save(items, path=None, **kwargs):
        return real_save(items, path=board_path if path is None else path, **kwargs)

    def set_dismissed(opp_id, dismissed=True, path=None):
        return real_set_dismissed(
            opp_id, dismissed=dismissed,
            path=dismissed_path if path is None else path,
        )

    monkeypatch.setattr(store, "load", load)
    monkeypatch.setattr(store, "save", save)
    monkeypatch.setattr(store, "set_dismissed", set_dismissed)
    monkeypatch.setattr(jobs, "_build_board", lambda *_a, **_kw: None)
    return board_path, dismissed_path


def test_cli_dismiss_unknown_does_not_create_tombstone(tmp_path, monkeypatch, capsys):
    posting = _opportunity()
    board_path, dismissed_path = _isolate_store(monkeypatch, tmp_path, [posting])

    rc = jobs.cmd_flag(types.SimpleNamespace(id="greenhouse:acme:missing"), dismissed=True)

    assert rc == 1
    assert "No posting with id greenhouse:acme:missing" in capsys.readouterr().out
    assert store.load_opportunities(board_path)[0].id == posting.id
    assert store.load_dismissed(dismissed_path) == set()


def test_cli_dismiss_known_removes_posting_and_records_tombstone(tmp_path, monkeypatch):
    posting = _opportunity()
    board_path, dismissed_path = _isolate_store(monkeypatch, tmp_path, [posting])

    assert jobs.cmd_flag(types.SimpleNamespace(id=posting.id), dismissed=True) == 0

    assert store.load_opportunities(board_path) == []
    assert store.load_dismissed(dismissed_path) == {posting.id}


def test_load_dismissed_recovers_from_valid_but_unusable_json_shapes(tmp_path):
    path = tmp_path / "dismissed.json"
    for raw in ("42", "null", "true", '"one"', '{"ids":42}', '{"ids":null}'):
        path.write_text(raw, encoding="utf-8")
        assert store.load_dismissed(path) == set(), raw

    path.write_text(json.dumps({"ids": ["a", "b"]}), encoding="utf-8")
    assert store.load_dismissed(path) == {"a", "b"}
