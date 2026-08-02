"""Profile filter: keep the roles that match a person's search.

Runs on structured ATS data (title, department, location) — no scraping
heuristics. Each install uses private `config/search.local.json` preferences,
created from the public `config/search.example.json` starter.

Location is the subtle part: "remote" must mean *remote-US*, not "remote
anywhere". A role tagged remote but located in Canada/UK/EMEA is not relevant
for an NYC-based US search, so a foreign-region guard takes precedence over the
remote flag.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import Opportunity

# Locations that are clearly outside the US. If a posting names one of these and
# does NOT also name a US anchor, it's filtered out even when tagged "remote".
_FOREIGN_MARKERS = (
    "canada", "ontario", "toronto", "vancouver", "montreal", "quebec",
    "united kingdom", "england", "scotland", "london", "ireland", "dublin",
    "emea", "apac", "latam", "germany", "berlin", "munich", "france", "paris",
    "netherlands", "amsterdam", "spain", "madrid", "barcelona", "portugal",
    "lisbon", "poland", "krakow", "romania", "bucharest", "italy", "sweden",
    "india", "bengaluru", "bangalore", "hyderabad", "gurgaon", "pune",
    "singapore", "australia", "sydney", "melbourne", "japan", "tokyo",
    "china", "shanghai", "hong kong", "korea", "seoul", "brazil", "mexico",
    "argentina", "colombia", "israel", "tel aviv", "dubai", "uae",
    "philippines", "manila", "vietnam", "indonesia", "new zealand", "nigeria",
    "south africa", "cape town", "johannesburg", "egypt", "kenya", "morocco",
    "costa rica", "chile", "peru", "malaysia", "thailand", "taiwan", "switzerland",
    "zurich", "austria", "vienna", "belgium", "denmark", "norway", "finland",
    "greece", "czech", "prague", "hungary", "budapest", "turkey", "istanbul",
)
# US-national / distributed postings. When a location names the country (or a
# "<State>, United States" / nationwide form) it's remote-or-distributed by
# nature — keep it even when the literal word "remote" is absent.
_US_NATIONAL = (
    "united states", "u.s.a", "u.s.", "usa", "us-based", "u.s.-based",
    "nationwide", "anywhere in the us", "anywhere in the u.s",
    "north america",
)
# US anchors that re-qualify an otherwise-foreign or remote posting.
_US_MARKERS = (
    "united states", "u.s.", "usa", "new york", "nyc", "remote - us",
    "remote, us", "remote (us", "us remote", "remote us", "americas",
    "san francisco", "seattle", "boston", "austin", "chicago", "denver",
    "atlanta", "los angeles", "washington", "miami", "dallas", "houston",
    "nationwide", "anywhere in the us",
)
# "new york" / "nyc" / the bare "ny" abbreviation (word-boundary matched below).
# Deliberately NOT bare "brooklyn"/"manhattan": those collide with Brooklyn, OH
# (a Cleveland suburb — KeyBank HQ), Manhattan, KS, and Manhattan Beach, CA. Real
# NYC listings always carry "NY"/"New York", so the borough names add only risk.
_NYC_MARKERS = ("new york", "nyc")
# Specifically-named non-NYC US *cities*. A posting that names one of these and
# does NOT say "remote" is an onsite role outside NYC — dropped, because the
# board is NYC + remote/distributed only (even when the text also names the
# country, e.g. "San Francisco, CA, USA").
_US_CITIES = (
    "san francisco", "sf bay", "bay area", "palo alto", "mountain view",
    "san jose", "san mateo", "redwood city", "sunnyvale", "menlo park",
    "seattle", "bellevue", "boston", "cambridge", "austin", "chicago",
    "denver", "boulder", "atlanta", "los angeles", "san diego", "irvine",
    "washington", "arlington", "miami", "dallas", "houston", "philadelphia",
    "phoenix", "portland", "nashville", "raleigh", "durham", "salt lake city",
    "minneapolis", "detroit", "pittsburgh", "columbus", "san bruno",
)

# US states — used to tell a *distributed* posting ("California, United States")
# from an *onsite* one ("New Brunswick, New Jersey, United States"). If, after
# removing country/state/qualifier words, a specific place-name token remains,
# the posting names a city and is onsite (see _names_specific_city).
_US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee",
    "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia",
)
_US_STATE_ABBR = (
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il",
    "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt",
    "ne", "nv", "nh", "nj", "nm", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
    "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
)
_COUNTRY_WORDS = (
    "united states of america", "united states", "u.s.a.", "u.s.a", "u.s.",
    "usa", "us-based", "u.s.-based", "north america", "namer", "americas",
    "america", "nationwide", "anywhere in the us", "anywhere in the u.s",
)
_LOC_QUALIFIERS = (
    "remote", "hybrid", "onsite", "on-site", "office", "offices", "headquarters",
    "hq", "work at home", "work from home", "home based", "home", "field",
    "various", "multiple", "locations", "location", "other", "flexible",
    "metro", "area", "greater", "based", "site", "us", "u.s", "distributed",
)


def _names_specific_city(loc: str) -> bool:
    """True if the location text names a specific city (not just a state/country).

    "California, United States" → False (state-level, distributed).
    "New Brunswick, New Jersey, United States" → True (a city → onsite).
    "USA, TX, Irving" / "Marietta, Ohio, USA" / "Wilmington NC USA" → True.
    Works by stripping country + state + qualifier words and checking whether a
    proper place-name token remains — so it catches cities not in _US_CITIES.
    """
    if any(c in loc for c in _US_CITIES):
        return True
    s = loc
    for w in _COUNTRY_WORDS:
        s = s.replace(w, " ")
    for st in _US_STATE_NAMES:
        s = re.sub(r"\b" + re.escape(st) + r"\b", " ", s)
    for ab in _US_STATE_ABBR + _LOC_QUALIFIERS:
        s = re.sub(r"\b" + re.escape(ab) + r"\b", " ", s)
    return bool(re.findall(r"[a-z]{3,}", s))


# Foreign markers, word-boundary matched (substring matching dropped real US
# locations: "ontario" hit Ontario-the-city in California, "paris" hit Paris, TX).
_FOREIGN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in sorted(_FOREIGN_MARKERS, key=len, reverse=True)) + r")\b"
)
# What must follow a foreign-named token for it to read as a US place instead:
# a full US state / country word ("Ontario, California", "Paris, Texas, USA")…
_US_STATE_AFTER_RE = re.compile(
    r"^[\s,\-–—/()]*(?:"
    + "|".join(re.escape(s) for s in sorted(
        _US_STATE_NAMES + ("united states", "u.s.a", "u.s", "usa"), key=len, reverse=True))
    + r")\b"
)
# …or a comma-attached state abbreviation ("London, KY"). Comma required so the
# ambiguous 2-letter set ("or", "in", "me") can't false-match prose.
_US_ABBR_AFTER_RE = re.compile(r"^\s*,\s*(?:" + "|".join(_US_STATE_ABBR) + r")\b")

# US state name that is ALSO a country: without a country word alongside it,
# "Tbilisi, Georgia" reads exactly like "Atlanta, Georgia" — so bare "georgia"
# never anchors the US on its own (Atlanta itself is covered by _US_MARKERS,
# and real US postings write "Georgia, USA" / "…, United States").
_AMBIGUOUS_STATE_NAMES = ("georgia",)


def _has_foreign(loc: str) -> bool:
    """True if the location names a foreign region. Word-boundary matched, and a
    marker immediately followed by a US state/USA token doesn't count — "Ontario,
    California, United States", "Paris, Texas, USA", "London, KY" are US cities,
    while "Ontario, Canada" / "Paris, France" stay foreign."""
    for m in _FOREIGN_RE.finditer(loc):
        rest = loc[m.end():]
        if _US_STATE_AFTER_RE.match(rest) or _US_ABBR_AFTER_RE.match(rest):
            continue
        return True
    return False


def _has_us_anchor(loc: str) -> bool:
    """True if the location names the US: a country word, a full US state name
    (word-boundary matched), or a US marker. State *abbreviations* are
    intentionally NOT used here — "or" (Oregon), "in" (Indiana), "me" (Maine)
    collide with common English words ("BC, or NS"). Every real onsite US posting
    carries "USA"/"United States" or a full state name, so this stays accurate
    without the ambiguous 2-letter set.
    """
    if any(m in loc for m in _US_NATIONAL) or any(m in loc for m in _US_MARKERS):
        return True
    # Bare "US" token ("US - Distributed") and "NAMER" (North-America region tag
    # remote-first employers use) — word-boundary so "aus"/"u.s." can't collide.
    if _has_word(loc, "us") or _has_word(loc, "namer"):
        return True
    for st in _US_STATE_NAMES:
        if not _has_word(loc, st):
            continue
        if st in _AMBIGUOUS_STATE_NAMES and not any(w in loc for w in _COUNTRY_WORDS):
            continue
        return True
    return False


@dataclass
class Profile:
    title_keywords: list[str] = field(default_factory=list)
    title_exclude: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    remote_ok: bool = True
    max_age_days: int = 30  # only keep postings listed within this many days (0 = no limit)

    @classmethod
    def load(cls, path: Path) -> "Profile":
        data = validate_search_config(
            json.loads(Path(path).read_text(encoding="utf-8"))
        )
        return cls(
            title_keywords=[s.lower() for s in data.get("title_keywords", [])],
            title_exclude=[s.lower() for s in data.get("title_exclude", [])],
            locations=[s.lower() for s in data.get("locations", [])],
            remote_ok=data.get("remote_ok", True),
            max_age_days=data.get("max_age_days", 30) or 0,
        )


def validate_search_config(data: object) -> dict:
    """Validate the JSON shape before it can silently change a search.

    The preferences file is deliberately hand-editable, but JSON makes a
    scalar string iterable and treats a string such as ``"false"`` as truthy.
    Reject those shapes with a plain error instead of quietly showing the wrong
    jobs.  The returned object is the original mapping so callers can continue
    to preserve optional fields such as ``fit`` and ``referral_bio``.
    """
    if not isinstance(data, dict):
        raise ValueError("search preferences must be a JSON object")

    list_fields = ("title_keywords", "title_exclude", "locations", "companies")
    for field_name in list_fields:
        if field_name not in data:
            continue
        value = data[field_name]
        if (not isinstance(value, list)
                or any(not isinstance(item, str) or not item.strip() for item in value)):
            raise ValueError(
                f"search preferences field '{field_name}' must be a JSON array of non-empty text values"
            )

    if "referral_bio" in data and not isinstance(data["referral_bio"], str):
        raise ValueError("search preferences field 'referral_bio' must be text")

    if "remote_ok" in data and type(data["remote_ok"]) is not bool:
        raise ValueError("search preferences field 'remote_ok' must be true or false")

    if "max_age_days" in data:
        value = data["max_age_days"]
        if type(value) is not int or value < 0:
            raise ValueError(
                "search preferences field 'max_age_days' must be a whole number 0 or greater"
            )

    fit = data.get("fit")
    if fit is not None:
        if not isinstance(fit, dict):
            raise ValueError("search preferences field 'fit' must be a JSON object")
        for field_name in ("skills", "too_junior", "too_senior", "gatekeepers"):
            if field_name in fit and (
                not isinstance(fit[field_name], list)
                or any(not isinstance(item, str) or not item.strip() for item in fit[field_name])
            ):
                raise ValueError(
                    f"search preferences fit.{field_name} must be a JSON array of non-empty text values"
                )

    return data


def _parse_dt(value: str | None):
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    for candidate in (s, s[:19], s[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def is_fresh(opp: Opportunity, max_age_days: int, now: datetime | None = None) -> bool:
    """True if the posting was listed within `max_age_days`.

    Uses the ATS-provided `posted_at` (falls back to when we first saw it). A
    posting with no parseable date is KEPT — better to show an undated role than
    to silently hide it. `max_age_days <= 0` disables the cutoff.

    Exception: a posting whose ATS only gave a floor ("Posted 30+ Days Ago" →
    `posted_is_floor`) is at LEAST that old and could be months older — it is
    treated as beyond the cutoff, not as an undated role.
    """
    if not max_age_days or max_age_days <= 0:
        return True
    if getattr(opp, "posted_is_floor", False):
        return False
    dt = _parse_dt(getattr(opp, "posted_at", None)) or _parse_dt(getattr(opp, "first_seen_at", None))
    if dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - dt) <= timedelta(days=max_age_days)


def _looks_like_title(text: str) -> bool:
    """A real job title is short and label-like — not a sentence or a section
    heading. Guards against ATS quirks (e.g. Lever handing us a JD sentence or
    "Qualifications:") whose stray words would otherwise match a search keyword.
    """
    t = (text or "").strip()
    if not t or t.endswith(":"):
        return False
    return len(t) <= 120 and len(t.split()) <= 16


def title_matches(opp: Opportunity, profile: Profile) -> bool:
    # With no title keywords the starter means "any role". Keep optional excludes
    # useful, but do not make a malformed ATS title disappear from a broad board.
    title_part = opp.title if _looks_like_title(opp.title) else ""
    title = title_part.lower()
    department = (opp.department or "").lower()

    if not profile.title_keywords:
        return not any(_has_word(title, term) for term in profile.title_exclude)

    # Title-driven match: keyword in the title, excludes judged on the title.
    # Excludes are word-boundary matched so "intern" doesn't kill "Internal
    # Audit" and "engineer" doesn't kill a role whose title merely names its
    # partner organization. The department deliberately can't veto a clean title.
    if title:
        if any(_has_word(title, term) for term in profile.title_exclude):
            return False
        if any(_keyword_matches(title, term) for term in profile.title_keywords):
            return True

    # Department-driven match: the keyword lives only in the department. A
    # department must not hit an exclude. The title-shape guard already removes
    # sentence/heading junk; every short title is a valid kind of job title.
    if not department or not any(_keyword_matches(department, term) for term in profile.title_keywords):
        return False
    if any(_has_word(department, term) for term in profile.title_exclude):
        return False
    return True


def _has_word(text: str, term: str) -> bool:
    """Word-boundary match so short terms like 'ny' don't hit 'sunnyvale'."""
    return re.search(r"\b" + re.escape(term) + r"\b", text) is not None


def _keyword_matches(text: str, term: str) -> bool:
    """Match a configurable keyword without turning a short word into a substring."""
    term = (term or "").strip().lower()
    if not term:
        return False
    # Punctuation-heavy terms such as "FP&A" or "C++" need substring matching;
    # ordinary words and phrases get boundaries so "art" does not match
    # "cartographer".
    return term in text if re.search(r"[^a-z0-9\s]", term) else _has_word(text, term)

def location_matches(opp: Opportunity, profile: Profile) -> bool:
    """Keep a posting when its location matches the private search preferences."""
    return location_verdict(opp, profile) == "keep"


def location_verdict(opp: Opportunity, profile: Profile) -> str:
    """Three-way location decision from the location field alone.

    - "keep":  a configured location, remote-US when enabled, or a US-wide
      posting when remote roles are enabled.
    - "maybe": a different US city with no remote signal (e.g.
      "San Francisco, CA, USA"). Onsite on its face, but remote-first employers
      and JD text ("open to remote") can make it eligible — so refresh may
      rescue it via
      `jd_allows_remote_or_ny` / the company's remote-first tag.
    - "drop":  foreign-only or otherwise not US-relevant.
    """
    loc = (opp.location or "").lower()
    if any(term.strip().lower() in {"all", "any"} for term in profile.locations):
        return "keep"
    if not loc:
        # No location string at all — surface the match; the human confirms.
        return "keep"

    configured = [
        term for term in profile.locations
        if term and term != "remote"
    ]
    if any(_has_word(loc, term) for term in configured):
        return "keep"

    is_remote = profile.remote_ok and (
        "remote" in loc or "work at home" in loc or "work from home" in loc)
    has_foreign = _has_foreign(loc)
    has_us = _has_us_anchor(loc)
    names_city = _names_specific_city(loc)

    # Hybrid tied to an unselected hub requires being near that office. A remote
    # JD signal cannot rescue it; only a genuine remote marker here can.
    if "hybrid" in loc and names_city and not is_remote:
        return "drop"

    # A specifically-named unselected city with no remote signal. "New Brunswick,
    # New Jersey, United States" / "USA, TX, Irving" / "San Francisco, CA" — onsite.
    if names_city and not is_remote:
        # US city → "maybe" (a JD/remote-first rescue gets a second look).
        # A named city with no US anchor is foreign/unknown → hard drop.
        if has_us:
            return "drop" if has_foreign else "maybe"
        return "drop"

    # Remote — keep unless it points only at a foreign region.
    if is_remote:
        return "keep" if not (has_foreign and not has_us) else "drop"

    # US-national / distributed postings — the location names the country or a
    # "<State>, United States"/nationwide form with NO specific city (e.g.
    # "United States", "California, United States", "Nationwide"). Remote-or-
    # distributed by nature — keep even without the literal "remote".
    if profile.remote_ok and has_us and not has_foreign:
        return "keep"

    return "drop"


# Job-description signals that an onsite-city posting is actually remote-
# eligible. Remote-first employers state this in the JD even when the location
# field names their HQ city ("remote-friendly", "#LI-Remote", "open to remote").
_JD_REMOTE_SIGNALS = re.compile(
    r"(#li-remote|remote[-\s]?first|remote[-\s]?friendly|fully[-\s]remote|"
    r"work[-\s]from[-\s]anywhere|work\s+anywhere|remote\s*\(?\s*u\.?\s?s|"
    r"u\.?\s?s\.?[-\s]?based\s+remote|us[-\s]?remote|remote\s+within\s+the\s+u|"
    r"remote\s+in\s+the\s+u|open\s+to\s+remote|remote\s+candidates|"
    r"remote[-\s]eligible|remote\s+(?:role|position|opportunity)|"
    r"can\s+be\s+(?:done|performed|based)\s+remotely|(?:fully[-\s])?distributed\s+team|"
    r"hybrid\s+or\s+remote|flexible\s+(?:work\s+)?location|location[-\s]flexible|"
    r"anywhere\s+in\s+the\s+u)",
    re.I,
)
# NY *eligibility* — a location-choice or explicit-consideration construct, NOT a
# bare mention. Bare "New York" is almost always boilerplate (HQ blurb, office
# list, NYC Local Law 144 notice, salary-band footnote), so it is deliberately
# NOT a signal. Real signals look like "…based in San Francisco or New York City"
# or "candidates in NYC … will also be considered".
_JD_NY_SIGNAL = re.compile(
    r"\bor\s+(?:new york|nyc)\b"
    r"|(?:new york|nyc)\s+or\s+(?:remote|hybrid|san|seattle|boston|austin|chicago|washington)"
    r"|(?:open\s+to|considering|candidates?\s+(?:in|based\s+in)|hiring\s+in)\s+(?:new york|nyc)",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def jd_allows_remote_or_ny(
    description_html: str | None,
    profile: Profile | None = None,
) -> bool:
    """True if the job description signals remote eligibility or NY consideration.

    Rescues an onsite-city ("maybe") posting when its own text says the role is
    remote-eligible or would consider New York candidates — the case the applicant
    flagged where the location field says one city but the JD is broader. Bare
    mentions of New York (office lists, legal notices) are intentionally ignored.
    """
    if not description_html:
        return False
    text = _TAG_RE.sub(" ", description_html)
    allow_remote = profile is None or profile.remote_ok
    configured = set(profile.locations) if profile is not None else {"new york", "nyc", "ny"}
    allow_ny = bool(configured & {"new york", "nyc", "ny"})
    return bool(
        (allow_remote and _JD_REMOTE_SIGNALS.search(text))
        or (allow_ny and _JD_NY_SIGNAL.search(text))
    )


def remote_first_slugs(companies: list[dict]) -> set:
    """Slugs of companies tagged remote-first (`remote_first: true` or a
    "remote-first" tag). Their onsite-city roles are kept without a JD check."""
    out = set()
    for c in companies:
        tags = [str(t).lower() for t in (c.get("tags") or [])]
        if c.get("remote_first") or "remote-first" in tags:
            slug = c.get("slug")
            if slug:
                out.add(slug)
    return out


def matches(opp: Opportunity, profile: Profile) -> bool:
    return title_matches(opp, profile) and location_matches(opp, profile)


def apply_profile(opportunities: list[Opportunity], profile: Profile) -> list[Opportunity]:
    return [opp for opp in opportunities if matches(opp, profile)]
