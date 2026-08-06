"""The ``email`` delivery channel. SPEC §2.0 — the mobile reading path.

Sends the briefing over SMTP-over-SSL from the dedicated account declared in
``config/delivery.yaml``. Only the password is a secret and it arrives through
the environment (``SMTP_PASSWORD``); addresses, host and port live in config
where a wrong value can be reviewed rather than discovered.

``body: summary`` in the config means this channel receives the header plus the
directional-rating section rather than the full document — a judgment about
phone screens, made in ``src/notify/base.py`` where the payload is routed, not
here. This adapter sends whatever it is given.
"""

from __future__ import annotations

import datetime as dt
import os
import smtplib
from email.message import EmailMessage

from src.notify.base import DeliveryResult


class EmailChannel:
    """Send the report as a plain-text email."""

    name = "email"

    def __init__(
        self,
        *,
        to: str,
        sender: str,
        host: str,
        port: int,
        secret_env: str = "SMTP_PASSWORD",
        body: str = "summary",
    ) -> None:
        self.to = to
        self.sender = sender
        self.host = host
        self.port = port
        self.secret_env = secret_env
        self.body = body

    def send(self, report: str, *, day: dt.date, label: str | None = None) -> DeliveryResult:
        password = os.environ.get(self.secret_env, "")
        if not password:
            # A missing secret is a delivery failure, not a crash: the vault
            # copy must still land, and the header of the *next* report will
            # carry this channel as failed.
            return DeliveryResult(self.name, False, f"{self.secret_env} is not set")

        message = EmailMessage()
        # The report's own title line is the subject, so the inbox list reads
        # as dates rather than as thirty copies of the same string.
        first_line = report.strip().splitlines()[0].lstrip("# ").strip()
        message["Subject"] = first_line or f"마켓 브리핑 {day.isoformat()}"
        message["From"] = self.sender
        message["To"] = self.to
        message.set_content(report)

        try:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as smtp:
                smtp.login(self.sender, password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            return DeliveryResult(self.name, False, f"{type(exc).__name__}: {exc}")

        return DeliveryResult(self.name, True, f"{self.to} ({len(report):,} chars)")
