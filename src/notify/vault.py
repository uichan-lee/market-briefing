"""The ``vault`` delivery channel. SPEC §2.0.

Writes the rendered briefing to ``reports/YYYY-MM-DD.md``, which the Obsidian
Git plugin pulls into the vault. This is the default channel and the only one
that works without a credential.

**Committing is not done here.** ``delivery.yaml`` carries ``commit: true``, and
SPEC §2.0 assigns the commit to the Actions workflow — which also has to
``git pull --rebase`` around the hourly news job. Running ``git`` from inside the
adapter as well would race with that for the index. The flag is reported back in
:class:`DeliveryResult` so the caller can see it was requested and by whom it is
honoured, rather than the adapter quietly doing nothing with it.

**A same-day re-render overwrites.** Unlike ``data/raw/``, which CLAUDE.md rule 1
protects because it is collected and unrepeatable, a report is derived output —
re-rendering an hour later with more complete data should replace the earlier
copy, not accumulate ``-v2`` files in a folder a human reads. The rating behind
it is kept separately and immutably by the renderer.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from src.notify.base import DeliveryResult


class VaultChannel:
    """Write the report into a directory the vault syncs."""

    name = "vault"

    def __init__(self, *, path: Path, commit: bool = False, root: Path | None = None) -> None:
        self.path = path
        self.commit = commit
        self.root = root or Path()

    def target(self, day: dt.date) -> Path:
        return self.root / self.path / f"{day.isoformat()}.md"

    def send(self, report: str, *, day: dt.date) -> DeliveryResult:
        target = self.target(day)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report, encoding="utf-8")
        except OSError as exc:
            # Narrow: a write failure is the one thing that can go wrong here,
            # and anything else should surface rather than be swallowed.
            return DeliveryResult(self.name, False, f"could not write {target}: {exc}")

        note = " (commit left to the Actions workflow)" if self.commit else ""
        return DeliveryResult(self.name, True, f"{target}{note}")
