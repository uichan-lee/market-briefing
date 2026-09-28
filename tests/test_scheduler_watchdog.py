"""Execute the workflow's email assembly without network or delivery side effects."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


@pytest.mark.parametrize("details", [None, "", "run=42\ncreated_at=2026-09-28T20:17:15Z", "x" * 5000])
def test_watchdog_evidence_is_optional(monkeypatch, details):
    from src.notify import base
    from src.util import config

    sent = []
    monkeypatch.setenv("SCHEDULER_REASON", "test reason")
    monkeypatch.delenv("SCHEDULER_DETAILS", raising=False)
    if details is not None:
        monkeypatch.setenv("SCHEDULER_DETAILS", details)
    channels = [{"type": "vault"}, {"type": "email"}]
    monkeypatch.setattr(config, "load_delivery", lambda: {"channels": channels})

    def deliver(notice, selected, **kwargs):
        sent.append((notice, selected, kwargs["label"]))
        return [SimpleNamespace(ok=True)]

    monkeypatch.setattr(base, "deliver", deliver)
    workflow = yaml.safe_load(Path(".github/workflows/scheduler-watchdog.yml").read_text())
    script = workflow["jobs"]["notify"]["steps"][-1]["run"]
    code = script.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    try:
        exec(compile(code, "scheduler-watchdog.yml", "exec"), {})
    except SystemExit as exc:
        assert exc.code == 0
    assert len(sent) == 1
    notice, selected, label = sent[0]
    assert selected == [{"type": "email"}]
    assert label == "SCHEDULER"
    assert "test reason" in notice
    assert ("판단 근거" in notice) == bool(details)
    if details:
        assert details[:4000] in notice
        assert len(notice) < 4100
