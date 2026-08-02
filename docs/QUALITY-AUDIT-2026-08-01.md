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

The current branch is a clean public release and the checked-in tree
contains no `.env` or `.env.example`, API-key provider tooling, live board data, or private
connections. Git hosting still retains unreachable historical objects that can
be fetched by an old SHA; one such object exposes the earlier Leo author email
and removed optional tooling. This is not a current-tree credential leak, but it
means a strict Nicole-only history claim is not proven. Repository rename or
delete/recreate is an owner-controlled privacy decision, not a source-code gate.

Historical note: that earlier review described an opt-in LinkedIn automation arm.
The current public cut deleted that automation. It keeps only an optional local
Connections CSV match and ordinary LinkedIn search links; neither is required
for the core board, refresh, or Apply links. Lower-risk follow-ups remain the
same: fuller description search, visible refresh-feed caps, two-tab conflict
messaging, overlay pruning, and a live ATS check outside the fixture-backed CI
run.

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

### Focused resilience fixes — 2026-08-02

32. **One malformed ATS row could poison an otherwise healthy feed.** The
    Greenhouse, Ashby, Lever, and Workday adapters now validate nested shapes,
    skip only the malformed posting, and retain a `dropped_malformed` receipt;
    refresh metadata surfaces that warning while keeping good rows.
33. **Corrupt local state could crash the board renderer.** Store recovery now
    repairs malformed application prompts/gates, fit labels, and warm-path
    people, drops unusable rows, and records a recovery warning instead of
    handing unsafe shapes to the browser.
34. **A poisoned cache timestamp could stay fresh forever.** Cache reads now
    reject NaN, infinity, future, and absurdly large timestamps before using a
    cached payload.

The remaining any-job follow-ups are intentionally visible: Workday broad
refreshes can be request-heavy, description search is still shallow, overlay
state is single-profile/local, two-tab conflict messaging is not yet shown,
and live ATS availability remains an opt-in verifier rather than default CI.

## Final takeover receipt — 2026-08-02

This receipt supersedes the earlier proof counts above. It is the bounded final
pass for the public any-job board, not a claim that every optional enhancement
is complete.

- `./.venv/bin/python -m pytest -q`: **188 passed**.
- `./jobs doctor`: green; 241 configured official ATS companies, no account,
  API key, paid provider, or browser automation required.
- `JOBBOARD_E2E_PORT=8903 JOBBOARD_E2E_ARTIFACT_DIR=/tmp/jobboard-final-e2e
  npm run test:e2e`: green. Captured desktop, mobile detail, loading, and
  refresh-without-server states in `/tmp/jobboard-final-e2e`.
- Any-job matrix: ten independent technology profiles plus unrestricted
  all-role and department-driven cases remain covered by the committed
  profile-matrix tests.
- Taste lint: **0 errors, 0 warnings, 0 suggestions** across `docs/` and
  `README.md`.
- The final fixes harden malformed ATS rows, malformed nested local state,
  poisoned cache timestamps, punctuation-aware profile filtering, actionable
  Apply links, broad-feed empty-result preservation, and profile-scoped browser
  state. Good rows survive a bad row with a visible refresh warning.
- `git diff --check`: clean. Impeccable warnings were reviewed and retained
  only where the visual treatment is semantic or communicates progress.

The public product is called **Job Hunt Board** and accepts any job category.
The GitHub slug remains `finance-job-board` because this account has push but
not admin permission; renaming or history surgery is a separate owner/admin
decision. Historical unreachable objects also mean a strict Nicole-only history
claim is not proven by this source pass.

## Continuation audit — 2026-08-02

The next source pass challenged the two remaining high-leverage paths before
touching the release again. The Pivot SQL workbook handoff was cold-tested in a
fresh browser with local storage cleared: opening `fct_gl_transactions` produced
the sheet tab, active table, and data grid, so no Pivot change was justified.

35. **Broad Workday refreshes could spend hundreds of serial requests per
    tenant.** The adapter now caps an unrestricted any-role search at 10 pages
    (200 newest roles) and makes focused title searches share a 50-page (1,000
    role) budget. Each term gets a turn before any term gets a second page, and
    finished terms release their unused turns. Per-feed receipts include
    `pages`, `page_budget`, `terms_completed`, and an explicit truncation
    warning. Offsets reset for each search term, so the global budget cannot
    skip the first page of a later term. Multi-location detail GETs are skipped
    for unrestricted locations and capped at 200 per refresh otherwise, with
    fair tenant sampling and a visible warning for any remainder.
36. **Structured ATS Apply-link host matching accepted dotted lookalikes.** The
    Ashby, Lever, and Greenhouse path branches now parse and compare exact hosts
    while retaining the legitimate Workday tenant suffix boundary. This closes
    substring coincidence in those structured branches. Numeric `gh_jid` links
    on employer-owned custom domains remain a deliberate compatibility branch
    for Stripe-style feeds because this helper has no company-catalog context;
    the official feed adapter remains that branch's trust boundary.

Both fixes are local, deterministic, and covered by fixture-backed tests; no
paid provider, login, browser automation, LinkedIn data, or network-dependent
behavior was added.

### Continuation proof

- `./.venv/bin/python -m pytest -q`: **200 passed**, 74.92% statement coverage (the 60% gate remains
  green).
- `JOBBOARD_E2E_PORT=8907 JOBBOARD_E2E_ARTIFACT_DIR=/tmp/jobboard-continuation-e2e-cap
  npm run test:e2e`: **green**; the existing desktop, mobile, loading, and
  no-server checks still pass.
- `./jobs doctor`: **green**; 241 official ATS companies, no account/API key,
  paid provider, or browser automation required.
- `./jobs serve --port -1 --no-open` now fails with a friendly argparse message,
  without a traceback; `python3 -m py_compile jobhunt/model.py jobhunt/ats/workday.py` and
  `git diff --check`: **clean**.

## Current simplification receipt — 2026-08-02

This bounded pass kept the public any-job board's primary path intact and fixed
one reproduced state-consistency gap. A dismiss/undismiss API request now
updates the stored row and rebuilds the static board immediately, so a second
browser or a reopened board cannot keep showing the old dismissal state. The
E2E smoke test now proves both directions through the API and local board data.

### Fresh proof

- `npm test`: **201 passed**, 75.13% total coverage (the 60% gate remains green).
- `JOBBOARD_E2E_PORT=8911 JOBBOARD_E2E_ARTIFACT_DIR=/tmp/jobboard-simplify-e2e
  npm run test:e2e`: **green**; desktop, mobile detail, loading, and
  refresh-without-server artifacts were captured.
- `git diff --check`: clean.

The board remains category-neutral in product copy and configuration: users can
filter any role and the official-source, exact-Apply-link, local-first,
links-only LinkedIn boundaries remain unchanged. The GitHub slug is still
`finance-job-board` because the public account has push but not admin access;
the stale root deployment and any owner-only rename/transfer/history operation
remain outside this source-only pass.
