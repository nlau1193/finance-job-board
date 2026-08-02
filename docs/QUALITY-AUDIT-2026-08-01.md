# Quality audit — 2026-08-01

This is a public, source-backed review of the any-role Job Hunt Board. It is
not a promise that every edge case is solved. The release rule is simple: fix
reproduced, high-impact failures; leave lower-risk work explicit so the next
maintainer does not rediscover it.

## Fixed in this pass

1. **Keyboard pagination dropped the active row after the first 100 roles.**
   Arrow navigation now expands the rendered page before selecting the next
   matching role.
2. **Job options had no listbox parent.** The list now exposes `listbox` and
   `aria-activedescendant` semantics.
3. **Refresh progress was visual-only.** The bar now exposes progressbar values
   and an honest text value for indeterminate phases.
4. **A partially resolved feed looked green and live.** Feed warnings/errors
   now give the status pill an amber partial state.
5. **`./jobs open` opened a static file whose Refresh button could not work.**
   `open` now uses the local server path, like `start`, with `--port` and
   `--no-open` options.
6. **A `null` or array in localStorage crashed the board.** Invalid overlay
   JSON now falls back to an empty state and malformed per-job entries are
   ignored.
7. **The mobile detail dialog remained exposed to assistive technology while
   closed.** Closed mobile detail is now `aria-hidden` and inert; desktop
   detail remains available.
8. **The whole job description was an aria-live region.** Detail content is no
   longer re-announced wholesale on every selection.

## Reproduced or source-backed follow-up inventory

9. Workday can cap a broad tenant at 1,000 newest roles; the receipt carries a
   warning, but a future UI could make the cap more prominent.
10. A dismiss request is intentionally fire-and-forget in the browser; a
    future version should surface a failed persistence response without
    undoing the local hide.
11. Two tabs can race a refresh against a dismiss write; server-side state is
    durable, but conflict resolution is not yet shown in the UI.
12. The local overlay has no age-based pruning or per-user namespace; stale
    IDs are harmless but can accumulate.
13. Search currently indexes title, company, location, and team, not the full
    sanitized description.
14. The “Quick” application pill is an ATS-form preview, not a guarantee that
    an employer will ask only for a resume.
15. Clicking Apply marks a role read before the external page confirms it
    opened; that is a deliberate triage convention, not application proof.
16. The HTML sanitizer must remain defensive around malformed attributes and
    truncation; its failure mode is a shortened description, not a refresh
    crash.
17. The local server intentionally has no account/auth layer; it binds to
    loopback and rejects non-local origins, but it is not a multi-user service.
18. The public README and launcher use Python 3.10+, while a few historical
    comments still mention older Python versions.
19. State is keyed by the normalized posting id; an ATS changing its id will
    look like a new role and cannot be reconciled automatically.
20. No network-backed end-to-end refresh is part of the default CI run; live
    ATS availability is checked by the opt-in universe verifier.

Items 9–20 are bounded follow-up work, not hidden acceptance criteria. The
public board stays local-first, free, read-only with respect to employers, and
safe to run without LinkedIn, an account, or an API key.

## Finish receipts

- Taste lint: **0 errors, 0 warnings, 0 suggestions** across `docs/` and
  `README.md`.
- Impeccable detector: two intentional warnings remain. The description
  blockquote uses a thin semantic rule to mark quoted source text, and the
  refresh bar animates `width` because its progress value is the thing being
  communicated. Both are visible, bounded choices rather than accidental
  generated UI.
- Browser proof: the clean fictional E2E run passed and captured desktop,
  mobile detail, loading, and no-server recovery states in `/tmp/jobboard-e2e`.
- Contrast spot-check against the rendered paper palette: body 15.39:1,
  status 7.32:1, refresh action 5.45:1, row text 16.19:1, and note text
  5.46:1. These are all above the 4.5:1 text target.
- Contributor hygiene: the coverage database produced by `npm test` is now
  ignored, so a local quality run cannot accidentally enter the public history.

## Takeover pass — 2026-08-02

This follow-up pass re-read the public checkout and tested the any-role workflow
from a clean local install. It keeps the board useful for any job search while
preserving the local-first, no-paid-tool default.

### Fixed in the takeover pass

21. **A profile switch could reuse another profile's local board.** Board and
    overlay storage now include a stable hash of the active profile fields, so
    Nicole's sample settings cannot silently become another install's state.
22. **A malformed refresh cache could poison a later run.** Cache timestamps are
    validated as finite numbers, and bad board JSON falls back to a safe empty
    state with a visible refresh path.
23. **The verifier could report a feed as resolved without an actionable link.**
    Unknown ATS values, per-feed failures, and search-page URLs now fail closed
    with a receipt instead of becoming fake Apply buttons.
24. **Host-boundary URL matching was too permissive.** Actionable-link checks
    now require the official ATS host boundary rather than accepting a lookalike
    hostname.
25. **Department search was missing from the visible filter.** Search now covers
    title, company, location, department, and team while remaining local.
26. **The empty mobile detail state had a dangling accessible name.** Both the
    empty and no-match states now own the `detail-title` target referenced by
    `aria-labelledby`, and the browser smoke test checks that target exists.

### Release boundary and remaining work

The source checkout is now green, but the current GitHub history still contains
an earlier, later-deleted `.env.example` and optional LinkedIn/API-key tooling.
Deletion in a later commit is not enough for a public repository: the release
must be republished from a fresh history so secrets and paid-provider names are
not recoverable from ordinary history browsing. That history rewrite is the
remaining publishing action; no credentials or private connection data are part
of the new tree.

The LinkedIn arm remains opt-in and read-only for installations that explicitly
choose it. It is not required for the core board, refresh, or Apply links. Lower
risk follow-ups remain the same: fuller description search, visible refresh-feed
caps, two-tab conflict messaging, overlay pruning, and a live ATS check outside
the fixture-backed CI run.

### Fresh proof

- `./.venv/bin/python -m pytest -q`: **161 passed**.
- `JOBBOARD_E2E_PORT=8899 JOBBOARD_E2E_ARTIFACT_DIR=/tmp/jobboard-rigorous-e2e npm run test:e2e`: **green**, including profile isolation, actionable Apply links, persistence, and mobile detail naming.
- Coverage run: **67.28%** statement coverage.
- `git diff --check`: clean.

### Additional bounded candidates (27–31)

27. The ATS cache is TTL-based rather than validator-based; a refresh after the
    TTL re-downloads an unchanged feed instead of using an ETag/Last-Modified
    check.
28. Cache files have no age/size pruning, so a long-lived installation can
    accumulate old endpoint responses even though the board itself remains
    correct.
29. Detail hydration fans out one request per eligible posting. Large any-role
    refreshes can therefore hit an employer's rate limit or spend a long time
    enriching roles the user will never open; the receipt reports failures but
    does not yet offer a user-tunable enrichment budget.
30. The company catalog has no explicit duplicate-slug/duplicate-ATS identity
    report before network work begins; a hand-edited config can produce
    duplicate feeds and confusing counts.
31. The local server is intentionally single-user and loopback-oriented, but
    its refresh API has no authenticated session or CSRF token. It should stay
    documented as a trusted-local tool, not be exposed on an untrusted LAN.
