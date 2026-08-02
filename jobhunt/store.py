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

from .model import Opportunity, utc_now

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "jobs.local.json"
# Durable, LOCAL-ONLY (gitignored) set of dismissed posting ids. A dismissed id
# is excluded from EVERY future refresh, so a hidden role never comes back — even
# if it drops off the ATS and reappears later with the same id.
DISMISSED_PATH = Path(__file__).resolve().parents[1] / "data" / "dismissed.json"

# The board's local server handles requests on threads (and a background refresh
# can save while a dismiss lands), so the dismissed set's read-modify-write must
# be serialized or a near-simultaneous pair of dismisses loses one.
_DISMISSED_LOCK = threading.Lock()


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
    with _DISMISSED_LOCK:
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
