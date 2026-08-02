"""Tiny cached HTTP layer shared by the ATS fetchers.

Public ATS board APIs are CDN-cached and intended for job distribution, so a
light on-disk cache (default 1h) keeps refreshes cheap and polite. Set
`use_cache=False` (the `jobs refresh --no-cache` / `verify_universe.py` path) to
force a live hit.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / ".cache"
DEFAULT_TTL = 3600  # seconds
USER_AGENT = "job-hunt-board/1.0 (personal job-search; structured-ATS-API client)"


class FetchError(RuntimeError):
    """Raised when a board API cannot be fetched or parsed."""


def _cache_path_for(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{digest}.json"


def _cache_path(url: str) -> Path:
    return _cache_path_for(url)


def _read_cache(path: Path, ttl: int) -> object | None:
    if ttl <= 0 or not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(envelope, dict):
        return None
    fetched_epoch = envelope.get("fetched_epoch")
    if (isinstance(fetched_epoch, bool)
            or not isinstance(fetched_epoch, (int, float))):
        return None
    try:
        if not math.isfinite(fetched_epoch) or fetched_epoch > time.time():
            return None
    except (OverflowError, TypeError):
        return None
    if time.time() - fetched_epoch > ttl:
        return None
    return envelope.get("payload")


def _write_cache(path: Path, payload: object) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fetched_epoch": time.time(), "payload": payload}),
            encoding="utf-8",
        )
    except OSError:
        pass  # cache is best-effort; never fail a refresh over it


def get_json(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 20,
    ttl: int = DEFAULT_TTL,
    use_cache: bool = True,
) -> object:
    """GET `url`, returning parsed JSON. Cached on disk unless `use_cache=False`."""
    cache_path = _cache_path(url)
    if use_cache:
        cached = _read_cache(cache_path, ttl)
        if cached is not None:
            return cached

    http = session or requests
    try:
        response = http.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise FetchError(f"request failed: {exc}") from exc

    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code} for {url}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise FetchError(f"non-JSON response from {url}: {exc}") from exc

    _write_cache(cache_path, payload)
    return payload


def post_json(
    url: str,
    body: dict,
    *,
    session: requests.Session | None = None,
    timeout: int = 20,
    ttl: int = DEFAULT_TTL,
    use_cache: bool = True,
) -> object:
    """POST `body` as JSON to `url`, returning parsed JSON.

    Used for GraphQL board APIs (Ashby's application form). Cached on disk like
    `get_json`, but keyed on url + the request body so different queries to the
    same endpoint don't collide.
    """
    cache_key = url + "\n" + json.dumps(body, sort_keys=True, ensure_ascii=False)
    cache_path = _cache_path_for(cache_key)
    if use_cache:
        cached = _read_cache(cache_path, ttl)
        if cached is not None:
            return cached

    http = session or requests
    try:
        response = http.post(
            url,
            json=body,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise FetchError(f"request failed: {exc}") from exc

    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code} for {url}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise FetchError(f"non-JSON response from {url}: {exc}") from exc

    _write_cache(cache_path, payload)
    return payload


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session
