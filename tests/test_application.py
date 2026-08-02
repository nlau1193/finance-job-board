"""Application-form classification + extraction tests (no network).

Fixtures capture real Greenhouse `?questions=true` and Ashby `applicationForm`
shapes. We assert the classifier surfaces free-form prompts + eligibility gates
and drops boilerplate (name/email/resume/URLs/EEO), and that the effort badge
buckets correctly.
"""

from jobhunt import application


# --- classify_field --------------------------------------------------------

def test_classify_boilerplate():
    assert application.classify_field("First Name", "input_text")[0] == "boilerplate"
    assert application.classify_field("Email", "Email")[0] == "boilerplate"
    assert application.classify_field("Resume", "File")[0] == "boilerplate"
    assert application.classify_field("Linkedin URL", "String")[0] == "boilerplate"
    assert application.classify_field("Gender", "multi_value_single_select")[0] == "boilerplate"
    assert application.classify_field("How did you hear about this job?", "input_text")[0] == "boilerplate"


def test_classify_prompt():
    assert application.classify_field("Why do you want to work here?", "textarea")[0] == "prompt"
    assert application.classify_field(
        "Please share your experience with DCF models.", "LongText")[0] == "prompt"
    # "Additional information / a note" is a real free-form prompt, not boilerplate.
    assert application.classify_field(
        "Additional information or a note you want to share", "LongText")[0] == "prompt"


def test_classify_gate_kinds():
    b, k = application.classify_field(
        "Will you now or in the future require company sponsorship?", "multi_value_single_select")
    assert (b, k) == ("gate", "sponsorship")
    b, k = application.classify_field("Can you be based in our San Francisco office?", "Boolean")
    assert (b, k) == ("gate", "in_office")
    b, k = application.classify_field("Are you legally authorized to work in the US?", "Boolean")
    assert (b, k) == ("gate", "work_auth")


def test_classify_cover_letter():
    assert application.classify_field("Cover Letter", "input_file")[0] == "cover_letter"


# --- extraction ------------------------------------------------------------

def test_extract_greenhouse(load_fixture):
    data = load_fixture("greenhouse_questions.json")
    app = application.extract_greenhouse(data["questions"])

    assert app["extractable"] is True
    labels = [p["label"] for p in app["prompts"]]
    assert "Why do you want to work here?" in labels
    assert "Describe a forecasting model you built end to end." in labels
    assert len(app["prompts"]) == 2
    # boilerplate is dropped
    assert not any("Name" in lbl for lbl in labels)
    assert not any("Gender" == lbl for lbl in labels)
    # sponsorship surfaced as a gate; optional cover letter does not flag required
    assert any(g["kind"] == "sponsorship" for g in app["gates"])
    assert app["requires_cover_letter"] is False


def test_extract_ashby(load_fixture):
    data = load_fixture("ashby_form.json")
    sections = data["data"]["jobPosting"]["applicationForm"]["sections"]
    app = application.extract_ashby(sections)

    assert app["extractable"] is True
    assert len(app["prompts"]) == 3  # DCF, IRR, additional note
    kinds = {g["kind"] for g in app["gates"]}
    assert "sponsorship" in kinds and "in_office" in kinds
    # Name/Email/Phone/File/Location/URL all dropped
    labels = [p["label"] for p in app["prompts"]]
    assert not any("Linkedin" in lbl for lbl in labels)


# --- effort summary --------------------------------------------------------

def test_summary_light(load_fixture):
    app = application.extract_greenhouse(load_fixture("greenhouse_questions.json")["questions"])
    summary = application.application_summary(app)
    assert summary["effort"] == "light"  # 2 prompts, no required cover letter
    assert summary["prompt_count"] == 2
    assert "sponsorship" in summary["flags"]


def test_summary_heavy(load_fixture):
    sections = load_fixture("ashby_form.json")["data"]["jobPosting"]["applicationForm"]["sections"]
    app = application.extract_ashby(sections)
    summary = application.application_summary(app)
    assert summary["effort"] == "heavy"  # 3 prompts
    assert "in-office" in summary["flags"] and "sponsorship" in summary["flags"]


def test_summary_quick():
    app = application.extract_greenhouse([
        {"label": "First Name", "required": True, "fields": [{"type": "input_text"}]},
        {"label": "Resume", "required": True, "fields": [{"type": "input_file"}]},
    ])
    assert application.application_summary(app)["effort"] == "quick"


def test_cover_letter_forces_heavy():
    app = application.extract_greenhouse([
        {"label": "Cover Letter", "required": True, "fields": [{"type": "input_file"}]},
    ])
    assert app["requires_cover_letter"] is True
    summary = application.application_summary(app)
    assert summary["effort"] == "heavy"
    assert "cover letter" in summary["flags"]


def test_not_extractable():
    ne = application.not_extractable()
    assert ne["extractable"] is False
    assert ne["prompts"] == []
    # summary is empty so the board shows a neutral note, not a badge
    assert application.application_summary(ne) == {}
    assert application.application_summary({}) == {}
