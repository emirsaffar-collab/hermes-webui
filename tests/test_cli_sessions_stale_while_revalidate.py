"""Stale-while-revalidate for the CLI-sessions sidebar cache (#sidebar-230s).

2026-09-14 receipts: /api/sessions took 230-294s under IO starvation while the
same read-only queries run 0.04-2.8s unloaded. The synchronous owner path made
each request wait for its own slow rebuild even with a usable stale entry in
hand; non-owner waiters already served stale after 0.1-0.25s. These tests pin
the new contract: stale is served IMMEDIATELY when one exists, the refresh runs
in the background, and a cold cache still builds synchronously.
"""

import threading
import time

import api.models as models
from api.models import get_cli_sessions


def _reset_caches():
    with models._CLI_SESSIONS_CACHE_LOCK:
        models._CLI_SESSIONS_CACHE.clear()
        models._CLI_SESSIONS_CACHE_INFLIGHT.clear()
        models._CLI_SESSIONS_BG_REFRESH_THREADS.clear()


def _force_expire_all_entries():
    """Rewrite every cache entry with a past expiry so the stale path engages."""
    with models._CLI_SESSIONS_CACHE_LOCK:
        for key, entry in list(models._CLI_SESSIONS_CACHE.items()):
            if len(entry) == 3:
                _expires, stamp, sessions = entry
                models._CLI_SESSIONS_CACHE[key] = (time.monotonic() - 1, stamp, sessions)
            else:
                _expires, sessions = entry
                models._CLI_SESSIONS_CACHE[key] = (time.monotonic() - 1, sessions)


def test_stale_entry_served_immediately_with_background_refresh(monkeypatch):
    """An expired-but-valid entry is returned at once; the rebuild never blocks."""
    _reset_caches()
    release = threading.Event()
    started = threading.Event()

    def _slow_rebuild(*a, **k):
        started.set()
        release.wait(timeout=10)  # simulate the IO-starved rebuild
        return [{"session_id": "fresh"}]

    # 1. Seed a valid entry with a live TTL.
    monkeypatch.setattr(models, '_CLI_SESSIONS_CACHE_TTL_SECONDS', 60.0)
    monkeypatch.setattr(
        models, '_load_cli_sessions_uncached',
        lambda *a, **k: [{"session_id": "stale-row"}],
    )
    seeded = get_cli_sessions()
    assert any(s.get("session_id") == "stale-row" for s in seeded)

    # 2. Expire it, then swap in the slow rebuild.
    _force_expire_all_entries()
    monkeypatch.setattr(models, '_load_cli_sessions_uncached', _slow_rebuild)

    t0 = time.monotonic()
    result = get_cli_sessions()
    elapsed = time.monotonic() - t0
    assert any(s.get("session_id") == "stale-row" for s in result), (
        "expired entry must be served immediately instead of waiting for the rebuild"
    )
    assert elapsed < 2.0, f"request blocked {elapsed:.1f}s on its own rebuild"
    assert started.wait(timeout=5), "background refresh must actually run"
    release.set()
    _reset_caches()


def test_cold_cache_still_builds_synchronously(monkeypatch):
    """No stale entry -> the owner builds synchronously (no empty sidebar flash)."""
    _reset_caches()
    built = threading.Event()

    def _build(*a, **k):
        built.set()
        return [{"session_id": "cold"}]

    monkeypatch.setattr(models, '_load_cli_sessions_uncached', _build)
    monkeypatch.setattr(models, '_CLI_SESSIONS_CACHE_TTL_SECONDS', 60.0)
    result = get_cli_sessions()
    assert built.is_set(), "cold build must run synchronously in the request"
    assert any(s.get("session_id") == "cold" for s in result)
    _reset_caches()


def test_background_refresh_populates_cache(monkeypatch):
    """The background thread lands the fresh rows so the NEXT request hits them."""
    _reset_caches()
    monkeypatch.setattr(models, '_CLI_SESSIONS_CACHE_TTL_SECONDS', 60.0)

    monkeypatch.setattr(
        models, '_load_cli_sessions_uncached',
        lambda *a, **k: [{"session_id": "old"}],
    )
    get_cli_sessions()  # seeds the valid entry
    _force_expire_all_entries()

    monkeypatch.setattr(
        models, '_load_cli_sessions_uncached',
        lambda *a, **k: [{"session_id": "new"}],
    )
    served = get_cli_sessions()  # stale serve + background refresh
    assert any(s.get("session_id") == "old" for s in served)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with models._CLI_SESSIONS_CACHE_LOCK:
            threads_alive = [
                t for t in models._CLI_SESSIONS_BG_REFRESH_THREADS.values() if t.is_alive()
            ]
        if not threads_alive:
            break
        time.sleep(0.02)
    served_after = get_cli_sessions()  # now expects the cached FRESH rows
    assert any(s.get("session_id") == "new" for s in served_after), (
        "background refresh must populate the cache for the next request"
    )
    _reset_caches()
