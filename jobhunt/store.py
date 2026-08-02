"""Persist the board to data/jobs.local.json with dedupe + read-state carry-over.

A refresh replaces the active board with the currently-open postings, but read
and dismissed flags (and the original first-seen timestamp) are carried over for
any posting that is still live, so the applicant's triage survives a refresh.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .model import Opportunity, is_actionable_url, utc_now

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "jobs.local.json"
# Durable, LOCAL-ONLY (gitignored) set of dismissed posting ids. A dismissed id
# is excluded from EVERY future refresh, so a hidden role never comes back — even
# if it drops off the ATS and reappears later with the same id.
DISMISSED_PATH = Path(__file__).resolve().parents[1] / "data" / "dismissed.json"


def _dismissed_path_for_board(path: Path) -> Path:
    """Return the sibling tombstone file for a board path.

    Production uses ``data/jobs.local.json`` and ``data/dismissed.json``. Keeping
    custom board paths paired with a sibling file makes store tests and isolated
    installs deterministic without ever consulting another checkout's state.
    """
    path = Path(path)
    if path == DEFAULT_PATH:
        return DISMISSED_PATH
    return path.with_name("dismissed.json")

# Fields that make a stored row renderable and safe to hand to Opportunity.
# Local JSON is intentionally treated as recoverable user state: a cancelled
# copy, hand edit, or older version must not take down `jobs board`.
_REQUIRED_OPPORTUNITY_FIELDS = (
    "id", "company", "title", "location", "url", "ats", "company_slug", "job_id",
)

# The board's local server handles requests on threads (and a background refresh
# can save while a dismiss lands).  Keep every local state mutation under one
# re-entrant lock: atomic replace prevents partial files, but without this lock a
# later refresh save can still overwrite a read/dismiss flag written moments ago.
_BOARD_STATE_LOCK = threading.RLock()


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via a same-directory temp file + os.replace, so a
    crash mid-write or a concurrent reader never sees a partial/corrupt file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_dismissed(path: Path = DISMISSED_PATH) -> set:
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError):
        return set()
    if isinstance(data, dict):
        data = data.get("ids", [])
    # The file is local state and can be hand-edited or truncated into a valid
    # but unusable JSON shape.  Treat anything other than the generated list as
    # an empty set so refresh can recover instead of crashing on iteration.
    if not isinstance(data, list):
        return set()
    return {str(x) for x in data if x}


def set_dismissed(opp_id: str, dismissed: bool = True, path: Path = DISMISSED_PATH) -> bool:
    """Add (dismissed=True) or remove (False) an id from the durable dismissed set.
    Returns True if the set changed."""
    with _BOARD_STATE_LOCK:
        ids = load_dismissed(path)
        had = str(opp_id) in ids
        if dismissed:
            ids.add(str(opp_id))
        else:
            ids.discard(str(opp_id))
        changed = (str(opp_id) in ids) != had
        atomic_write_text(path, json.dumps(sorted(ids), indent=2) + "\n")
        return changed

def load(path: Path = DEFAULT_PATH) -> dict:
    if not Path(path).exists():
        return {"version": 1, "opportunities": [], "meta": {}}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("opportunities"), list):
        raise ValueError(f"{path} is not a valid board file")
    recovery_messages = []
    if not isinstance(data.get("meta"), dict):
        data = dict(data)
        data["meta"] = {}
        recovery_messages.append("Reset invalid board metadata")
    else:
        data = dict(data)
        meta = dict(data["meta"])
        for field in ("errors", "warnings", "recovery_warnings"):
            if field in meta and not isinstance(meta[field], list):
                meta[field] = []
                recovery_messages.append(f"Reset invalid board metadata field '{field}'")
        data["meta"] = meta
    # Only the generated boolean marker can authorize a demo row with no Apply
    # URL. Treat stringy hand-edits such as "false" as false; truthiness here
    # would turn a malformed local file into an empty-link bypass.
    allow_empty_url = data["meta"].get("sample") is True
    valid = []
    invalid_count = 0
    for item in data["opportunities"]:
        # Older/local hand-edited board files can contain a valid-looking row
        # with one malformed nested value.  Repair the list fields that the
        # browser dereferences instead of throwing away the whole posting.
        # Keep the recovery note visible so the owner can clean the source file
        # without making a single bad connection/form entry take down the board.
        if isinstance(item, dict):
            application = item.get("application")
            if isinstance(application, dict):
                for field, predicate, label in (
                    ("prompts", lambda value: isinstance(value, dict)
                     and isinstance(value.get("label"), str)
                     and bool(value.get("label", "").strip()), "application prompt"),
                    ("gates", lambda value: isinstance(value, dict)
                     and isinstance(value.get("label"), str)
                     and bool(value.get("label", "").strip()), "application gate"),
                ):
                    values = application.get(field)
                    if isinstance(values, list):
                        cleaned = [value for value in values if predicate(value)]
                        removed = len(values) - len(cleaned)
                        if removed:
                            application[field] = cleaned
                            entry_word = "entry" if removed == 1 else "entries"
                            recovery_messages.append(
                                f"Removed {removed} malformed {label} {entry_word}"
                            )

            enrichment = item.get("enrichment")
            fit = enrichment.get("fit") if isinstance(enrichment, dict) else None
            if isinstance(fit, dict):
                for field in ("why", "red_flags", "missing"):
                    values = fit.get(field)
                    if isinstance(values, list):
                        cleaned = [value.strip() for value in values
                                   if isinstance(value, str) and value.strip()]
                        removed = len(values) - len(cleaned)
                    else:
                        cleaned = []
                        removed = 1 if field in fit else 0
                    if removed:
                        fit[field] = cleaned
                        entry_word = "entry" if removed == 1 else "entries"
                        recovery_messages.append(
                            f"Removed {removed} malformed fit {field} {entry_word}"
                        )
            warm = enrichment.get("warm") if isinstance(enrichment, dict) else None
            if isinstance(warm, dict) and "people" in warm:
                people = warm.get("people")
                cleaned_people = (
                    [person for person in people
                     if isinstance(person, dict)
                     and isinstance(person.get("name"), str)
                     and bool(person.get("name", "").strip())]
                    if isinstance(people, list) else []
                )
                removed = (len(people) - len(cleaned_people)
                           if isinstance(people, list) else 1)
                if removed or not isinstance(people, list):
                    warm["people"] = cleaned_people
                    # A non-empty count with no safe person would make the
                    # board render an empty warm card as if it were a match.
                    if not cleaned_people:
                        warm["count"] = 0
                    entry_word = "entry" if removed == 1 else "entries"
                    recovery_messages.append(
                        f"Removed {removed} malformed warm connection {entry_word}"
                    )
        application = item.get("application") if isinstance(item, dict) else None
        enrichment = item.get("enrichment") if isinstance(item, dict) else None
        enriched_application = enrichment.get("application") if isinstance(enrichment, dict) else None
        valid_item = (
            isinstance(item, dict)
            and all(isinstance(item.get(field), str)
                    for field in _REQUIRED_OPPORTUNITY_FIELDS)
            and all(item[field].strip() for field in _REQUIRED_OPPORTUNITY_FIELDS
                    if field not in ("url", "location"))
            and ((allow_empty_url and item["url"].strip() == "")
                 or is_actionable_url(item["url"]))
            and ("tags" not in item or isinstance(item["tags"], list))
            # Read/dismissed are browser state, not free-form JSON. Reject
            # string values such as "false" instead of letting JavaScript
            # treat them as truthy and hide a posting from the board.
            and ("read" not in item or isinstance(item["read"], bool))
            and ("dismissed" not in item or isinstance(item["dismissed"], bool))
            and ("application" not in item or (
                isinstance(application, dict)
                and all(isinstance(application.get(field), list)
                        for field in ("prompts", "gates", "flags")
                        if field in application)
            ))
            and ("enrichment" not in item or (
                isinstance(enrichment, dict)
                and ("fit" not in enrichment or isinstance(enrichment["fit"], dict))
                and ("application" not in enrichment or (
                    isinstance(enriched_application, dict)
                    and all(isinstance(enriched_application.get(field), list)
                            for field in ("flags", "prompts")
                            if field in enriched_application)
                ))
            ))
        )
        if valid_item:
            valid.append(item)
        else:
            invalid_count += 1
    if invalid_count:
        data["opportunities"] = valid
        recovery_messages.append(f"Skipped {invalid_count} invalid stored posting row(s)")
    if recovery_messages:
        meta = dict(data.get("meta") or {})
        recovery = list(meta.get("recovery_warnings") or [])
        meta["recovery_warnings"] = recovery + recovery_messages
        data["meta"] = meta
    return data


def load_opportunities(path: Path = DEFAULT_PATH) -> list[Opportunity]:
    return [Opportunity.from_dict(o) for o in load(path).get("opportunities", [])]


def _existing_state(path: Path) -> dict[str, dict]:
    try:
        prior = load(path)
    except (ValueError, json.JSONDecodeError):
        return {}
    return {o["id"]: o for o in prior.get("opportunities", []) if isinstance(o, dict) and "id" in o}


def merge_read_state(new_opps: list[Opportunity], path: Path = DEFAULT_PATH,
                     *, now: str | None = None) -> list[Opportunity]:
    now = now or utc_now()
    prior = _existing_state(path)
    # Dedupe by id, keep first occurrence.
    seen: set[str] = set()
    merged: list[Opportunity] = []
    for opp in new_opps:
        if opp.id in seen:
            continue
        seen.add(opp.id)
        old = prior.get(opp.id)
        if old:
            opp.first_seen_at = old.get("first_seen_at") or now
            opp.read = bool(old.get("read", False))
            opp.dismissed = bool(old.get("dismissed", False))
        else:
            opp.first_seen_at = now
        opp.last_seen_at = now
        merged.append(opp)
    return merged


def sort_for_board(opportunities: list[Opportunity]) -> list[Opportunity]:
    # Active + unread first, then by company, then title.
    return sorted(
        opportunities,
        key=lambda o: (o.dismissed, o.read, o.company.lower(), o.title.lower()),
    )


def save(opportunities: list[Opportunity], path: Path = DEFAULT_PATH,
         *, meta: dict | None = None, now: str | None = None) -> dict:
    now = now or utc_now()
    with _BOARD_STATE_LOCK:
        # Refresh builds its merged list before saving. Re-read the just-written
        # local state while holding the same lock as set_flag so a concurrent
        # read/dismiss cannot be lost by this replacement write.
        prior = _existing_state(path)
        durable_dismissed = load_dismissed(path=_dismissed_path_for_board(path))
        for opportunity in opportunities:
            old = prior.get(opportunity.id)
            if old:
                opportunity.read = bool(old.get("read", opportunity.read))
                opportunity.dismissed = bool(old.get("dismissed", opportunity.dismissed))
            if opportunity.id in durable_dismissed:
                opportunity.dismissed = True

        ordered = sort_for_board(opportunities)
        payload = {
            "version": 1,
            "generated_at": now,
            "meta": meta or {},
            "opportunities": [o.to_dict() for o in ordered],
        }
        atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
        return payload


def set_flag(opp_id: str, *, read: bool | None = None, dismissed: bool | None = None,
             path: Path = DEFAULT_PATH) -> bool:
    """Mutate read/dismissed for one opportunity. Returns True if found."""
    with _BOARD_STATE_LOCK:
        data = load(path)
        found = False
        for o in data.get("opportunities", []):
            if o.get("id") == opp_id:
                if read is not None:
                    o["read"] = read
                if dismissed is not None:
                    o["dismissed"] = dismissed
                found = True
                break
        if found:
            atomic_write_text(path, json.dumps(data, indent=2) + "\n")
        return found
