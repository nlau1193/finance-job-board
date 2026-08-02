"""Board rendering tests: data injection, actionable links, empty state."""

import json
from pathlib import Path

import jobs
from jobhunt import board
from jobhunt.model import Opportunity, is_actionable_url


def opp(job_id, title="FP&A Manager"):
    return Opportunity(
        id=Opportunity.make_id("greenhouse", "stripe", job_id),
        company="Stripe", title=title, location="New York, NY",
        url=f"https://stripe.com/jobs/search?gh_jid={job_id}",
        ats="greenhouse", company_slug="stripe", job_id=job_id, tags=["fintech"],
    )


def test_build_board_data_counts_companies():
    data = board.build_board_data([opp("1"), opp("2")], generated_at="2026-06-30T00:00:00Z")
    assert data["meta"]["companies_with_postings"] == 1
    assert len(data["opportunities"]) == 2


def test_render_embeds_actionable_url(tmp_path):
    data = board.build_board_data([opp("7978019")], generated_at="2026-06-30T00:00:00Z")
    out = tmp_path / "index.html"
    board.render(data, out_path=out)
    html = out.read_text(encoding="utf-8")
    # The deep-link survives JSON embedding (escaped < but the gh_jid id is intact).
    assert "7978019" in html
    assert "gh_jid" in html
    assert "stripe.com/jobs/search" in html  # full posting URL embedded in the data blob


def test_board_amber_token_meets_small_text_contrast_bar():
    html = Path("templates/board.html").read_text(encoding="utf-8")
    assert "--amber:#805714" in html


def test_render_no_script_breakout(tmp_path):
    # A title containing </script> must not break out of the data tag.
    o = opp("1", title="Manager </script><script>alert(1)</script>")
    data = board.build_board_data([o], generated_at="2026-06-30T00:00:00Z")
    out = tmp_path / "index.html"
    board.render(data, out_path=out)
    html = out.read_text(encoding="utf-8")
    # The injected data must not contain a literal closing script tag.
    data_blob = html.split('id="board-data">', 1)[1].split("</script>", 1)[0]
    assert "<script>alert(1)</script>" not in data_blob
    assert "\\u003c" in data_blob  # angle brackets were escaped


def test_render_sanitizes_local_description_before_inner_html(tmp_path):
    # ATS adapters sanitize their payloads, but a user can still have an old or
    # hand-edited local board. The browser detail view uses innerHTML for the
    # allowlisted formatting, so render must enforce that boundary again.
    o = opp("1")
    o.description_html = '<p>safe</p><img src=x onerror="alert(1)"><script>alert(2)</script>'
    out = tmp_path / "index.html"
    board.render(board.build_board_data([o], generated_at="2026-06-30T00:00:00Z"), out_path=out)
    html = out.read_text(encoding="utf-8")
    assert "safe" in html
    assert "<img" not in html
    assert "<script>alert(2)</script>" not in html


def test_render_empty_board(tmp_path):
    data = board.build_board_data([], generated_at="2026-06-30T00:00:00Z")
    out = tmp_path / "index.html"
    board.render(data, out_path=out)
    html = out.read_text(encoding="utf-8")
    assert '"opportunities":[]' in html.replace(" ", "")
    assert "No postings loaded" in html  # empty-state copy is in the template
    assert "./jobs refresh" in html


def test_sample_board_cannot_offer_a_real_apply_destination():
    sample = json.loads(Path("data/jobs.sample.json").read_text(encoding="utf-8"))
    assert sample["meta"]["sample"] is True
    assert all(not is_actionable_url(o.get("url")) for o in sample["opportunities"])


def test_render_includes_demo_guard_and_bounded_list_controls(tmp_path):
    data = board.build_board_data(
        [opp("1")],
        generated_at="2026-06-30T00:00:00Z",
        meta={"sample": True},
    )
    out = tmp_path / "index.html"
    board.render(data, out_path=out)
    html = out.read_text(encoding="utf-8")
    assert "Demo only" in html
    assert "Demo board." in html
    assert "var visibleLimit = 100" in html
    assert 'id="show-more"' in html
    assert "rfFocusables" in html
    assert 'aria-modal="false"' in html
    assert 'document.body.classList.contains("detail-open") && e.key === "Tab"' in html


def test_large_board_uses_compact_browser_payload_but_keeps_local_data(tmp_path):
    data = board.build_board_data(
        [opp(str(i)) for i in range(301)],
        generated_at="2026-06-30T00:00:00Z",
    )
    data["opportunities"][0]["description_html"] = "<p>" + ("Long detail " * 5000) + "</p>"
    data["opportunities"][0]["application"] = {
        "effort": "heavy",
        "prompts": [{"label": "Tell us more"}] * 50,
    }
    out = tmp_path / "index.html"
    board.render(data, out_path=out)
    html = out.read_text(encoding="utf-8")
    assert '"board_compacted": true' in html
    assert "Large board" in html
    assert "Tell us more" not in html
    assert len(html) < 1_000_000
    assert len(data["opportunities"][0]["description_html"]) > 10_000


def test_refresh_health_floor_rejects_total_outage():
    assert not jobs._refresh_is_publishable(139, 200)
    assert jobs._refresh_is_publishable(140, 200)
    assert jobs._refresh_is_publishable(2, 2)
    assert not jobs._refresh_is_publishable(1, 2)
    assert not jobs._refresh_is_publishable(0, 200)
    assert not jobs._refresh_is_publishable(0, 0)


def test_empty_feed_guard_only_protects_broad_board():
    assert not jobs._refresh_is_publishable(
        1, 1, actionable_count=0, raw_total=0, prior_count=1, protect_empty=True
    )
    # A focused search may legitimately have no matching source rows; publish
    # that empty result instead of leaving unrelated roles on screen.
    assert jobs._refresh_is_publishable(
        1, 1, actionable_count=0, raw_total=0, prior_count=1, protect_empty=False
    )
    assert not jobs._refresh_is_publishable(
        1, 1, actionable_count=0, raw_total=1, prior_count=1, protect_empty=True
    )
