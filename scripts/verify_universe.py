#!/usr/bin/env python3
"""Verify the company universe: live-curl every slug on its ATS, report resolve-rate.

This is the honesty gate for config/companies.json. A company "resolves" when
its ATS board API returns at least one posting. A company has a match when at
least one posting matches the local search preferences (or public starter).
Broken entries (wrong slug/ATS) must be fixed or dropped — they never ship.

Exit non-zero when the resolve rate or matching coverage is below threshold, so
this can gate CI / pre-commit.

  python3 scripts/verify_universe.py                 # human table
  python3 scripts/verify_universe.py --json          # machine output
  python3 scripts/verify_universe.py --min-resolve 0.9 --min-matching-companies 30
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobhunt.discover import discover_company  # noqa: E402
from jobhunt.filter import Profile, matches  # noqa: E402
from jobhunt.ats._http import new_session  # noqa: E402

CONFIG = ROOT / "config"


def _check(company: dict, profile: Profile, session) -> dict:
    ats = (company.get("ats") or "").lower()
    name = company.get("name", company.get("slug"))
    if ats not in {"greenhouse", "ashby", "lever", "workday"}:
        return {"name": name, "slug": company.get("slug"), "ats": ats,
                "resolved": False, "raw": 0, "actionable": 0, "matching": 0,
                "result": "unknown_ats"}
    try:
        opps, receipt = discover_company(
            company, session=session, use_cache=False,
            search_terms=profile.title_keywords,
        )
    except Exception as exc:  # noqa: BLE001 — one malformed feed never kills the table
        return {"name": name, "slug": company.get("slug"), "ats": ats,
                "resolved": False, "raw": 0, "actionable": 0, "matching": 0,
                "result": "error", "error": str(exc)}
    raw = receipt.get("raw")
    if not isinstance(raw, int):
        raw = len(opps) + int(receipt.get("dropped_non_actionable", 0) or 0)
    matching = sum(1 for o in opps if matches(o, profile))
    return {
        "name": name, "slug": company.get("slug"), "ats": ats,
        "resolved": receipt.get("result") == "ok" and len(opps) > 0,
        "raw": raw, "actionable": len(opps), "matching": matching,
        "result": receipt.get("result"),
        "error": receipt.get("error"),
        "warning": receipt.get("warning"),
        "truncated": bool(receipt.get("truncated")),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify the company universe resolves on its ATS.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-resolve", type=float, default=0.90)
    ap.add_argument("--min-matching-companies", type=int, default=30,
                    help="minimum companies with at least one matching role")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    data = json.loads((CONFIG / "companies.json").read_text(encoding="utf-8"))
    companies = data.get("companies", data) if isinstance(data, dict) else data
    local = CONFIG / "search.local.json"
    profile = Profile.load(local if local.exists() else CONFIG / "search.example.json")
    session = new_session()

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_check, c, profile, session) for c in companies]
        for fut in as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: (not r["resolved"], -r["matching"], r["name"].lower()))

    total = len(results)
    resolved = sum(1 for r in results if r["resolved"])
    matching_companies = sum(1 for r in results if r["matching"] > 0)
    matching_roles = sum(r["matching"] for r in results)
    resolve_rate = resolved / total if total else 0.0
    dead = [r for r in results if not r["resolved"]]
    warnings = [{"name": r["name"], "warning": r["warning"]}
                for r in results if r.get("warning")]

    pass_resolve = resolve_rate >= args.min_resolve
    pass_matching = matching_companies >= args.min_matching_companies
    passed = pass_resolve and pass_matching

    summary = {
        "total": total, "resolved": resolved, "resolve_rate": round(resolve_rate, 4),
        "matching_companies": matching_companies, "matching_roles": matching_roles,
        "dead": [{"name": r["name"], "slug": r["slug"], "ats": r["ats"],
                  "result": r["result"], "error": r.get("error")} for r in dead],
        "warnings": warnings,
        "thresholds": {"min_resolve": args.min_resolve,
                       "min_matching_companies": args.min_matching_companies},
        "pass": passed,
    }

    if args.json:
        print(json.dumps({"summary": summary, "results": results}, indent=2))
        return 0 if passed else 1

    print(f"\n  Company universe verification — {total} companies\n")
    print(f"  {'COMPANY':<24} {'ATS':<11} {'JOBS':>5} {'MATCH':>5}  STATUS")
    print("  " + "-" * 60)
    for r in results:
        status = "ok" if r["resolved"] else (r["result"] or "fail")
        flag = " " if r["resolved"] else "✗"
        print(f"  {flag} {r['name'][:22]:<22} {r['ats']:<11} {r['raw']:>5} {r['matching']:>5}  {status}")
    print("  " + "-" * 60)
    print(f"  Resolve rate:      {resolved}/{total} = {resolve_rate:.0%}   "
          f"({'PASS' if pass_resolve else 'FAIL'} ≥ {args.min_resolve:.0%})")
    print(f"  Matching coverage: {matching_companies} companies, {matching_roles} roles   "
          f"({'PASS' if pass_matching else 'FAIL'} ≥ {args.min_matching_companies} companies)")
    if dead:
        print(f"\n  {len(dead)} did NOT resolve (fix the slug/ats or drop them):")
        for r in dead:
            print(f"     - {r['name']} ({r['ats']}/{r['slug']}): {r['result']} {r.get('error') or ''}".rstrip())
    if warnings:
        print("\n  Warnings:")
        for item in warnings:
            print(f"     - {item['name']}: {item['warning']}")
    print(f"\n  RESULT: {'PASS ✓' if passed else 'FAIL ✗'}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
