"""/api/refresh state machine — the claim must be synchronous.

The bug: do_POST used to read `running` under the lock but let the *spawned
thread* set `running=True`. Two near-simultaneous POSTs (two near-simultaneous local requests)
could both see `running=False` and start two full pipelines writing
jobs.local.json concurrently; and a status poll landing right after the POST
could read the PREVIOUS run's persisted "done" and reload a stale board.
`_start_refresh_async` claims the state under the lock before spawning.
"""

import threading
import time

import jobs


def _reset_state():
    with jobs._REFRESH_LOCK:
        jobs._REFRESH_STATE.update(running=False, phase="idle", done=0, total=0,
                                   message="", started_at=None, finished_at=None,
                                   result=None, error=None)


def test_start_refresh_claims_state_synchronously(monkeypatch):
    _reset_state()
    calls = []
    release = threading.Event()

    def slow_worker():
        calls.append(1)
        release.wait(timeout=5)

    monkeypatch.setattr(jobs, "_run_refresh_thread", slow_worker)
    # Simulate the stale terminal state a finished run leaves behind.
    with jobs._REFRESH_LOCK:
        jobs._REFRESH_STATE.update(phase="done", result={"postings": 7},
                                   finished_at="2026-07-12T00:00:00Z")

    try:
        assert jobs._start_refresh_async() is True
        # Claimed before the worker even runs: a poll right now must never see
        # running=False or the previous run's "done"/result.
        with jobs._REFRESH_LOCK:
            snap = dict(jobs._REFRESH_STATE)
        assert snap["running"] is True
        assert snap["phase"] == "starting"
        assert snap["result"] is None and snap["error"] is None
        assert snap["finished_at"] is None
        assert snap["started_at"]

        # A second POST while one is running must NOT start a second pipeline.
        assert jobs._start_refresh_async() is False
        for _ in range(200):          # let the (single) worker thread start
            if calls:
                break
            time.sleep(0.01)
        assert calls == [1]
    finally:
        release.set()
        _reset_state()


def test_worker_direct_invocation_still_claims(monkeypatch):
    # _run_refresh_thread called directly (no _start_refresh_async) must still
    # claim + reset the state itself, then release `running` when done.
    _reset_state()
    monkeypatch.setattr(jobs, "refresh_board", lambda progress=None: {
        "opportunities": [], "meta": {"companies_with_postings": 0}})
    with jobs._REFRESH_LOCK:
        jobs._REFRESH_STATE.update(phase="error", error="old failure")

    jobs._run_refresh_thread()

    with jobs._REFRESH_LOCK:
        snap = dict(jobs._REFRESH_STATE)
    assert snap["running"] is False           # released
    assert snap["phase"] == "done"
    assert snap["error"] is None              # old error cleared
    assert snap["result"] == {
        "postings": 0, "companies": 0, "warnings": 0, "errors": 0,
    }
    _reset_state()


def test_worker_result_keeps_feed_health_counts(monkeypatch):
    _reset_state()
    monkeypatch.setattr(jobs, "refresh_board", lambda progress=None: {
        "opportunities": [{"id": "one"}],
        "meta": {
            "companies_with_postings": 1,
            "warnings": [{"company": "Capped Co"}],
            "errors": [{"company": "Down Co"}],
        },
    })

    jobs._run_refresh_thread()

    with jobs._REFRESH_LOCK:
        assert jobs._REFRESH_STATE["result"] == {
            "postings": 1, "companies": 1, "warnings": 1, "errors": 1,
        }
    _reset_state()
