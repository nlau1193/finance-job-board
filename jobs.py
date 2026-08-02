#!/usr/bin/env python3
"""Job Hunt Board CLI — one free, local entrypoint.

  ./jobs setup      one-time: check deps, create private search preferences
  ./jobs start      open the local board; refresh from the button
  ./jobs configure  update the private search preferences
  ./jobs refresh    pull live job postings from every company's official ATS
  ./jobs board      rebuild the board HTML from the current data (no network)
  ./jobs open       rebuild + open the board in your browser
  ./jobs doctor     health check: deps, config, last refresh, dead companies
  ./jobs read <id>     mark a posting read     ./jobs dismiss <id>   hide a posting
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config"
DATA = ROOT / "data"
BOARD_FILE = DATA / "jobs.local.json"
SAMPLE_FILE = DATA / "jobs.sample.json"
BOARD_HTML = ROOT / "artifacts" / "board" / "index.html"
SEARCH_LOCAL = CONFIG / "search.local.json"
SEARCH_EXAMPLE = CONFIG / "search.example.json"

# Shared, thread-safe refresh state — read by the /api/refresh/status endpoint,
# mutated (under the lock) by the background refresh thread.
_REFRESH_LOCK = threading.Lock()
_REFRESH_STATE = {"running": False, "phase": "idle", "done": 0, "total": 0,
                  "message": "", "started_at": None, "finished_at": None,
                  "result": None, "error": None}


# --- pretty output ---------------------------------------------------------

def say(msg=""): print(msg)
def ok(msg): print(f"  \033[32m✓\033[0m {msg}")
def warn(msg): print(f"  \033[33m!\033[0m {msg}")
def err(msg): print(f"  \033[31m✗\033[0m {msg}")
def head(msg): print(f"\n\033[1m{msg}\033[0m")


def _need_requests() -> bool:
    try:
        import requests  # noqa: F401
        return True
    except ImportError:
        err("Missing dependency: requests. Run:  pip install -r requirements.txt")
        return False


# --- commands --------------------------------------------------------------

def cmd_setup(args) -> int:
    head("Job Hunt Board setup")
    deps_ok = _need_requests()
    if deps_ok:
        ok("Python dependency `requests` is installed")

    if not SEARCH_LOCAL.exists():
        shutil.copy(SEARCH_EXAMPLE, SEARCH_LOCAL)
        ok("Created your private search preferences: config/search.local.json")
    else:
        ok("Private search preferences already present")

    companies = _load_companies()
    profile = _load_profile_raw()
    keyword_count = len(profile.get("title_keywords", []))
    ok(f"Loaded {len(companies)} companies and {keyword_count} title keywords")

    # Seed a sample board so `open` shows something before the first refresh.
    if SAMPLE_FILE.exists() and not BOARD_FILE.exists():
        shutil.copy(SAMPLE_FILE, BOARD_FILE)
        ok("Seeded a fictional sample board — run `./jobs refresh` for live postings")
    _build_board()
    ok(f"Built board: {BOARD_HTML}")

    head("Next steps")
    say("  1.  ./jobs configure      # optional: choose titles, locations, and fit")
    say("  2.  ./jobs start          # open the board; use its Refresh button")
    say("")
    return 0 if deps_ok else 1


def refresh_board(*, no_cache=False, no_forms=False, progress=None) -> dict:
    """Run the full refresh pipeline and return the saved payload dict.

    `progress`, if given, is called as progress(phase, done, total, message)
    where phase is one of: 'discovering','filtering','forms','enriching','saving','done'.
    `progress` may be None.
    """
    from jobhunt import store
    from jobhunt.discover import discover_all, hydrate_details
    from jobhunt.filter import Profile
    from jobhunt.model import utc_now

    try:
        prior_count = len(store.load(BOARD_FILE).get("opportunities", []))
    except (OSError, ValueError, json.JSONDecodeError):
        prior_count = 0

    def _emit(phase, done, total, message):
        if progress:
            progress(phase, done, total, message)

    companies = _load_companies()
    profile = Profile.load(_profile_path())
    profile_config = _load_profile_raw()

    def _discover_progress(done, total, receipt):
        _emit("discovering", done, total, f"{receipt.get('company')}…")

    _emit("discovering", 0, len(companies), "Resolving companies…")
    raw, receipts = discover_all(
        companies,
        use_cache=not no_cache,
        progress=_discover_progress,
        search_terms=profile.title_keywords,
    )

    _emit("filtering", 0, 0, "Matching your search…")
    # Title first, then a 3-way location verdict: keep, maybe (a location that
    # needs its description checked), or drop.
    from jobhunt.filter import (
        location_verdict, title_matches, jd_allows_remote_or_ny, remote_first_slugs,
    )

    # Workday's list API reports multi-location postings as an opaque
    # "N Locations", which the location filter can only drop. Resolve the real
    # location list first so a preferred location inside a multi-location req survives.
    # An unrestricted location profile already keeps these rows, so avoid detail
    # calls that cannot change the verdict. The Workday helper caps the remaining
    # refresh-wide detail work and samples tenants fairly.
    from jobhunt.ats import workday as workday_ats
    configured_locations = {
        term.strip().lower() for term in profile.locations
        if isinstance(term, str) and term.strip()
    }
    multi_loc = [o for o in raw
                 if o.ats == "workday" and workday_ats.is_multi_location(o.location)
                 and title_matches(o, profile)]
    location_warnings = []
    if configured_locations & {"all", "any"}:
        multi_loc = []
    if multi_loc:
        _emit("filtering", 0, 0,
              f"Resolving {len(multi_loc)} multi-location Workday postings…")
        location_cap = workday_ats._MAX_LOCATION_RESOLVES
        if len(multi_loc) > location_cap:
            location_warnings.append({
                "company": "Workday",
                "warning": (
                    f"Resolved {location_cap} of {len(multi_loc)} multi-location "
                    "postings; the remainder kept their feed location"
                ),
            })
        workday_ats.resolve_locations(
            multi_loc, companies, use_cache=not no_cache, max_targets=location_cap
        )

    keeps, maybes = [], []
    for o in raw:
        if not title_matches(o, profile):
            continue
        v = location_verdict(o, profile)
        if v == "keep":
            keeps.append(o)
        elif v == "maybe":
            maybes.append(o)

    # Hydrate keeps + maybes together (descriptions + forms) so a "maybe" can be
    # rescued from its own JD, then keep the maybes that are actually reachable.
    candidates = keeps + maybes
    _emit("forms", 0, len(candidates), "Previewing application forms…")
    details = hydrate_details(candidates, use_cache=not no_cache, forms=not no_forms)

    remote_first = remote_first_slugs(companies)
    rescued = [
        m for m in maybes
        if (profile.remote_ok and m.company_slug in remote_first)
        or jd_allows_remote_or_ny(m.description_html, profile)
    ]
    filtered = keeps + rescued
    if maybes:
        _emit("filtering", 0, 0,
              f"Rescued {len(rescued)}/{len(maybes)} onsite-city roles via remote/NY signal")

    # Freshness: only keep postings listed within profile.max_age_days (default 30).
    from jobhunt.filter import is_fresh
    max_age = getattr(profile, "max_age_days", 30)
    if max_age and max_age > 0:
        before = len(filtered)
        filtered = [o for o in filtered if is_fresh(o, max_age)]
        aged = before - len(filtered)
        if aged:
            _emit("filtering", 0, 0, f"Dropped {aged} postings older than {max_age} days")

    # Dismissed roles never come back: exclude the durable dismissed-id set from
    # every refresh (the applicant hides a role once and it stays gone).
    dismissed_ids = store.load_dismissed()
    if dismissed_ids:
        before = len(filtered)
        filtered = [o for o in filtered if o.id not in dismissed_ids]
        hidden = before - len(filtered)
        if hidden:
            _emit("filtering", 0, 0, f"Hid {hidden} dismissed postings")

    from jobhunt import enrich
    _emit("enriching", 0, len(filtered), "Enriching postings…")
    fit_cfg = _load_profile_raw().get("fit", {})
    # Stage the momentum baseline until both fail-closed publication gates and
    # the board save succeed. An outage must not become next refresh's baseline.
    enrich_summary = enrich.enrich_all(filtered, raw, fit=fit_cfg, persist_snapshot=False)

    now = utc_now()
    merged = store.merge_read_state(filtered, now=now)

    resolved = sum(1 for r in receipts if r.get("result") == "ok")
    errored = [r for r in receipts if r.get("result") != "ok"]
    warnings = location_warnings + [
        {"company": r.get("company"), "warning": r.get("warning")}
        for r in receipts if r.get("warning")
    ]
    for r in receipts:
        malformed = int(r.get("dropped_malformed", 0) or 0)
        if malformed:
            warnings.append({
                "company": r.get("company"),
                "warning": f"Skipped {malformed} malformed posting row(s); other rows were kept",
            })
    by_ats = {}
    for r in receipts:
        by_ats[r.get("ats", "?")] = by_ats.get(r.get("ats", "?"), 0) + (r.get("count") or 0)
    dropped = sum(r.get("dropped_non_actionable", 0) for r in receipts)
    dropped_malformed = sum(int(r.get("dropped_malformed", 0) or 0) for r in receipts)
    companies_with = len({o.company for o in merged})

    meta = {
        "companies_total": len(companies),
        "companies_resolved": resolved,
        "companies_with_postings": companies_with,
        "raw_matches": len(filtered),
        "dropped_non_actionable": dropped,
        "dropped_malformed": dropped_malformed,
        "by_ats": by_ats,
        "forms_extractable": sum(1 for o in filtered if (o.application or {}).get("extractable")),
        # Postings on an ATS whose application form the API can expose at all
        # (Greenhouse/Ashby). Lever/Workday never can — count them apart so
        # "X/Y previewed" doesn't read as Y-X failures.
        "forms_supported": sum(1 for o in filtered if o.ats in ("greenhouse", "ashby")),
        "connections_loaded": enrich_summary.get("connections_loaded", False),
        "errors": [{"company": r.get("company"), "result": r.get("result"),
                    "error": r.get("error")} for r in errored],
        "warnings": warnings,
        "hydrate_errors": details.get("hydrate_errors", []) if isinstance(details, dict) else [],
        "search_profile_key": _profile_storage_key(profile_config),
    }
    if not _refresh_is_publishable(resolved, len(companies)):
        raise RuntimeError(
            "Too few company feeds resolved to trust this refresh. Your existing "
            "board was kept unchanged; check the network and run `./jobs doctor` "
            "before retrying."
        )
    raw_total = sum(int(r.get("raw", r.get("count", 0)) or 0) for r in receipts)
    dropped_total = sum(int(r.get("dropped_non_actionable", 0) or 0) for r in receipts)
    if not _refresh_is_publishable(
        resolved,
        len(companies),
        actionable_count=len(raw),
        raw_total=raw_total,
        dropped_total=dropped_total,
        prior_count=prior_count,
        protect_empty=not profile.title_keywords,
    ):
        raise RuntimeError(
            "No actionable postings came back from the successful feeds. Your "
            "existing board was kept unchanged; check the network and run `./jobs doctor` "
            "before retrying."
        )
    _emit("saving", 0, 0, "Saving board…")
    payload = store.save(merged, now=now, meta=meta)
    enrich.write_momentum_snapshot(enrich_summary.get("momentum_snapshot", {}))
    _build_board(payload)
    _emit("done", len(merged), len(merged), f"{len(merged)} postings from {companies_with} companies")
    return payload


def _refresh_is_publishable(
    resolved: int,
    total: int,
    *,
    actionable_count: int | None = None,
    raw_total: int | None = None,
    dropped_total: int = 0,
    prior_count: int = 0,
    protect_empty: bool = False,
) -> bool:
    """Fail closed when fewer than 70% of configured feeds resolve.

    A majority outage is not a truthful job market. The caller keeps the last
    known-good board instead of replacing it with a badly partial refresh.
    """
    minimum = max(1, (total * 7 + 9) // 10)
    if not (total > 0 and resolved >= minimum):
        return False
    # For the broad any-role starter, a 200 response with an empty/malformed
    # payload from every feed is not evidence that the market disappeared. Keep
    # the last known-good board when it exists. A focused search is allowed to
    # publish zero matches because its server-side title query can be validly
    # empty.
    if protect_empty and prior_count and actionable_count == 0:
        return False
    return True


def cmd_refresh(args) -> int:
    if not _need_requests():
        return 1
    from jobhunt import store  # noqa: F401 — kept for parity with prior imports

    companies = _load_companies()
    head(f"Refreshing {len(companies)} companies (structured ATS APIs)…")

    progress_state = {"phase": None, "last_discovery": -1}

    def progress(phase, done, total, message):
        """Keep a terminal refresh visibly alive without spamming one line/job.

        The board UI already has a richer progress modal.  The CLI still needs
        a heartbeat for a large all-role search, where the first network batch
        can take several seconds.  Print every ten feeds plus phase changes and
        always flush so the output is useful when piped or run by an agent.
        """
        previous = progress_state["phase"]
        progress_state["phase"] = phase
        if phase == "discovering":
            should_print = (
                done == 0
                or done == total
                or done - progress_state["last_discovery"] >= 10
            )
            if should_print:
                progress_state["last_discovery"] = done
                print(
                    f"  Checking official company job feeds… {done}/{total}",
                    flush=True,
                )
            return
        if phase != previous or message:
            print(f"  {message}", flush=True)

    verbose = getattr(args, "verbose", False)

    def discover_receipt_progress(phase, done, total, message):
        # Nothing to print here; kept minimal. Live per-company errors are shown by
        # the receipt inspection below after refresh completes.
        pass

    try:
        payload = refresh_board(
            no_cache=args.no_cache,
            no_forms=getattr(args, "no_forms", False),
            progress=progress,
        )
    except RuntimeError as exc:
        err(str(exc))
        return 1

    opps = payload.get("opportunities", [])
    meta = payload.get("meta", {})
    resolved = meta.get("companies_resolved", 0)
    filtered_n = meta.get("raw_matches", 0)
    companies_with = meta.get("companies_with_postings", 0)
    dropped = meta.get("dropped_non_actionable", 0)
    errored = meta.get("errors", []) or []

    if verbose:
        for e in errored:
            warn(f"{e.get('company')}: {e.get('result')} {e.get('error','')}".rstrip())
    for item in meta.get("warnings", []) or []:
        warn(f"{item.get('company')}: {item.get('warning')}".rstrip())

    head("Refresh complete")
    ok(f"{len(opps)} postings from {companies_with} companies")
    say(f"     ATS resolved: {resolved}/{len(companies)} companies  ·  matching postings: {filtered_n}")
    if not getattr(args, "no_forms", False):
        supported = meta.get("forms_supported", filtered_n)
        unsupported = max(0, filtered_n - supported)
        forms_line = f"Application forms previewed: {meta.get('forms_extractable', 0)}/{supported} postings"
        if unsupported:
            forms_line += f" ({unsupported} on ATSes without form APIs)"
        say(f"     {forms_line}")
    if dropped:
        warn(f"Dropped {dropped} non-deep-link URLs (search pages) — board stays clickable-only")
    if errored:
        warn(f"{len(errored)} companies did not resolve (run `./jobs doctor` to see them)")
    say(f"     Board: {BOARD_HTML}")
    say("     Open it:  ./jobs open")
    return 0


def _start_refresh_async() -> bool:
    """Claim the shared refresh state and start the background worker.

    Returns False (and starts nothing) when a refresh is already running. The
    claim happens HERE, synchronously under the lock — not in the spawned
    thread — so two near-simultaneous /api/refresh POSTs can never both start a
    pipeline, and a status poll arriving right after the POST sees this run's
    "starting" state instead of the previous run's persisted "done"/"error"."""
    from jobhunt.model import utc_now

    with _REFRESH_LOCK:
        if _REFRESH_STATE["running"]:
            return False
        _REFRESH_STATE.update(running=True, phase="starting", done=0, total=0,
                              message="Starting refresh…", started_at=utc_now(),
                              finished_at=None, result=None, error=None)
    threading.Thread(target=_run_refresh_thread, daemon=True).start()
    return True


def _run_refresh_thread() -> None:
    """Background worker for the /api/refresh endpoint. Updates _REFRESH_STATE
    (always under _REFRESH_LOCK) as the pipeline advances. The state is normally
    claimed by _start_refresh_async before this runs; a direct call claims it
    here instead."""
    from jobhunt.model import utc_now

    with _REFRESH_LOCK:
        if not _REFRESH_STATE["running"]:  # direct invocation (not via _start_refresh_async)
            _REFRESH_STATE.update(done=0, total=0, started_at=utc_now(),
                                  finished_at=None, result=None, error=None)
        _REFRESH_STATE.update(running=True, phase="discovering",
                              message="Starting refresh…")

    def progress(phase, done, total, message):
        with _REFRESH_LOCK:
            _REFRESH_STATE.update(phase=phase, done=done, total=total, message=message)

    try:
        payload = refresh_board(progress=progress)
        with _REFRESH_LOCK:
            _REFRESH_STATE.update(
                phase="done",
                result={"postings": len(payload["opportunities"]),
                        "companies": payload["meta"].get("companies_with_postings"),
                        "warnings": len(payload["meta"].get("warnings") or []),
                        "errors": len(payload["meta"].get("errors") or [])},
                finished_at=utc_now(),
            )
    except Exception as exc:  # noqa: BLE001 — surface to the poller, never crash the server
        with _REFRESH_LOCK:
            _REFRESH_STATE.update(phase="error", error=str(exc), finished_at=utc_now())
    finally:
        with _REFRESH_LOCK:
            _REFRESH_STATE["running"] = False


def cmd_board(args) -> int:
    _build_board()
    ok(f"Built board: {BOARD_HTML}")
    return 0


def cmd_doctor(args) -> int:
    head("Job Hunt Board doctor")
    rc = 0

    pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        ok(f"Python {pyver}")
    else:
        err(f"Python {pyver} is too old — need 3.10+"); rc = 1
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    ok(f"Running in the project's private environment (.venv)") if in_venv else \
        warn("Not running inside .venv — `./jobs` normally handles this for you")

    if _need_requests():
        ok("requests installed")
    else:
        rc = 1

    try:
        companies = _load_companies()
        ok(f"config/companies.json valid — {len(companies)} companies")
        ats_counts = {}
        for c in companies:
            ats_counts[c.get("ats", "?")] = ats_counts.get(c.get("ats", "?"), 0) + 1
        say(f"     by ATS: {json.dumps(ats_counts)}")
    except Exception as exc:  # noqa: BLE001
        err(f"config/companies.json: {exc}"); rc = 1

    try:
        from jobhunt.filter import Profile
        p = Profile.load(_profile_path())
        ok(f"{_profile_path().relative_to(ROOT)} valid — {len(p.title_keywords)} keywords, locations={p.locations}")
    except Exception as exc:  # noqa: BLE001
        err(f"search preferences: {exc}"); rc = 1

    ok("No account, API key, paid provider, or browser automation required")

    if BOARD_FILE.exists():
        try:
            from jobhunt import store
            data = store.load(BOARD_FILE)
            opps = data.get("opportunities", [])
            n = len(opps)
            gen = data.get("generated_at", "?")
            ok(f"Board data present — {n} postings, generated {gen}")
            apps = [o.get("application") for o in opps if isinstance(o.get("application"), dict)]
            extractable = sum(1 for a in apps if a.get("extractable"))
            with_prompts = sum(1 for a in apps if a.get("prompts"))
            if apps:
                say(f"     Application forms: {extractable}/{n} previewable, {with_prompts} with free-form prompts")
            errors = (data.get("meta") or {}).get("errors") or []
            if errors:
                warn(f"{len(errors)} companies failed last refresh:")
                for e in errors[:15]:
                    say(f"       - {e.get('company')}: {e.get('result')} {e.get('error') or ''}".rstrip())
            warnings = (data.get("meta") or {}).get("warnings") or []
            if warnings:
                warn(f"{len(warnings)} refresh warnings:")
                for item in warnings[:15]:
                    say(f"       - {item.get('company')}: {item.get('warning') or ''}".rstrip())
            recovery = (data.get("meta") or {}).get("recovery_warnings") or []
            if recovery:
                warn("Local board recovery:")
                for item in recovery[:15]:
                    say(f"       - {item}")
        except Exception as exc:  # noqa: BLE001
            err(f"Board data unreadable: {exc}"); rc = 1
    else:
        warn("No board data yet — run `./jobs refresh`")

    say()
    if rc == 0:
        head("Everything looks good. ✓  Run `./jobs start`.")
    else:
        head("Some checks failed (✗ above). Fix those, then re-run `./jobs doctor`.")
    say()
    return rc


def cmd_linkedin(args) -> int:
    """Print ordinary LinkedIn search links. Never logs in or automates LinkedIn."""
    from jobhunt import linkedin
    company = args.company
    if company.strip().casefold() in {"setup", "login"}:
        err(
            "There is no LinkedIn setup or login here. The optional warm path is "
            "links-only: add your own Connections CSV or open a search link yourself."
        )
        return 2
    links = linkedin.linkedin_links(company)
    head(f"LinkedIn warm path — {company}")
    say("Optional links to open yourself. This tool never logs in, scrapes, or clicks:")
    ok(f"Your connections here:  {links['connections']}")
    ok(f"Recruiters / TA here:   {links['recruiters']}")
    return 0


def cmd_flag(args, *, dismissed=None, read=None) -> int:
    from jobhunt import store
    # A dismissal is durable: record it in the dismissed-id set so it's excluded
    # from every future refresh, then drop it from the current board immediately.
    if dismissed is not None:
        if dismissed and not _board_contains_id(args.id):
            err(f"No posting with id {args.id}")
            return 1
        store.set_dismissed(args.id, dismissed=dismissed)
        if dismissed:
            data = store.load()
            kept = [o for o in data.get("opportunities", []) if o.get("id") != args.id]
            store.save([store.Opportunity.from_dict(o) for o in kept],
                       meta=data.get("meta"))
            ok(f"Dismissed {args.id} — hidden now and on every future refresh")
            _build_board()
            return 0
    found = store.set_flag(args.id, read=read, dismissed=dismissed)
    if found:
        ok(f"Updated {args.id}")
        _build_board()
        return 0
    err(f"No posting with id {args.id}")
    return 1


def _board_contains_id(opp_id: str) -> bool:
    """Return whether an id is present in the current local board.

    Dismissal is a durable mutation.  Refuse to create a tombstone for an id
    that is not currently on the board, otherwise a typo can hide a future
    posting forever.  A missing/corrupt board is treated as empty here; the
    caller reports the same actionable "No posting" error as a missing id.
    """
    from jobhunt import store

    try:
        data = store.load()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    opportunities = data.get("opportunities", [])
    if not isinstance(opportunities, list):
        return False
    return any(isinstance(item, dict) and item.get("id") == opp_id
               for item in opportunities)


# --- helpers ---------------------------------------------------------------

def _load_company_catalog() -> list[dict]:
    """Load and shape-check the public company catalog before using it.

    A malformed catalog is an installation/configuration error, not a reason to
    let a refresh thread throw an AttributeError halfway through a run.  Keep
    this validation in one place so ``doctor``, ``configure``, and ``refresh``
    all give the same actionable message.
    """
    path = CONFIG / "companies.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    companies = data.get("companies", data) if isinstance(data, dict) else data
    if not isinstance(companies, list) or not companies:
        raise ValueError("companies.json must contain a non-empty companies[] list")

    invalid = []
    for index, company in enumerate(companies, start=1):
        if not isinstance(company, dict):
            invalid.append(f"entry {index} must be an object")
            continue
        missing = [
            field for field in ("name", "slug", "ats")
            if not isinstance(company.get(field), str) or not company[field].strip()
        ]
        if missing:
            invalid.append(f"entry {index} needs non-empty {', '.join(missing)}")
    if invalid:
        raise ValueError("companies.json has invalid entries: " + "; ".join(invalid[:5]))
    return companies


def _load_companies() -> list[dict]:
    companies = _load_company_catalog()
    try:
        requested = _load_profile_raw().get("companies", [])
    except (OSError, ValueError):
        requested = []
    if requested:
        requested_by_key = {
            str(value).strip().lower(): str(value).strip()
            for value in requested
            if str(value).strip()
        }
        wanted = set(requested_by_key)
        selected = [
            company for company in companies
            if str(company.get("name", "")).lower() in wanted
            or str(company.get("slug", "")).lower() in wanted
        ]
        matched = {
            str(company.get("name", "")).lower() for company in selected
        } | {
            str(company.get("slug", "")).lower() for company in selected
        }
        missing = sorted(wanted - matched)
        if missing:
            display_names = [requested_by_key[key] for key in missing]
            raise ValueError(
                f"unknown companies in search preferences: {', '.join(display_names)}"
            )
        if not selected:
            raise ValueError("search preferences selected no known companies")
        return selected
    return companies


def _validate_company_preferences(values: list[str]) -> None:
    """Reject unknown shortlist values before writing private config."""
    catalog = _load_company_catalog()
    known = {
        key
        for company in catalog
        for key in (company["name"].strip().casefold(), company["slug"].strip().casefold())
    }
    missing = [value for value in values if value.strip().casefold() not in known]
    if missing:
        raise ValueError("unknown companies: " + ", ".join(missing))


def _profile_path() -> Path:
    """Use private per-install preferences; fall back to the public starter."""
    return SEARCH_LOCAL if SEARCH_LOCAL.exists() else SEARCH_EXAMPLE


def _ensure_search_local() -> Path:
    if not SEARCH_LOCAL.exists():
        shutil.copy(SEARCH_EXAMPLE, SEARCH_LOCAL)
    return SEARCH_LOCAL


def _load_profile_raw() -> dict:
    from jobhunt.filter import validate_search_config
    return validate_search_config(
        json.loads(_profile_path().read_text(encoding="utf-8"))
    )


def _profile_storage_key(config: dict) -> str:
    """Return a stable, non-personal namespace for browser triage state."""
    fields = {
        name: config.get(name)
        for name in ("title_keywords", "title_exclude", "locations",
                     "remote_ok", "max_age_days", "companies")
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return "p-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _build_board(payload: dict | None = None) -> None:
    from jobhunt import board, store
    from jobhunt.model import utc_now
    if payload is None:
        if BOARD_FILE.exists():
            try:
                payload = store.load(BOARD_FILE)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                warn(f"Board data is unreadable ({exc}); showing the starter board. Run `./jobs refresh` to rebuild it.")
                if SAMPLE_FILE.exists():
                    payload = store.load(SAMPLE_FILE)
                else:
                    payload = {"version": 1, "generated_at": utc_now(), "meta": {}, "opportunities": []}
        else:
            payload = {"version": 1, "generated_at": utc_now(), "meta": {}, "opportunities": []}
    # Optional personal context comes only from the private per-install file.
    try:
        config = _load_profile_raw()
        payload.setdefault("meta", {})["search_profile_key"] = _profile_storage_key(config)
        bio = (config.get("referral_bio") or "").strip()
        if bio:
            payload["meta"]["referral_bio"] = bio
    except (OSError, ValueError):
        pass
    board.render(payload)


def _ensure_live_data() -> None:
    """First-run self-heal: if there's no live board data yet (fresh clone, or someone
    just ran `open`/`start` before `refresh`), pull today's postings now so the board is
    never blank. Falls back to the bundled sample if offline. This is why a fresh clone
    'just works' — the live data is gitignored (regenerated per machine), not missing."""
    if BOARD_FILE.exists():
        return
    if not _need_requests():
        if SAMPLE_FILE.exists():
            shutil.copy(SAMPLE_FILE, BOARD_FILE)
            warn("Showing sample postings (Python deps missing). Run `./jobs setup` then `./jobs refresh`.")
        return
    head("First run — pulling today's job postings (one-time, ~30s)…")
    import types
    try:
        cmd_refresh(types.SimpleNamespace(no_cache=False, no_forms=False, verbose=False))
    except Exception as exc:  # noqa: BLE001 — never let first-run fetch crash the open
        warn(f"Live fetch failed ({exc}); showing sample postings. Try `./jobs refresh` when online.")
    if not BOARD_FILE.exists() and SAMPLE_FILE.exists():
        shutil.copy(SAMPLE_FILE, BOARD_FILE)


# --- private per-install search preferences --------------------------------

def _csv_values(raw: str) -> list[str]:
    return [value.strip() for value in raw.split(",") if value.strip()]


def _port_arg(raw: str) -> int:
    """Parse a TCP port into the range accepted by the standard library."""
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be a whole number between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _write_search_config(config: dict) -> None:
    path = _ensure_search_local()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def cmd_configure(args) -> int:
    """Show or update the private, per-install search preferences."""
    if getattr(args, "reset", False):
        shutil.copy(SEARCH_EXAMPLE, SEARCH_LOCAL)
        ok("Reset private search preferences to the public starter")

    try:
        from jobhunt.filter import validate_search_config
        config = validate_search_config(
            json.loads(_ensure_search_local().read_text(encoding="utf-8"))
        )
    except (OSError, ValueError) as exc:
        err(f"search preferences: {exc}")
        say("  Fix config/search.local.json or run `./jobs configure --reset`.")
        return 1
    updates = False
    mapping = {
        "titles": "title_keywords",
        "exclude": "title_exclude",
        "locations": "locations",
    }
    for arg_name, field_name in mapping.items():
        raw = getattr(args, arg_name, None)
        if raw is not None:
            normalized = raw.strip().lower()
            if arg_name == "titles" and normalized in {"", "all", "any"}:
                values = []
            elif arg_name == "exclude" and normalized in {"", "all", "any", "none", "no"}:
                values = []
            elif arg_name == "locations" and normalized in {"all", "any"}:
                values = ["all"]
            else:
                values = _csv_values(raw)
            if not values and arg_name not in {"titles", "exclude"}:
                err(f"--{arg_name} needs at least one comma-separated value")
                return 1
            config[field_name] = values
            updates = True

    company_raw = getattr(args, "companies", None)
    if company_raw is not None:
        config["companies"] = [] if company_raw.strip().lower() == "all" else _csv_values(company_raw)
        if company_raw.strip().lower() != "all" and not config["companies"]:
            err("--companies needs comma-separated names or `all`")
            return 1
        if config["companies"]:
            try:
                _validate_company_preferences(config["companies"])
            except (OSError, ValueError) as exc:
                err(f"--companies: {exc}")
                return 1
        updates = True

    if getattr(args, "remote", None) is not None:
        config["remote_ok"] = args.remote == "yes"
        updates = True
    if getattr(args, "max_age", None) is not None:
        if args.max_age < 0:
            err("--max-age must be zero or greater")
            return 1
        config["max_age_days"] = args.max_age
        updates = True
    if getattr(args, "bio", None) is not None:
        config["referral_bio"] = args.bio.strip()
        updates = True

    interactive = getattr(args, "interactive", False) or (
        not updates and not getattr(args, "reset", False)
    )
    if not updates and not getattr(args, "reset", False) and interactive:
        ask = getattr(args, "input_fn", input)
        say("Press Enter to keep the current value.")
        titles = ask(
            "Job titles or keywords (type `all` for every kind of job) "
            f"[{', '.join(config.get('title_keywords', [])[:5]) or 'all'}]: "
        ).strip()
        locations = ask(
            "Locations (type `all` for every location) "
            f"[{', '.join(config.get('locations', [])) or 'all'}]: "
        ).strip()
        companies = ask(
            "Companies (comma-separated, or all) "
            f"[{', '.join(config.get('companies', [])) or 'all'}]: "
        ).strip()
        remote = ask(f"Include remote roles? [{'yes' if config.get('remote_ok', True) else 'no'}]: ").strip().lower()
        age = ask(f"Maximum posting age in days [{config.get('max_age_days', 30)}]: ").strip()
        bio = ask(f"Short professional summary [{config.get('referral_bio', '')}]: ").strip()
        if titles:
            config["title_keywords"] = [] if titles.lower() in {"all", "any"} else _csv_values(titles)
        if locations:
            config["locations"] = ["all"] if locations.lower() in {"all", "any"} else _csv_values(locations)
        if companies:
            config["companies"] = (
                [] if companies.lower() == "all" else _csv_values(companies)
            )
            if config["companies"]:
                try:
                    _validate_company_preferences(config["companies"])
                except (OSError, ValueError) as exc:
                    err(f"Companies: {exc}")
                    return 1
        if remote in {"yes", "y", "no", "n"}:
            config["remote_ok"] = remote in {"yes", "y"}
        if age:
            try:
                parsed_age = int(age)
            except ValueError:
                err("Maximum age must be a whole number")
                return 1
            if parsed_age < 0:
                err("Maximum age must be zero or greater")
                return 1
            config["max_age_days"] = parsed_age
        if bio:
            config["referral_bio"] = bio
        updates = any((titles, locations, companies, remote, age, bio))

    if updates:
        _write_search_config(config)
        ok("Saved private search preferences")

    head("Your search")
    say(f"  File:       {SEARCH_LOCAL.relative_to(ROOT)} (private; never committed)")
    keywords = config.get("title_keywords", [])
    say(f"  Keywords:   {', '.join(keywords[:8]) if keywords else 'all job titles'}")
    locations = config.get("locations", [])
    say(f"  Locations:  {', '.join(locations) if locations else 'all locations'}")
    say(f"  Companies:  {', '.join(config.get('companies', [])) or 'all configured companies'}")
    say(f"  Remote:     {'included' if config.get('remote_ok', True) else 'excluded'}")
    say(f"  Max age:    {config.get('max_age_days', 30)} days")
    say("\nRun `./jobs start`, then use Refresh in the board.")
    return 0


def cmd_serve(args) -> int:
    """Open and serve the board on this computer only."""
    import http.server
    import socketserver
    import functools
    import webbrowser

    _ensure_live_data()
    _build_board()
    if not BOARD_HTML.exists():
        err("No board to serve. Run `./jobs refresh` first.")
        return 1

    host = "127.0.0.1"

    class BoardHandler(http.server.SimpleHTTPRequestHandler):
        """Static board server + a tiny stdlib-only refresh API."""

        def _is_local_request(self):
            host_header = self.headers.get("Host", "")
            host_name = host_header.rsplit(":", 1)[0].strip("[]").lower()
            if host_name not in {"localhost", "127.0.0.1", "::1"}:
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return origin in {
                f"http://localhost:{args.port}",
                f"http://127.0.0.1:{args.port}",
                f"http://[::1]:{args.port}",
            }

        def _send_json(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 — http.server naming
            if not self._is_local_request():
                self._send_json(403, {"error": "local_requests_only"})
                return
            if self.path == "/api/refresh":
                if _start_refresh_async():
                    self._send_json(202, {"status": "started"})
                else:
                    self._send_json(200, {"status": "already_running"})
                return
            if self.path == "/api/dismiss":
                # Durable dismiss/undismiss: persist to the dismissed-id set so the
                # role stays hidden across every future refresh.
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if length > 16 * 1024:
                        self._send_json(413, {"error": "request_too_large"})
                        return
                    if "application/json" not in self.headers.get("Content-Type", ""):
                        self._send_json(415, {"error": "json_required"})
                        return
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        self._send_json(400, {"error": "bad_request"})
                        return
                    raw_id = body.get("id", "")
                    raw_dismissed = body.get("dismissed", True)
                    if not isinstance(raw_id, str) or type(raw_dismissed) is not bool:
                        self._send_json(400, {"error": "bad_request"})
                        return
                    opp_id = raw_id.strip()
                    dismissed = raw_dismissed
                except (ValueError, TypeError):
                    self._send_json(400, {"error": "bad_request"})
                    return
                if not opp_id:
                    self._send_json(400, {"error": "missing_id"})
                    return
                from jobhunt import store
                if dismissed and not _board_contains_id(opp_id):
                    self._send_json(404, {"error": "posting_not_found"})
                    return
                store.set_dismissed(opp_id, dismissed=dismissed)
                # Keep the rendered board in sync with the durable tombstone.
                # The browser overlay hides the row immediately, but another
                # browser (or a reopened static board) reads jobs.local.json;
                # leaving its flag stale makes a dismissed role reappear there.
                board_updated = store.set_flag(opp_id, dismissed=dismissed)
                if board_updated:
                    _build_board()
                self._send_json(200, {
                    "status": "ok",
                    "id": opp_id,
                    "dismissed": dismissed,
                    "board_updated": board_updated,
                })
                return
            self._send_json(404, {"error": "not_found"})

        def do_GET(self):  # noqa: N802 — http.server naming
            if not self._is_local_request():
                self.send_error(403, "Local requests only")
                return
            if self.path == "/api/refresh/status":
                with _REFRESH_LOCK:
                    snapshot = dict(_REFRESH_STATE)
                self._send_json(200, snapshot)
                return
            return super().do_GET()

        def log_message(self, *_a):  # silence default request logging noise
            pass

    class BoardServer(socketserver.ThreadingTCPServer):
        # Rebind immediately after a Ctrl+C restart (sockets in TIME_WAIT
        # otherwise fail the bind for up to a minute).
        allow_reuse_address = True
        daemon_threads = True

    Handler = functools.partial(BoardHandler, directory=str(BOARD_HTML.parent))
    try:
        httpd = BoardServer((host, args.port), Handler)
    except OSError as exc:
        err(f"Could not bind port {args.port}: {exc}. Try `./jobs start --port 8800`.")
        return 1

    url = f"http://localhost:{args.port}/"
    head("Serving your board")
    ok(f"On this computer: {url}")
    if not getattr(args, "no_open", False):
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    say("\nLeave this running. Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        say("\nStopped.")
    finally:
        httpd.server_close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jobs", description="Job Hunt Board — find real jobs from official ATS feeds")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("setup", help="one-time setup")

    r = sub.add_parser("refresh", help="pull live job postings")
    r.add_argument("--no-cache", action="store_true", help="ignore the on-disk cache (force live fetch)")
    r.add_argument("--no-forms", action="store_true", help="skip fetching each posting's application form")
    r.add_argument("-v", "--verbose", action="store_true", help="show per-company errors live")

    sub.add_parser("board", help="rebuild board HTML (no network)")
    op = sub.add_parser("open", help="serve + open the board with one-click refresh")
    op.add_argument("--port", type=_port_arg, default=8787, help="port to serve on (default 8787)")
    op.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    sub.add_parser("doctor", help="health check")

    cf = sub.add_parser("configure", aliases=["config"], help="show or update private search preferences")
    cf.add_argument("--interactive", action="store_true", help="answer a few plain-English setup questions")
    cf.add_argument("--titles", help="comma-separated job-title keywords, or `all`")
    cf.add_argument("--exclude", help="comma-separated title words to exclude, or `none` to clear")
    cf.add_argument("--locations", help="comma-separated locations")
    cf.add_argument("--companies", help="comma-separated company names/slugs, or `all`")
    cf.add_argument("--remote", choices=("yes", "no"), help="include remote roles")
    cf.add_argument("--max-age", type=int, help="oldest posting to keep, in days (0 disables)")
    cf.add_argument("--bio", help="short optional professional summary used in referral drafts")
    cf.add_argument("--reset", action="store_true", help="restore the public starter search")

    for command in ("start", "serve"):
        sv = sub.add_parser(command, help="open the local board with one-click refresh")
        sv.add_argument("--port", type=_port_arg, default=8787, help="port to serve on (default 8787)")
        sv.add_argument("--no-open", action="store_true", help="do not open a browser automatically")

    rd = sub.add_parser("read", help="mark a posting read"); rd.add_argument("id")
    dm = sub.add_parser("dismiss", help="hide a posting"); dm.add_argument("id")

    lk = sub.add_parser("linkedin", help="print optional, ordinary LinkedIn search links")
    lk.add_argument("company", help="company name")

    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return 0

    dispatch = {
        "setup": cmd_setup, "refresh": cmd_refresh, "board": cmd_board,
        "open": cmd_serve, "doctor": cmd_doctor,
        "start": cmd_serve, "serve": cmd_serve,
        "configure": cmd_configure, "config": cmd_configure,
        "linkedin": cmd_linkedin,
        "read": lambda a: cmd_flag(a, read=True),
        "dismiss": lambda a: cmd_flag(a, dismissed=True),
    }
    try:
        return dispatch[args.cmd](args)
    except FileNotFoundError as exc:
        err(f"Missing file: {exc}")
        return 1
    except ValueError as exc:
        err(f"Invalid search preferences: {exc}")
        say("  Fix config/search.local.json or run `./jobs configure --reset`.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
