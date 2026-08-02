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
