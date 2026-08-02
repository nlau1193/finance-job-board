"""Private search preferences and first-run behavior."""

import json
import types

import jobs


def _config_args(**overrides):
    values = {
        "titles": None,
        "exclude": None,
        "locations": None,
        "companies": None,
        "remote": None,
        "max_age": None,
        "bio": None,
        "reset": False,
        "interactive": False,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _isolate_search(tmp_path, monkeypatch):
    example = tmp_path / "search.example.json"
    local = tmp_path / "search.local.json"
    example.write_text(json.dumps({
        "title_keywords": ["finance analyst"],
        "companies": [],
        "title_exclude": ["intern"],
        "locations": ["new york"],
        "remote_ok": True,
        "max_age_days": 30,
        "referral_bio": "I work in finance",
        "fit": {"skills": ["forecasting"]},
    }), encoding="utf-8")
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    monkeypatch.setattr(jobs, "SEARCH_EXAMPLE", example)
    monkeypatch.setattr(jobs, "SEARCH_LOCAL", local)
    return example, local


def test_private_search_is_created_from_public_starter(tmp_path, monkeypatch):
    example, local = _isolate_search(tmp_path, monkeypatch)
    assert jobs._ensure_search_local() == local
    assert local.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")


def test_configure_updates_search_without_losing_fit_rules(tmp_path, monkeypatch):
    _, local = _isolate_search(tmp_path, monkeypatch)
    rc = jobs.cmd_configure(_config_args(
        titles="strategic finance, FP&A",
        locations="Boston, remote",
        remote="no",
        max_age=14,
        bio="I build forecasts",
        companies="Ramp, stripe",
    ))
    assert rc == 0
    saved = json.loads(local.read_text(encoding="utf-8"))
    assert saved["title_keywords"] == ["strategic finance", "FP&A"]
    assert saved["locations"] == ["Boston", "remote"]
    assert saved["remote_ok"] is False
    assert saved["max_age_days"] == 14
    assert saved["referral_bio"] == "I build forecasts"
    assert saved["companies"] == ["Ramp", "stripe"]
    assert saved["fit"] == {"skills": ["forecasting"]}


def test_configure_all_titles_clears_title_filter(tmp_path, monkeypatch):
    _isolate_search(tmp_path, monkeypatch)
    assert jobs.cmd_configure(_config_args(titles="all")) == 0
    saved = json.loads((tmp_path / "search.local.json").read_text(encoding="utf-8"))
    assert saved["title_keywords"] == []


def test_configure_none_clears_title_exclusions(tmp_path, monkeypatch):
    _isolate_search(tmp_path, monkeypatch)
    for value in ("none", ""):
        assert jobs.cmd_configure(_config_args(exclude=value)) == 0
        saved = json.loads((tmp_path / "search.local.json").read_text(encoding="utf-8"))
        assert saved["title_exclude"] == []


def test_cli_rejects_invalid_server_port():
    import pytest

    with pytest.raises(SystemExit):
        jobs.main(["serve", "--port", "-1"])


def test_configure_all_locations_uses_explicit_all_sentinel(tmp_path, monkeypatch):
    _isolate_search(tmp_path, monkeypatch)
    assert jobs.cmd_configure(_config_args(locations="all")) == 0
    saved = json.loads((tmp_path / "search.local.json").read_text(encoding="utf-8"))
    assert saved["locations"] == ["all"]


def test_bare_configure_runs_plain_english_setup(tmp_path, monkeypatch):
    _, local = _isolate_search(tmp_path, monkeypatch)
    answers = iter([
        "FP&A, strategic finance",
        "Chicago",
        "Ramp, Stripe",
        "no",
        "21",
    ])
    args = _config_args(input_fn=lambda _prompt: next(answers))

    assert jobs.cmd_configure(args) == 0

    saved = json.loads(local.read_text(encoding="utf-8"))
    assert saved["title_keywords"] == ["FP&A", "strategic finance"]
    assert saved["locations"] == ["Chicago"]
    assert saved["companies"] == ["Ramp", "Stripe"]
    assert saved["remote_ok"] is False
    assert saved["max_age_days"] == 21
    # The common setup does not collect private referral copy. Keep the
    # optional value already present, and require --bio for an explicit change.
    assert saved["referral_bio"] == "I work in finance"


def test_configure_rejects_negative_age(tmp_path, monkeypatch):
    _isolate_search(tmp_path, monkeypatch)
    assert jobs.cmd_configure(_config_args(max_age=-1)) == 1


def test_configure_reset_restores_public_starter(tmp_path, monkeypatch):
    example, local = _isolate_search(tmp_path, monkeypatch)
    local.write_text('{"title_keywords":["changed"]}', encoding="utf-8")
    assert jobs.cmd_configure(_config_args(reset=True)) == 0
    assert local.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")


def test_profile_path_prefers_private_file(tmp_path, monkeypatch):
    example, local = _isolate_search(tmp_path, monkeypatch)
    assert jobs._profile_path() == example
    local.write_text("{}", encoding="utf-8")
    assert jobs._profile_path() == local


def test_malformed_scalar_preferences_are_rejected_instead_of_coerced(tmp_path):
    from jobhunt.filter import Profile

    config = tmp_path / "search.local.json"
    config.write_text(json.dumps({
        "title_keywords": "backend",
        "locations": "Chicago",
        "remote_ok": "false",
        "max_age_days": "30",
    }), encoding="utf-8")

    try:
        Profile.load(config)
    except ValueError as exc:
        assert "title_keywords" in str(exc)
    else:
        raise AssertionError("scalar preferences must not be coerced into a search")


def test_empty_terms_and_non_text_bio_are_rejected(tmp_path):
    from jobhunt.filter import Profile

    config = tmp_path / "search.local.json"
    config.write_text(json.dumps({
        "title_keywords": [" "]
    }), encoding="utf-8")
    try:
        Profile.load(config)
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("blank search terms must be rejected")

    config.write_text(json.dumps({"referral_bio": 123}), encoding="utf-8")
    try:
        Profile.load(config)
    except ValueError as exc:
        assert "referral_bio" in str(exc)
    else:
        raise AssertionError("non-text referral bio must be rejected")


def test_profile_storage_key_is_stable_and_search_scoped():
    first = {
        "title_keywords": ["analyst"], "title_exclude": [],
        "locations": ["New York"], "remote_ok": True,
        "max_age_days": 30, "companies": [], "referral_bio": "private",
    }
    second = dict(first, referral_bio="a different private bio")
    third = dict(first, locations=["Chicago"])
    assert jobs._profile_storage_key(first) == jobs._profile_storage_key(second)
    assert jobs._profile_storage_key(first) != jobs._profile_storage_key(third)
    assert jobs._profile_storage_key(first).startswith("p-")


def test_company_shortlist_accepts_names_and_slugs(tmp_path, monkeypatch):
    _, local = _isolate_search(tmp_path, monkeypatch)
    local.write_text(json.dumps({"companies": ["Ramp", "stripe"]}), encoding="utf-8")
    (tmp_path / "companies.json").write_text(json.dumps({
        "companies": [
            {"name": "Ramp", "slug": "ramp", "ats": "ashby"},
            {"name": "Stripe", "slug": "stripe", "ats": "greenhouse"},
            {"name": "Brex", "slug": "brex", "ats": "greenhouse"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(jobs, "CONFIG", tmp_path)

    assert [company["name"] for company in jobs._load_companies()] == ["Ramp", "Stripe"]


def test_company_shortlist_rejects_unknown_company(tmp_path, monkeypatch):
    _, local = _isolate_search(tmp_path, monkeypatch)
    local.write_text(json.dumps({"companies": ["Imaginary Finance Co"]}), encoding="utf-8")
    (tmp_path / "companies.json").write_text(json.dumps({
        "companies": [{"name": "Ramp", "slug": "ramp", "ats": "ashby"}],
    }), encoding="utf-8")
    monkeypatch.setattr(jobs, "CONFIG", tmp_path)

    try:
        jobs._load_companies()
    except ValueError as exc:
        assert "Imaginary Finance Co" in str(exc)
    else:
        raise AssertionError("unknown company should be rejected")


def test_company_catalog_rejects_malformed_entry(tmp_path, monkeypatch):
    _isolate_search(tmp_path, monkeypatch)
    (tmp_path / "companies.json").write_text(json.dumps({"companies": [None]}), encoding="utf-8")
    monkeypatch.setattr(jobs, "CONFIG", tmp_path)

    try:
        jobs._load_companies()
    except ValueError as exc:
        assert "entry 1 must be an object" in str(exc)
    else:
        raise AssertionError("malformed company catalog should fail closed")


def test_company_catalog_rejects_duplicate_identity(tmp_path, monkeypatch):
    _isolate_search(tmp_path, monkeypatch)
    (tmp_path / "companies.json").write_text(json.dumps({"companies": [
        {"name": "Ramp", "slug": "ramp", "ats": "ashby"},
        {"name": "Ramp copy", "slug": "ramp", "ats": "ashby"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(jobs, "CONFIG", tmp_path)

    try:
        jobs._load_company_catalog()
    except ValueError as exc:
        assert "duplicates entry 1" in str(exc)
    else:
        raise AssertionError("duplicate ATS/slug identity should fail closed")


def test_doctor_marks_partial_refresh_as_needing_attention(tmp_path, monkeypatch):
    _isolate_search(tmp_path, monkeypatch)
    board = tmp_path / "jobs.local.json"
    board.write_text(json.dumps({
        "version": 1,
        "generated_at": "2026-08-02T00:00:00Z",
        "meta": {"errors": [{"company": "Example", "error": "timeout"}]},
        "opportunities": [],
    }), encoding="utf-8")
    monkeypatch.setattr(jobs, "BOARD_FILE", board)
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    monkeypatch.setattr(jobs, "CONFIG", tmp_path)
    (tmp_path / "companies.json").write_text(json.dumps({"companies": [
        {"name": "Example", "slug": "example", "ats": "ashby"},
    ]}), encoding="utf-8")

    assert jobs.cmd_doctor(types.SimpleNamespace()) == 1


def test_configure_rejects_unknown_company_without_writing(tmp_path, monkeypatch):
    _, local = _isolate_search(tmp_path, monkeypatch)
    jobs._ensure_search_local()
    before = local.read_text(encoding="utf-8")
    (tmp_path / "companies.json").write_text(json.dumps({
        "companies": [{"name": "Ramp", "slug": "ramp", "ats": "ashby"}],
    }), encoding="utf-8")
    monkeypatch.setattr(jobs, "CONFIG", tmp_path)

    assert jobs.cmd_configure(_config_args(companies="Imaginary Co")) == 1
    assert local.read_text(encoding="utf-8") == before


def test_ensure_live_data_copies_sample_when_board_missing(tmp_path, monkeypatch):
    board = tmp_path / "jobs.local.json"
    sample = tmp_path / "jobs.sample.json"
    sample.write_text('{"opportunities": [{"id": "s1"}]}', encoding="utf-8")
    monkeypatch.setattr(jobs, "BOARD_FILE", board)
    monkeypatch.setattr(jobs, "SAMPLE_FILE", sample)
    monkeypatch.setattr(jobs, "_need_requests", lambda: False)

    jobs._ensure_live_data()

    assert board.exists()
    assert board.read_text(encoding="utf-8") == sample.read_text(encoding="utf-8")


def test_ensure_live_data_noop_when_board_exists(tmp_path, monkeypatch):
    board = tmp_path / "jobs.local.json"
    sample = tmp_path / "jobs.sample.json"
    board.write_text('{"opportunities": [{"id": "live"}]}', encoding="utf-8")
    sample.write_text('{"opportunities": [{"id": "sample"}]}', encoding="utf-8")
    monkeypatch.setattr(jobs, "BOARD_FILE", board)
    monkeypatch.setattr(jobs, "SAMPLE_FILE", sample)
    monkeypatch.setattr(
        jobs,
        "_need_requests",
        lambda: (_ for _ in ()).throw(AssertionError("must not check deps")),
    )

    jobs._ensure_live_data()

    assert '"live"' in board.read_text(encoding="utf-8")
