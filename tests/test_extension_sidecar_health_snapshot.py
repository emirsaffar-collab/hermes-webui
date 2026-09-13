"""Drift guard for the server-side loopback sidecar health snapshot.

Root cause this locks in (RCA 2026-09-14): the Extensions panel's sidecar badge
probed http://127.0.0.1:<port>/health DIRECTLY from the browser, so on a phone
(loopback = the phone itself) or in Safari (loopback fetch from an https page is
mixed content) a perfectly healthy sidecar rendered "unreachable / blocked".

The fix: /api/extensions/status now embeds a server-side probe under the
`sidecar_health` key (device-independent), and panels.js treats it as the
PRIMARY badge source, falling back to the direct browser probe only when the
server snapshot is absent (pre-restart back-compat). These tests fail if either
side drifts.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PANELS_JS = REPO_ROOT / "static" / "panels.js"


@pytest.fixture(autouse=True)
def _clear_extension_env(monkeypatch):
    for name in (
        "HERMES_WEBUI_EXTENSION_DIR",
        "HERMES_WEBUI_EXTENSION_MANIFEST",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def _write_manifest(root: Path, origin: str, health_path: str = "/health") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "extensions.json").write_text(
        json.dumps(
            {
                "extensions": [
                    {
                        "id": "probe-sidecar",
                        "name": "Probe Sidecar",
                        "sidecar": {
                            "type": "loopback",
                            "origin": origin,
                            "health_path": health_path,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _status_for(tmp_path, monkeypatch, origin="http://127.0.0.1:17999"):
    root = tmp_path / "extensions"
    _write_manifest(root, origin)
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_DIR", str(root))
    monkeypatch.setenv("HERMES_WEBUI_EXTENSION_MANIFEST", "extensions.json")
    import api.extensions as extensions

    extensions._SIDECAR_HEALTH_CACHE.clear()
    return extensions.get_extension_status()


def test_status_embeds_server_side_sidecar_health(tmp_path, monkeypatch):
    """The status payload carries a sidecar_health map keyed by extension id."""
    status = _status_for(tmp_path, monkeypatch)
    assert status["sidecars"], "manifest sidecar record missing"
    health = status.get("sidecar_health")
    assert isinstance(health, dict), "sidecar_health key missing from status"
    entry = health.get("probe-sidecar")
    assert isinstance(entry, dict)
    # Nothing listens on 17796 in the test env → probe must report a failure
    # STATE, never raise, and never mark a silent default "healthy".
    assert entry["status"] in ("unreachable", "unhealthy")
    assert isinstance(entry["detail"], str) and entry["detail"]


def test_sidecar_health_probe_is_cached(tmp_path, monkeypatch):
    """A second status call within the TTL reuses the cached probe result."""
    import api.extensions as extensions

    _status_for(tmp_path, monkeypatch)
    assert "http://127.0.0.1:17999/health" in extensions._SIDECAR_HEALTH_CACHE
    probe_calls = []

    real = extensions._probe_sidecar_health_sidecars

    def counting(sidecars):
        probe_calls.append(1)
        return real(sidecars)

    monkeypatch.setattr(extensions, "_probe_sidecar_health_sidecars", counting)
    status2 = extensions.get_extension_status()
    assert status2["sidecar_health"]["probe-sidecar"]["status"] in ("unreachable", "unhealthy")
    assert probe_calls == [1], "probe re-ran inside the TTL window"


def test_sidecar_health_reports_misconfigured_for_unsafe_origin(tmp_path, monkeypatch):
    """A sidecar record whose origin cannot be a safe loopback is reported
    as misconfigured rather than dialed."""
    status = _status_for(tmp_path, monkeypatch)
    import api.extensions as extensions

    unsafe = [{"id": "bad", "origin": "http://evil.example", "health_url": "http://evil.example/health"}]
    result = extensions._probe_sidecar_health_sidecars(unsafe)
    assert result == {"bad": {"status": "misconfigured", "detail": "unsafe or missing loopback origin"}}


def test_sidecar_records_shape_unchanged(tmp_path, monkeypatch):
    """The public sidecars[] record shape must not change: it is a contract
    (see test_extension_status_endpoint). sidecar_health is additive only."""
    status = _status_for(tmp_path, monkeypatch)
    (record,) = status["sidecars"]
    assert set(record.keys()) == {
        "id",
        "name",
        "type",
        "origin",
        "health_path",
        "health_url",
        "proxy",
    }, f"sidecar record keys drifted: {sorted(record.keys())}"


# ── Frontend contract: panels.js must keep server-first, direct-fallback ────


def test_panels_js_uses_server_health_as_primary_source():
    js = PANELS_JS.read_text(encoding="utf-8")
    assert "sidecar_health" in js, "panels.js no longer reads sidecar_health"
    # Primary branch: an entry with a string status short-circuits the fetch.
    assert "healthy (server)" in js and "misconfigured" in js
    # Fallback branch: the direct fetch(healthUrl) probe still exists and its
    # catch still maps network errors to 'unreachable / blocked'.
    assert "fetch(healthUrl,{credentials:'omit'" in js
    assert "unreachable / blocked'" in js
    # The health_url-missing path must say 'misconfigured', not 'unreachable'.
    import re

    missing_branch = re.search(
        r"if\(!healthUrl\)\{[^}]*_setExtensionSidecarHealth\(index,'blocked','([^']+)'", js
    )
    assert missing_branch, "health_url-missing branch not found"
    assert missing_branch.group(1) == "misconfigured"


def test_panels_js_repolls_while_visible():
    """The badge must not be a single-shot snapshot: a re-poll path exists."""
    js = PANELS_JS.read_text(encoding="utf-8")
    assert "_extensionsSidecarHealthTimer" in js
    assert "loadExtensionsPanel({preserveExisting:true}),30000" in js
