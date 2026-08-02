"""Verifier uses the same actionable/deep-link pipeline as production."""

import importlib.util
from pathlib import Path

from jobhunt.model import Opportunity
from jobhunt.filter import Profile


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_universe.py"
    spec = importlib.util.spec_from_file_location("verify_universe_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _opp(url):
    return Opportunity(
        id="greenhouse:acme:1", company="Acme", title="Analyst",
        location="New York, NY", url=url, ats="greenhouse",
        company_slug="acme", job_id="1",
    )


def test_check_drops_non_actionable_feed_results(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "discover_company", lambda *a, **k: (
        [], {"result": "ok", "raw": 1, "dropped_non_actionable": 1}
    ))
    result = module._check(
        {"name": "Acme", "slug": "acme", "ats": "greenhouse"},
        Profile(), None,
    )
    assert result["raw"] == 1
    assert result["actionable"] == 0
    assert result["resolved"] is False


def test_check_turns_malformed_feed_into_a_dead_result(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "discover_company", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad feed")))
    result = module._check(
        {"name": "Acme", "slug": "acme", "ats": "greenhouse"},
        Profile(), None,
    )
    assert result["result"] == "error"
    assert result["resolved"] is False
    assert "bad feed" in result["error"]
