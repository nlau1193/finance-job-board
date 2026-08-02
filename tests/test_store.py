"""Store tests: dedupe + read-state preservation across refreshes."""

from jobhunt import store
from jobhunt.model import Opportunity


def opp(job_id, title="FP&A Manager"):
    return Opportunity(
        id=Opportunity.make_id("greenhouse", "acme", job_id),
        company="Acme", title=title, location="New York, NY",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        ats="greenhouse", company_slug="acme", job_id=job_id,
    )


def test_merge_sets_first_and_last_seen(tmp_path):
    path = tmp_path / "jobs.json"
    merged = store.merge_read_state([opp("1"), opp("2")], path=path, now="2026-06-30T00:00:00Z")
    assert all(o.first_seen_at == "2026-06-30T00:00:00Z" for o in merged)
    assert all(o.last_seen_at == "2026-06-30T00:00:00Z" for o in merged)


def test_read_state_preserved_across_refresh(tmp_path):
    path = tmp_path / "jobs.json"
    # First refresh, mark one read + one dismissed.
    first = store.merge_read_state([opp("1"), opp("2"), opp("3")], path=path, now="2026-06-29T00:00:00Z")
    store.save(first, path=path, now="2026-06-29T00:00:00Z")
    store.set_flag("greenhouse:acme:1", read=True, path=path)
    store.set_flag("greenhouse:acme:2", dismissed=True, path=path)

    # Second refresh: same postings still live -> flags + first_seen carry over.
    second = store.merge_read_state([opp("1"), opp("2"), opp("3")], path=path, now="2026-06-30T00:00:00Z")
    by_id = {o.id: o for o in second}
    assert by_id["greenhouse:acme:1"].read is True
    assert by_id["greenhouse:acme:1"].first_seen_at == "2026-06-29T00:00:00Z"
    assert by_id["greenhouse:acme:1"].last_seen_at == "2026-06-30T00:00:00Z"
    assert by_id["greenhouse:acme:2"].dismissed is True
    assert by_id["greenhouse:acme:3"].read is False


def test_dedupe_by_id(tmp_path):
    path = tmp_path / "jobs.json"
    merged = store.merge_read_state([opp("1"), opp("1"), opp("2")], path=path)
    ids = [o.id for o in merged]
    assert ids.count("greenhouse:acme:1") == 1
    assert len(merged) == 2


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "jobs.json"
    payload = store.save([opp("1"), opp("2")], path=path, meta={"k": "v"})
    assert payload["meta"] == {"k": "v"}
    loaded = store.load_opportunities(path)
    assert {o.id for o in loaded} == {"greenhouse:acme:1", "greenhouse:acme:2"}


def test_load_drops_corrupt_rows_and_records_recovery_warning(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        '{"version": 1, "meta": {}, "opportunities": [null, '
        '{"id": "ok", "company": "Acme", "title": "Role", '
        '"location": "Remote", "url": "https://boards.greenhouse.io/acme/jobs/1", '
        '"ats": "greenhouse", "company_slug": "acme", "job_id": "1"}]}',
        encoding="utf-8",
    )
    data = store.load(path)
    assert [item["id"] for item in data["opportunities"]] == ["ok"]
    assert data["meta"]["recovery_warnings"] == ["Skipped 1 invalid stored posting row(s)"]
    assert store.load_opportunities(path)[0].id == "ok"


def test_load_drops_non_actionable_live_rows_but_keeps_demo_rows(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        '{"version": 1, "meta": {}, "opportunities": [{"id":"bad",'
        '"company":"Acme","title":"Role","location":"Remote",'
        '"url":"","ats":"greenhouse","company_slug":"acme","job_id":"1"}]}',
        encoding="utf-8",
    )
    assert store.load(path)["opportunities"] == []

    demo = tmp_path / "demo.json"
    demo.write_text(
        '{"version": 1, "meta": {"sample": true}, "opportunities": [{"id":"demo",'
        '"company":"Acme","title":"Role","location":"Remote",'
        '"url":"","ats":"greenhouse","company_slug":"acme","job_id":"1"}]}',
        encoding="utf-8",
    )
    assert store.load(demo)["opportunities"][0]["id"] == "demo"


def test_string_false_sample_marker_cannot_authorize_empty_apply_url(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        '{"version": 1, "meta": {"sample": "false"}, "opportunities": [{"id":"demo",'
        '"company":"Acme","title":"Role","location":"Remote",'
        '"url":"","ats":"greenhouse","company_slug":"acme","job_id":"1"}]}',
        encoding="utf-8",
    )

    data = store.load(path)

    assert data["opportunities"] == []
    assert any("invalid stored posting" in warning
               for warning in data["meta"]["recovery_warnings"])


def test_load_drops_rows_with_unrenderable_nested_state(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        '{"version": 1, "meta": {}, "opportunities": [{"id":"bad",'
        '"company":"Acme","title":"Role","location":"Remote",'
        '"url":"https://boards.greenhouse.io/acme/jobs/1",'
        '"ats":"greenhouse","company_slug":"acme","job_id":"1",'
        '"application":{"prompts":"oops"}}]}',
        encoding="utf-8",
    )
    assert store.load(path)["opportunities"] == []


def test_load_repairs_malformed_form_and_warm_entries(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        '{"version": 1, "meta": {}, "opportunities": [{"id":"ok",'
        '"company":"Acme","title":"Role","location":"Remote",'
        '"url":"https://boards.greenhouse.io/acme/jobs/1",'
        '"ats":"greenhouse","company_slug":"acme","job_id":"1",'
        '"application":{"prompts":[null,{"label":"Why Acme?"}],'
        '"gates":[{"label":"Work authorization"},"bad"]},'
        '"enrichment":{"application":{"effort":"light"},'
        '"warm":{"count":2,"people":[null,{"name":"Alex","position":"Finance"}]}}}]}',
        encoding="utf-8",
    )

    data = store.load(path)
    row = data["opportunities"][0]
    assert row["application"]["prompts"] == [{"label": "Why Acme?"}]
    assert row["application"]["gates"] == [{"label": "Work authorization"}]
    assert row["enrichment"]["warm"]["people"] == [{"name": "Alex", "position": "Finance"}]
    assert any("malformed" in warning for warning in data["meta"]["recovery_warnings"])

    # The repaired payload is safe to pass to the self-contained renderer.
    from jobhunt.board import render
    out = tmp_path / "board.html"
    render(data, out_path=out)
    assert out.exists()


def test_load_repairs_non_list_warm_people(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        '{"version": 1, "meta": {}, "opportunities": [{"id":"ok",'
        '"company":"Acme","title":"Role","location":"Remote",'
        '"url":"https://boards.greenhouse.io/acme/jobs/1",'
        '"ats":"greenhouse","company_slug":"acme","job_id":"1",'
        '"enrichment":{"warm":{"count":1,"people":"oops"}}}]}',
        encoding="utf-8",
    )
    row = store.load(path)["opportunities"][0]
    assert row["enrichment"]["warm"] == {"count": 0, "people": []}

    path.write_text(
        '{"version": 1, "meta": {}, "opportunities": [{"id":"bad",'
        '"company":"Acme","title":"Role","location":"Remote",'
        '"url":"https://boards.greenhouse.io/acme/jobs/1",'
        '"ats":"greenhouse","company_slug":"acme","job_id":"1",'
        '"enrichment":{"application":{"flags":"oops"}}}]}',
        encoding="utf-8",
    )
    assert store.load(path)["opportunities"] == []


def test_load_repairs_malformed_fit_lists_and_renders(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        '{"version": 1, "meta": {}, "opportunities": [{"id":"ok",'
        '"company":"Acme","title":"Role","location":"Remote",'
        '"url":"https://boards.greenhouse.io/acme/jobs/1",'
        '"ats":"greenhouse","company_slug":"acme","job_id":"1",'
        '"enrichment":{"fit":{"bucket":"APPLY",'
        '"why":"not-a-list","red_flags":[" senior ",4,""],'
        '"missing":[null," CPA "]}}}]}',
        encoding="utf-8",
    )

    data = store.load(path)
    fit = data["opportunities"][0]["enrichment"]["fit"]
    assert fit["why"] == []
    assert fit["red_flags"] == ["senior"]
    assert fit["missing"] == ["CPA"]
    assert any("malformed fit" in warning for warning in data["meta"]["recovery_warnings"])

    # The repaired fit arrays are safe for the board's `.map(...)` rendering.
    from jobhunt.board import render
    out = tmp_path / "board.html"
    render(data, out_path=out)
    assert out.exists()


def test_load_resets_corrupt_metadata_shape(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text('{"version": 1, "meta": [], "opportunities": []}', encoding="utf-8")
    data = store.load(path)
    assert data["meta"]["recovery_warnings"] == ["Reset invalid board metadata"]


def test_load_resets_corrupt_metadata_lists(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        '{"version": 1, "meta": {"errors": "oops", "warnings": "oops"}, '
        '"opportunities": []}',
        encoding="utf-8",
    )
    data = store.load(path)
    assert data["meta"]["errors"] == []
    assert data["meta"]["warnings"] == []
    assert data["meta"]["recovery_warnings"] == [
        "Reset invalid board metadata field 'errors'",
        "Reset invalid board metadata field 'warnings'",
    ]


def test_sort_unread_first(tmp_path):
    a, b = opp("1"), opp("2")
    a.read = True
    ordered = store.sort_for_board([a, b])
    assert ordered[0].id == "greenhouse:acme:2"  # unread first


def test_set_flag_missing_returns_false(tmp_path):
    path = tmp_path / "jobs.json"
    store.save([opp("1")], path=path)
    assert store.set_flag("greenhouse:acme:999", read=True, path=path) is False


def test_atomic_write_text_replaces_without_droppings(tmp_path):
    # Board/seed/dismissed files are written temp-file + os.replace, so a crash
    # mid-write (or a reader racing a writer) can never see a partial file.
    target = tmp_path / "nested" / "out.json"
    store.atomic_write_text(target, '{"a": 1}\n')          # creates parent dirs
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n'
    store.atomic_write_text(target, '{"a": 2}\n')          # replaces in place
    assert target.read_text(encoding="utf-8") == '{"a": 2}\n'
    # no temp files left behind next to the target
    assert [p.name for p in target.parent.iterdir()] == ["out.json"]


def test_save_and_dismissed_leave_no_temp_files(tmp_path):
    path = tmp_path / "jobs.json"
    store.save([opp("1")], path=path)
    store.set_flag("greenhouse:acme:1", read=True, path=path)
    dpath = tmp_path / "dismissed.json"
    store.set_dismissed("greenhouse:acme:1", True, dpath)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["dismissed.json", "jobs.json"]
    # and the files are valid JSON end-to-end
    assert store.load_opportunities(path)[0].read is True
    assert store.load_dismissed(dpath) == {"greenhouse:acme:1"}


def test_durable_dismissed_set(tmp_path):
    from jobhunt import store
    path = tmp_path / "dismissed.json"
    assert store.load_dismissed(path) == set()
    assert store.set_dismissed("greenhouse:acme:1", True, path) is True
    assert store.set_dismissed("greenhouse:acme:1", True, path) is False   # idempotent
    assert store.set_dismissed("ashby:brex:2", True, path) is True
    assert store.load_dismissed(path) == {"greenhouse:acme:1", "ashby:brex:2"}
    # undismiss removes it
    assert store.set_dismissed("greenhouse:acme:1", False, path) is True
    assert store.load_dismissed(path) == {"ashby:brex:2"}
    # tolerates a legacy {"ids": [...]} shape and junk
    path.write_text('{"ids": ["x", "y"]}', encoding="utf-8")
    assert store.load_dismissed(path) == {"x", "y"}
    path.write_text("not json", encoding="utf-8")
    assert store.load_dismissed(path) == set()
