"""Application-form preview — classify what each posting's form actually asks.

The board's ATS fetchers can pull not just the job description but the
*application form* (Greenhouse `?questions=true`, Ashby's `applicationForm`
GraphQL). This module turns a raw form into a compact, board-ready shape that
surfaces the **signal** and hides the boilerplate:

- **prompts**  — free-form text questions (the essays worth prepping: "why this
  company", "describe a model you built", "explain your DCF experience").
- **gates**    — eligibility questions that can rule a role in/out (sponsorship,
  work authorization, in-office, start date, comp expectations).
- **requires_cover_letter** — a required cover-letter upload.

Everything else (name / email / phone / resume / LinkedIn URL / "how did you
hear" / EEO + demographic self-ID) is boilerplate and dropped.

Pure and network-free, mirroring `sanitize.py` / `enrich.py`: the ATS layer
fetches, this module classifies, and `application_summary` derives the
per-job "effort" badge the board renders.
"""

from __future__ import annotations

import re

from .model import utc_now

# Free-text field types across ATS platforms → a prompt candidate.
# Greenhouse: input_text / textarea.  Ashby: String / LongText / RichText.
_FREE_TEXT = {"textarea", "input_text", "longtext", "string", "richtext", "text"}

# --- keyword tables --------------------------------------------------------

# Standard identity / logistics / compliance fields we never surface.
_BOILERPLATE = (
    r"first name", r"last name", r"\bfull name\b", r"\bname\b", r"\bemail\b",
    r"\bphone\b", r"\bresume\b", r"\bcv\b", r"linkedin", r"github", r"portfolio",
    r"personal website", r"\bwebsite\b", r"\btwitter\b", r"\burl\b", r"\blinks?\b",
    r"social media",
    r"current (?:company|employer|title|role|position)", r"current/last",
    r"current or last", r"\bemployer\b", r"job title", r"do you know any",
    r"how did you hear", r"how did you find", r"how you found", r"where did you hear",
    r"current location", r"\blocation\b", r"\blocated\b", r"pronoun",
    r"if you (?:selected|responded|answered|chose)",
    # contact / logistics
    r"\bcity\b", r"address line", r"home address", r"mailing address", r"street address",
    r"zip code", r"postal code", r"primary residence",
    r"date of application", r"today's date", r"accommodation", r"were contacted by",
    # education fields
    r"universit", r"\bschool\b", r"\beducation\b", r"\bdegree\b", r"\bgpa\b",
    r"\balumni\b", r"graduat",
    # EEO / demographic self-identification
    r"\bgender\b", r"\brace\b", r"ethnic", r"hispanic", r"latino", r"veteran",
    r"disability", r"national origin", r"sexual orientation", r"transgender",
    r"self-identif", r"self identif",
)

# A required cover letter is a distinct signal (effort), not a prompt.
_COVER_LETTER = (r"cover letter",)

# Eligibility gates → surfaced as flags. Order-independent; first match wins.
_GATES = {
    "sponsorship": (r"sponsor", r"\bvisa\b"),
    "work_auth": (r"authoriz(?:ed|ation) to work", r"legally authorized",
                  r"right to work", r"work authorization"),
    "in_office": (r"based in", r"in[- ]office", r"on[- ]?site", r"relocat",
                  r"\bcommute\b", r"days (?:a|per) week", r"\bhybrid\b",
                  r"intend to work", r"work location", r"where are you (?:located|based)"),
    "start_date": (r"start date", r"notice period", r"when can you start",
                   r"available to start", r"earliest start"),
    "comp": (r"salary expectation", r"compensation expectation",
             r"desired.*salary", r"salary requirement", r"expected compensation",
             r"salary range", r"base salary", r"compensation range",
             r"desired.*compensation", r"pay expectation"),
}

# Short chip label per gate kind for the board's effort card. None = list only.
_GATE_FLAG = {
    "sponsorship": "sponsorship",
    "work_auth": "work auth",
    "in_office": "in-office",
    "start_date": "start date",
    "comp": "comp expectation",
    "other": None,
}


def _matches(text: str, patterns) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify_field(label: str, ftype: str) -> tuple[str, str | None]:
    """Bucket one form field.

    Returns `(bucket, kind)` where bucket is one of
    `prompt` | `gate` | `cover_letter` | `boilerplate`, and `kind` is the gate
    subtype (only when bucket == "gate").

    Gates are checked before boilerplate so an in-office question phrased with
    the word "location" ("can you be based in our office?") is not swallowed by
    the boilerplate `location` rule.
    """
    label_l = (label or "").strip().lower()
    ftype_l = (ftype or "").strip().lower()

    if _matches(label_l, _COVER_LETTER):
        return ("cover_letter", None)
    for kind, patterns in _GATES.items():
        if _matches(label_l, patterns):
            return ("gate", kind)
    if _matches(label_l, _BOILERPLATE):
        return ("boilerplate", None)
    if ftype_l in _FREE_TEXT and label_l:
        return ("prompt", None)
    return ("boilerplate", None)


def _assemble(prompts: list[dict], gates: list[dict],
              requires_cover_letter: bool) -> dict:
    return {
        "extractable": True,
        "prompts": prompts,
        "gates": gates,
        "requires_cover_letter": bool(requires_cover_letter),
        "fetched_at": utc_now(),
    }


def not_extractable() -> dict:
    """Placeholder for platforms whose form we can't read (e.g. Lever)."""
    return {
        "extractable": False,
        "prompts": [],
        "gates": [],
        "requires_cover_letter": False,
        "fetched_at": utc_now(),
    }


def _collect(fields) -> dict:
    """Shared classifier loop over normalized (label, type, required) fields."""
    prompts, gates = [], []
    requires_cover_letter = False
    for label, ftype, required in fields:
        label = (label or "").strip()
        if not label:
            continue
        bucket, kind = classify_field(label, ftype)
        if bucket == "prompt":
            prompts.append({"label": label, "required": bool(required)})
        elif bucket == "gate":
            gates.append({"label": label, "kind": kind, "required": bool(required)})
        elif bucket == "cover_letter":
            if required:
                requires_cover_letter = True
    return _assemble(prompts, gates, requires_cover_letter)


def extract_greenhouse(questions) -> dict:
    """Greenhouse `?questions=true` → application preview.

    Each question has a top-level `label` + `required`, and a `fields[]` list
    whose first entry carries the input `type` (input_text / textarea /
    input_file / multi_value_single_select …).
    """
    def norm():
        for q in questions or []:
            if not isinstance(q, dict):
                continue
            fields = q.get("fields") or []
            ftype = (fields[0].get("type") if fields and isinstance(fields[0], dict) else "") or ""
            yield (q.get("label"), ftype, q.get("required"))
    return _collect(list(norm()))


def extract_ashby(sections) -> dict:
    """Ashby `applicationForm.sections[].fieldEntries[]` → application preview.

    `field` arrives as a JSON object with `title` + `type`
    (String / LongText / Boolean / Email / File / Location …); `isRequired`
    lives on the field entry.
    """
    def norm():
        for section in sections or []:
            if not isinstance(section, dict):
                continue
            for entry in section.get("fieldEntries") or []:
                if not isinstance(entry, dict):
                    continue
                field = entry.get("field") or {}
                if not isinstance(field, dict):
                    continue
                required = entry.get("isRequired")
                if required is None:
                    required = field.get("isRequired")
                yield (field.get("title") or field.get("label"),
                       field.get("type"), required)
    return _collect(list(norm()))


def application_summary(application: dict) -> dict:
    """Per-job effort badge + gate flags, derived from an application preview.

    Returns `{}` for empty or non-extractable forms (the board then shows a
    neutral "opens on apply page" note instead of a badge).

    Effort: `quick` = no prompts and no required cover letter · `light` = 1–2
    prompts · `heavy` = 3+ prompts or a required cover letter.
    """
    if not application or not application.get("extractable"):
        return {}
    prompts = application.get("prompts") or []
    gates = application.get("gates") or []
    cover = bool(application.get("requires_cover_letter"))
    n = len(prompts)

    if n == 0 and not cover:
        effort = "quick"
    elif n >= 3 or cover:
        effort = "heavy"
    else:
        effort = "light"

    flags: list[str] = []
    if cover:
        flags.append("cover letter")
    seen: set[str] = set()
    for gate in gates:
        kind = gate.get("kind")
        chip = _GATE_FLAG.get(kind)
        if chip and kind not in seen:
            flags.append(chip)
            seen.add(kind)

    return {
        "effort": effort,
        "prompt_count": n,
        "gate_count": len(gates),
        "flags": flags,
    }
