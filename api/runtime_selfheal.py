"""Self-heal for a stale in-process Agent runtime.

The revision guard (``api.agent_runtime``) fail-closes the WebUI when the
Hermes Agent checkout moves under the running process — the correct
behavior, since mixing cached modules with new source is a real bug class.
Before this module the only recovery was a human restarting the service.

This module closes that gap WITHOUT weakening the guard: when the runtime
is known-stale and the server is idle (no active agent runs and no live SSE
streams), the process exits deliberately so the supervisor (launchd
``KeepAlive``, or any process manager) revives a fresh one that imports the
new revision cleanly. When the server is busy, it schedules a re-check so
the restart lands as soon as the last run drains.

Safety rails:
- Only exits when a supervisor is detected (launchd label env var or a
  state-file opt-in). Without a supervisor an exit would strand the UI, so
  the module logs and does nothing instead.
- Cooldown: at most one deliberate exit per ``_COOLDOWN_SECONDS``. A
  pathological checkout (e.g. a fast-moving rebase) cannot flap the service.
- Exits happen from a short-lived daemon thread via ``os._exit`` — never
  from a request handler (which would truncate the HTTP response) and never
  while a run/stream is active.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Distinctive exit code so supervisors/logs can tell a deliberate self-heal
# exit from a crash. 75 = EX_TEMPFAIL, the classic "retry me" convention.
SELF_HEAL_EXIT_CODE = 75

_COOLDOWN_SECONDS = 300.0
_RECHECK_SECONDS = 60.0
_EXIT_DELAY_SECONDS = 2.0

_last_exit_at: float | None = None
_scheduler_lock = threading.Lock()
_scheduler_started = False


def _supervisor_detected() -> bool:
    """Return True when a process supervisor will revive this process.

    ``launchctl`` sets ``XPC_SERVICE_NAME`` for every GUI-session job. On the
    dev workstation the WebUI also always carries ``HERMES_WEBUI_PORT`` from
    its plist, but that alone does not prove supervision — the launchd env
    check is the authoritative signal. A state-file opt-in covers container
    setups (docker restart policies, systemd) where the env var is absent.
    """
    if os.environ.get("XPC_SERVICE_NAME"):
        return True
    try:
        from api.config import STATE_DIR

        return bool((STATE_DIR / "selfheal-supervised").exists())
    except Exception:
        return False


def _server_idle() -> bool:
    """Return True when no agent run or live SSE stream is active.

    ``ACTIVE_RUNS`` tracks worker lifecycle (broader than streams); empty
    streams alone are not enough — a run between stream handoffs would be
    killed mid-flight. Both must be clear.
    """
    try:
        from api import config as live_config

        with live_config.ACTIVE_RUNS_LOCK:
            if live_config.ACTIVE_RUNS:
                return False
        with live_config.STREAMS_LOCK:
            if live_config.STREAMS:
                return False
        return True
    except Exception:
        # Unknown activity state: fail closed, never exit on uncertainty.
        return False


def _write_exit_receipt(message: str) -> None:
    """Write the self-heal receipt directly to stderr, bypassing logging.

    ``os._exit`` skips atexit and may race queued logging handlers, so the
    receipt must not depend on the logging pipeline. Plist captures stderr
    in hermes-webui.err — this line is the drift-proof audit trail.
    """
    try:
        import sys as _sys

        print(f"[selfheal] {message}", file=_sys.stderr, flush=True)
    except Exception:
        pass


def _exit_for_revive(reason: str) -> None:
    """Exit the process from a daemon thread so the supervisor revives it."""
    global _last_exit_at

    def _worker() -> None:
        time.sleep(_EXIT_DELAY_SECONDS)
        _write_exit_receipt(
            f"agent runtime stale and server idle ({reason}) — exiting {SELF_HEAL_EXIT_CODE} for supervisor revive"
        )
        os._exit(SELF_HEAL_EXIT_CODE)

    threading.Thread(target=_worker, name="webui-selfheal-exit", daemon=True).start()


def maybe_self_heal(stale_detected: bool) -> None:
    """Act on a stale-runtime detection: exit when idle, re-check when busy.

    Called from the barrier paths AFTER ``AgentRuntimeChangedError`` has been
    raised — the response the user sees is unaffected; this schedules the
    recovery in the background. Idempotent within the cooldown window.
    """
    global _last_exit_at

    if not stale_detected:
        return
    if not _supervisor_detected():
        logger.warning(
            "[selfheal] agent runtime stale but no supervisor detected — staying up (manual restart required)"
        )
        return

    now = time.time()
    if _last_exit_at is not None and (now - _last_exit_at) < _COOLDOWN_SECONDS:
        return

    try:
        idle = _server_idle()
    except Exception:
        idle = False  # unknown activity state: fail closed, never exit
    if idle:
        _last_exit_at = now
        _exit_for_revive("idle")
        return

    _last_exit_at = now
    _schedule_recheck()


def _schedule_recheck() -> None:
    """Re-check idleness in the background; exit once the last run drains."""

    def _worker() -> None:
        deadline = time.time() + _COOLDOWN_SECONDS
        while time.time() < deadline:
            time.sleep(_RECHECK_SECONDS)
            try:
                from api.agent_runtime import ensure_agent_runtime_current

                ensure_agent_runtime_current()
                return  # checkout moved back (reset) — cancel the restart
            except Exception:
                pass
            if _server_idle():
                _write_exit_receipt(
                    f"agent runtime stale and runs drained — exiting {SELF_HEAL_EXIT_CODE} for supervisor revive"
                )
                os._exit(SELF_HEAL_EXIT_CODE)

    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    threading.Thread(target=_worker, name="webui-selfheal-recheck", daemon=True).start()
