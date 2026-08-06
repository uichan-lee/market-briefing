"""Delivery channel interface. SPEC §2.0.

A channel takes a rendered report and puts it somewhere a human will see it.
Channels are selected by the ``type`` field in ``config/delivery.yaml``, and
CLAUDE.md's absolute rule 5 forbids adding one that is not declared there — so
:func:`channel_for` refuses an unknown type rather than improvising.

**Delivery reports rather than raises.** A channel that fails must not cost the
other channels their copy: the whole point of two channels is that the report
survives one of them being broken. This mirrors the collectors, where a failed
fetch is recorded and the pipeline continues.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class DeliveryResult:
    """What one channel did, whether or not it worked."""

    channel: str
    delivered: bool
    detail: str

    def __str__(self) -> str:
        mark = "ok" if self.delivered else "FAILED"
        return f"{self.channel:<8} {mark}  {self.detail}"


class Channel(Protocol):
    """What every adapter under ``src/notify/`` implements."""

    name: str

    def send(
        self,
        report: str,
        *,
        day: dt.date,
        label: str | None = None,
        html: str | None = None,
    ) -> DeliveryResult:
        """Deliver ``report``. Records failure in the result, never raises.

        ``label`` distinguishes the two SPEC §1 runs of one day ("morning" /
        "evening") so they do not claim the same destination. ``html`` is an
        optional rendered alternative for channels that display rich text;
        channels that store a file ignore it and keep the markdown source.
        """
        ...


class UnknownChannel(ValueError):
    """Raised for a ``type`` not implemented here.

    Deliberately an exception rather than a skipped channel: a typo in
    ``delivery.yaml`` would otherwise mean the report is silently sent to one
    fewer place than intended, which is exactly the quiet degradation CLAUDE.md
    forbids.
    """


def channel_for(config: Mapping[str, Any], *, root: Path | None = None) -> Channel:
    """Build the adapter one ``delivery.yaml`` entry describes.

    ``webhook`` is declared in SPEC §2.0 and deliberately not built; it raises
    here rather than being silently dropped, so a report configured to reach a
    destination never appears to have been delivered when it was not.
    """
    from src.notify.email import EmailChannel
    from src.notify.vault import VaultChannel

    kind = config.get("type")
    if kind == "vault":
        return VaultChannel(
            path=Path(config.get("path", "reports/")),
            commit=bool(config.get("commit", False)),
            root=root,
        )
    if kind == "email":
        try:
            return EmailChannel(
                to=config["to"],
                sender=config["from"],
                host=config["host"],
                port=int(config["port"]),
                secret_env=str(config.get("smtp_secret", "SMTP_PASSWORD")),
                body=str(config.get("body", "summary")),
            )
        except KeyError as exc:
            # A half-configured channel must fail loudly at build time, not
            # send to a default host nobody chose.
            raise UnknownChannel(f"email channel is missing {exc} in delivery.yaml") from exc
    if kind == "webhook":
        raise UnknownChannel("channel 'webhook' is declared in delivery.yaml but not built yet")
    raise UnknownChannel(f"unknown channel type {kind!r}")


def unavailable_channels(channels: list[Mapping[str, Any]]) -> list[str]:
    """Configured channels that cannot be built, known *before* sending.

    Delivery failures are discovered after the report is rendered, which is too
    late for the header. But a channel that is declared in ``delivery.yaml`` and
    has no adapter is knowable up front — and that is the case that matters
    today, since ``email`` is configured and unbuilt. Without this the report
    would state a clean header and then fail to reach the mailbox it names.
    """
    unavailable = []
    for config in channels:
        try:
            channel_for(config)
        except UnknownChannel:
            unavailable.append(str(config.get("type", "?")))
    return unavailable


def deliver(
    report: str,
    channels: list[Mapping[str, Any]],
    *,
    day: dt.date,
    label: str | None = None,
    summary: str | None = None,
    summary_html: str | None = None,
    root: Path | None = None,
) -> list[DeliveryResult]:
    """Send ``report`` through every configured channel.

    One channel failing does not stop the others, and a channel that cannot be
    built is reported as a failed delivery rather than omitted from the list —
    the caller needs to see that it was configured and did not happen.

    ``summary`` is the short form for channels whose config says
    ``body: summary``. The routing lives here rather than in the adapters so a
    channel never has to know the document's structure — it sends what it is
    handed. When no summary was built, every channel gets the full report; a
    shortened copy nobody asked for would be a silent truncation.
    """
    results: list[DeliveryResult] = []
    for config in channels:
        kind = str(config.get("type", "?"))
        try:
            adapter = channel_for(config, root=root)
        except UnknownChannel as exc:
            results.append(DeliveryResult(kind, False, str(exc)))
            continue
        wants_summary = getattr(adapter, "body", "full") == "summary" and summary is not None
        content = summary if wants_summary else report
        html = summary_html if wants_summary else None
        results.append(adapter.send(content, day=day, label=label, html=html))
    return results
