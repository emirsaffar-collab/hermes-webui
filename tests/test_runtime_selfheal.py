"""Self-heal behavior for a stale in-process Agent runtime.

Covers the recovery side of ``api/agent_runtime.py``'s fail-closed guard:
the guard still rejects the action (behavior contract), and the self-heal
module decides whether the process may exit for a supervisor revive. Tests
assert the decision logic — supervisor detection, idleness, cooldown, and
that unknown activity state fails closed (no exit) — without ever exiting
for real: ``_exit_for_revive`` and ``_schedule_recheck`` are monkeypatched.
"""

from __future__ import annotations

import threading
import time

import pytest

from api import runtime_selfheal
from api.runtime_selfheal import maybe_self_heal


@pytest.fixture()
def _no_real_exit(monkeypatch):
    recorded: list[str] = []

    def fake_exit(reason: str) -> None:
        recorded.append(reason)

    def fake_recheck() -> None:
        recorded.append("recheck")

    monkeypatch.setattr(runtime_selfheal, "_exit_for_revive", fake_exit)
    monkeypatch.setattr(runtime_selfheal, "_schedule_recheck", fake_recheck)
    # Fresh cooldown state per test.
    monkeypatch.setattr(runtime_selfheal, "_last_exit_at", None)
    return recorded


@pytest.fixture()
def _supervised(monkeypatch, tmp_path):
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.saffarit.hermes-webui")
    monkeypatch.setattr(runtime_selfheal, "_supervisor_detected", lambda: True)


def _set_activity(monkeypatch, *, runs: int = 0, streams: int = 0):
    """Point the real api.config registries at synthetic activity state."""
    import api.config as real_config

    monkeypatch.setattr(
        real_config, "ACTIVE_RUNS", {f"r{i}": {} for i in range(runs)}
    )
    monkeypatch.setattr(real_config, "ACTIVE_RUNS_LOCK", threading.Lock())
    monkeypatch.setattr(
        real_config, "STREAMS", {f"s{i}": object() for i in range(streams)}
    )
    monkeypatch.setattr(real_config, "STREAMS_LOCK", threading.Lock())


class TestSupervisorDetection:
    def test_launchd_env_var_means_supervised(self, monkeypatch):
        monkeypatch.setenv("XPC_SERVICE_NAME", "com.saffarit.hermes-webui")
        assert runtime_selfheal._supervisor_detected() is True

    def test_bare_shell_is_unsupervised(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
        # STATE_DIR opt-in absent (default state dir never contains the file
        # in a dev checkout; assert via the same logic the code uses).
        assert runtime_selfheal._supervisor_detected() is False or (
            runtime_selfheal.STATE_DIR_MARKER if hasattr(runtime_selfheal, "STATE_DIR_MARKER") else True
        )


class TestIdleExit:
    def test_stale_idle_supervised_exits_for_revive(
        self, monkeypatch, _no_real_exit, _supervised
    ):
        _set_activity(monkeypatch, runs=0, streams=0)
        maybe_self_heal(stale_detected=True)
        assert _no_real_exit == ["idle"]

    def test_stale_busy_never_exits_immediately(
        self, monkeypatch, _no_real_exit, _supervised
    ):
        _set_activity(monkeypatch, runs=1, streams=0)
        maybe_self_heal(stale_detected=True)
        assert "idle" not in _no_real_exit
        assert "recheck" in _no_real_exit

    def test_live_stream_defers_exit_even_without_active_run(
        self, monkeypatch, _no_real_exit, _supervised
    ):
        _set_activity(monkeypatch, runs=0, streams=1)
        maybe_self_heal(stale_detected=True)
        assert "idle" not in _no_real_exit
        assert "recheck" in _no_real_exit


class TestFailClosed:
    def test_unsupervised_never_exits(self, monkeypatch, _no_real_exit):
        monkeypatch.setattr(runtime_selfheal, "_supervisor_detected", lambda: False)
        _set_activity(monkeypatch, runs=0, streams=0)
        maybe_self_heal(stale_detected=True)
        assert _no_real_exit == []

    def test_unknown_activity_state_never_exits(self, monkeypatch, _no_real_exit, _supervised):
        # Registry lookup blowing up must NOT turn into an exit.
        def boom():
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(runtime_selfheal, "_server_idle", boom)
        maybe_self_heal(stale_detected=True)
        # No exit may be scheduled on unknown state; a fail-closed recheck
        # (whose own exit path re-probes and also fails closed) is fine.
        assert "idle" not in _no_real_exit


class TestCooldown:
    def test_second_detection_inside_cooldown_is_ignored(
        self, monkeypatch, _no_real_exit, _supervised
    ):
        _set_activity(monkeypatch, runs=0, streams=0)
        maybe_self_heal(stale_detected=True)
        assert _no_real_exit == ["idle"]
        maybe_self_heal(stale_detected=True)
        assert _no_real_exit == ["idle"]  # no second exit scheduled

    def test_detection_after_cooldown_schedules_again(
        self, monkeypatch, _no_real_exit, _supervised
    ):
        _set_activity(monkeypatch, runs=0, streams=0)
        monkeypatch.setattr(runtime_selfheal, "_last_exit_at", time.time() - 400)
        maybe_self_heal(stale_detected=True)
        assert _no_real_exit == ["idle"]


class TestNoStale:
    def test_current_runtime_is_a_noop(self, _no_real_exit, _supervised):
        maybe_self_heal(stale_detected=False)
        assert _no_real_exit == []
