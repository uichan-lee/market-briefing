"""Tests for the delivery layer. SPEC §2.0, §12 steps 10–11.

The failure modes that matter are the quiet ones: a channel silently dropped, a
summary silently sent where the full report was wanted, a missing secret raising
instead of being reported. Every test here is about failure being *stated*.
"""

from __future__ import annotations

import datetime as dt
import smtplib

import pytest

from src.notify.base import UnknownChannel, channel_for, deliver, unavailable_channels
from src.notify.email import EmailChannel
from src.notify.vault import VaultChannel

DAY = dt.date(2026, 8, 6)

EMAIL_CONFIG = {
    "type": "email",
    "to": "reader@example.com",
    "from": "sender@example.com",
    "host": "smtp.example.com",
    "port": 465,
    "body": "summary",
}


# --- channel_for -----------------------------------------------------------


def test_email_builds_from_a_complete_config():
    channel = channel_for(EMAIL_CONFIG)
    assert isinstance(channel, EmailChannel)
    assert channel.body == "summary"


def test_a_half_configured_email_channel_is_refused():
    """No default host: a report must never be sent to a server nobody chose."""
    broken = {k: v for k, v in EMAIL_CONFIG.items() if k != "host"}
    with pytest.raises(UnknownChannel, match="host"):
        channel_for(broken)


def test_webhook_stays_unbuilt_and_says_so():
    with pytest.raises(UnknownChannel, match="webhook"):
        channel_for({"type": "webhook"})


def test_the_committed_delivery_config_builds_every_channel():
    """The regression this pins: email was 'declared but not built' for one
    step, and the header had to carry it as a failure. Now both build."""
    from src.util.config import load_delivery

    assert unavailable_channels(load_delivery().get("channels", [])) == []


# --- email -----------------------------------------------------------------


def test_a_missing_smtp_secret_is_a_failed_delivery_not_a_crash(monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    result = channel_for(EMAIL_CONFIG).send("# 브리핑\n", day=DAY)
    assert not result.delivered
    assert "SMTP_PASSWORD" in result.detail


class _RecordingSMTP:
    """Stands in for smtplib.SMTP_SSL; records what would have been sent."""

    last: _RecordingSMTP | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.logins: list[tuple[str, str]] = []
        self.messages: list = []
        _RecordingSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, message):
        self.messages.append(message)


def test_email_sends_with_the_report_title_as_subject(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(smtplib, "SMTP_SSL", _RecordingSMTP)

    result = channel_for(EMAIL_CONFIG).send("# 📅 2026-08-06 (목) 브리핑\n\n본문", day=DAY)

    assert result.delivered
    smtp = _RecordingSMTP.last
    assert smtp.logins == [("sender@example.com", "app-password")]
    message = smtp.messages[0]
    assert message["Subject"] == "📅 2026-08-06 (목) 브리핑"
    assert message["To"] == "reader@example.com"


def test_an_smtp_error_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")

    class Refusing:
        def __init__(self, *a, **k):
            raise smtplib.SMTPConnectError(421, "busy")

    monkeypatch.setattr(smtplib, "SMTP_SSL", Refusing)
    result = channel_for(EMAIL_CONFIG).send("x", day=DAY)
    assert not result.delivered
    assert "SMTPConnectError" in result.detail


# --- deliver routing -------------------------------------------------------


def test_summary_goes_to_summary_channels_and_the_vault_keeps_the_full_text(tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(smtplib, "SMTP_SSL", _RecordingSMTP)

    channels = [
        {"type": "vault", "path": "reports/"},
        EMAIL_CONFIG,
    ]
    results = deliver(
        "FULL REPORT", channels, day=DAY, label="morning", summary="SUMMARY", root=tmp_path
    )

    assert all(r.delivered for r in results)
    written = (tmp_path / "reports" / "2026-08-06-morning.md").read_text(encoding="utf-8")
    assert written == "FULL REPORT"
    assert _RecordingSMTP.last.messages[0].get_content().strip() == "SUMMARY"


def test_without_a_summary_every_channel_gets_the_full_report(tmp_path, monkeypatch):
    """A shortened copy nobody built would be a silent truncation."""
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(smtplib, "SMTP_SSL", _RecordingSMTP)

    deliver("FULL", [EMAIL_CONFIG], day=DAY, root=tmp_path)
    assert _RecordingSMTP.last.messages[0].get_content().strip() == "FULL"


def test_the_run_label_keeps_morning_and_evening_files_apart(tmp_path):
    """SPEC §1 publishes twice a day; the morning file must not replace the
    evening one."""
    vault = VaultChannel(path=tmp_path / "reports", commit=False)
    vault.send("morning text", day=DAY, label="morning")
    vault.send("evening text", day=DAY, label="evening")

    assert (tmp_path / "reports" / "2026-08-06-morning.md").exists()
    assert (tmp_path / "reports" / "2026-08-06-evening.md").exists()
