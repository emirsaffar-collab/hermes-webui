"""Regression coverage for per-event notification controls (2026-09-11).

Companion to test_pwa_notification_controls.py: 'Response complete' pushes are
opt-in (notifications_complete_enabled, default off) because with many parallel
sessions a completion push per finished turn is spam. Approval/clarification
notifications stay on the master notifications_enabled switch.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")


def test_completion_notification_gated_by_optin_setting():
    """The done-path 'Response complete' push must be wrapped in the opt-in gate."""
    # Opt-IN semantics: undefined (not yet loaded) must NOT notify — only an
    # explicit true does. This is what makes the feature default-off for
    # existing installs without a settings round-trip.
    assert "window._notificationsCompleteEnabled===true" in MESSAGES_JS
    assert "window._notificationsCompleteEnabled!==false" not in MESSAGES_JS
    # The gate wraps the completion call specifically...
    assert (
        "if(window._notificationsCompleteEnabled===true){"
        in MESSAGES_JS
    )
    # ...and the completion call remains inside it (not duplicated ungated).
    assert (
        MESSAGES_JS.count("sendBrowserNotification('Response complete'")
        == 1
    )


def test_approval_and_clarification_notifications_ungated():
    """Approvals/clarifications always notify via the master switch only."""
    assert "sendBrowserNotification('Approval required'" in MESSAGES_JS
    assert "sendBrowserNotification('Clarification needed'" in MESSAGES_JS
    # No opt-in gate on the approval path (it precedes the completion gate).
    approval_idx = MESSAGES_JS.index("sendBrowserNotification('Approval required'")
    gate_idx = MESSAGES_JS.index("window._notificationsCompleteEnabled===true")
    assert approval_idx < gate_idx


def test_setting_key_plumbed_through_all_layers():
    # Server-side default + allowlist
    assert '"notifications_complete_enabled": False,' in CONFIG_PY
    assert '"notifications_complete_enabled",' in CONFIG_PY
    # Boot-time apply
    assert (
        "window._notificationsCompleteEnabled=!!s.notifications_complete_enabled;"
        in BOOT_JS
    )
    # Settings save payload + saved-settings apply + export body
    assert PANELS_JS.count("notifications_complete_enabled") >= 3
    assert 'id="settingsNotificationsCompleteEnabled"' in INDEX_HTML


def test_i18n_keys_present_in_every_locale():
    labels = I18N_JS.count("settings_label_notifications_complete:")
    descs = I18N_JS.count("settings_desc_notifications_complete:")
    # 15 locales (en,it,ja,ru,es,de,zh,zh-Hant,pt,ko,fr,cs,tr,pl,vi) each carry
    # exactly one label + one desc. The master-key pattern ends with ':'
    # before the word boundary, so the _complete keys do NOT inflate it.
    assert labels == 15, labels
    assert descs == 15, descs
    # Master keys still present in every locale, unchanged by the insertions.
    assert I18N_JS.count("settings_label_notifications:") == 15
    assert I18N_JS.count("settings_desc_notifications:") == 15
