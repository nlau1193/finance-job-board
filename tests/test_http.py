"""post_json caching — the GraphQL path used for Ashby application forms.

Unlike get_json, post_json keys its on-disk cache on url + request body, so two
different queries to the same GraphQL endpoint don't collide (and a repeat query
is served from cache without a second network call).
"""

import jobhunt.ats._http as http


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    """Records every POST so we can assert cache hits/misses."""

    def __init__(self):
        self.calls = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        return _Resp({"call": self.calls, "body": json})

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        return _Resp({"call": self.calls, "url": url})


def test_post_json_caches_by_body(monkeypatch, tmp_path):
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    session = _Session()

    first = http.post_json("https://x/graphql", {"q": 1}, session=session)
    again = http.post_json("https://x/graphql", {"q": 1}, session=session)
    assert first == again
    assert session.calls == 1  # second identical query served from cache

    http.post_json("https://x/graphql", {"q": 2}, session=session)
    assert session.calls == 2  # different body → distinct cache key → live call


def test_post_json_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    session = _Session()
    http.post_json("https://x/graphql", {"q": 1}, session=session, use_cache=False)
    http.post_json("https://x/graphql", {"q": 1}, session=session, use_cache=False)
    assert session.calls == 2


def test_post_json_raises_on_error(monkeypatch, tmp_path):
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)

    class _Bad:
        def post(self, *a, **k):
            class R:
                status_code = 500

                def json(self):
                    return {}
            return R()

    import pytest
    with pytest.raises(http.FetchError):
        http.post_json("https://x/graphql", {"q": 1}, session=_Bad())


def test_corrupt_cache_timestamp_is_a_miss(monkeypatch, tmp_path):
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    session = _Session()
    path = http._cache_path("https://x/jobs")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"fetched_epoch":"bad","payload":{"old":true}}', encoding="utf-8")

    value = http.get_json("https://x/jobs", session=session)
    assert value["call"] == 1
    assert session.calls == 1
