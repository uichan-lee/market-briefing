"""Tests for the backfill driver.

The one that matters is ``test_a_refused_krx_session_stops_the_source``. The
collectors *report* a refused KRX session as a check result rather than raising,
which is what CLAUDE.md asks of a collector — but a driver that only watches for
exceptions sees a clean return and an empty frame, and cannot tell that apart
from a range with no data in it.

That is not hypothetical. On 2026-08-05 the loop walked every chunk of a
three-year range, wrote nothing, and reported ``0 rows -> 0 files`` as a normal
outcome. Forty scheduled attempts over three hours did the same, none of them
saying why. A backfill that quietly fills nothing is the exact failure this
project treats as its worst.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from scripts import backfill
from src.collectors.validate import CheckResult, ValidationReport


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never let a test write into the real data/raw/."""
    monkeypatch.setattr(
        backfill,
        "PATHS",
        {name: tmp_path / name for name in backfill.PATHS},
    )


def _refused_report() -> ValidationReport:
    report = ValidationReport(collector="kr_price")
    report.add(CheckResult("schema", False, "frame is empty"))
    report.add(
        CheckResult("krx_session", False, "could not establish a KRX session: rate limiting.")
    )
    return report


def test_a_refused_krx_session_stops_the_source(monkeypatch):
    calls = {"n": 0}

    def fetch(tickers, start, end, **kwargs):
        calls["n"] += 1
        return pd.DataFrame(columns=["date", "ticker"]), _refused_report()

    result = backfill._backfill_kr(
        "kr_price", fetch, dt.date(2023, 8, 3), dt.date(2026, 8, 3), revise=False
    )

    # Stopped at the first chunk rather than walking all four.
    assert calls["n"] == 1
    assert "STOPPED" in result
    assert "re-run to continue" in result


def test_the_stop_message_carries_the_reason(monkeypatch):
    def fetch(tickers, start, end, **kwargs):
        return pd.DataFrame(columns=["date", "ticker"]), _refused_report()

    result = backfill._backfill_kr(
        "kr_price", fetch, dt.date(2026, 8, 1), dt.date(2026, 8, 3), revise=False
    )
    # "0 rows -> 0 files" told nobody anything. The reason has to travel.
    assert "rate limiting" in result


def test_other_check_failures_do_not_stop_the_run(monkeypatch):
    """Only a refused session is fatal to the driver.

    A continuity failure on one ticker is ordinary — a name listed after the
    window opens produces one — and must not abandon the remaining years.
    """
    calls = {"n": 0}

    def fetch(tickers, start, end, **kwargs):
        calls["n"] += 1
        report = ValidationReport(collector="kr_price")
        report.add(CheckResult("trading_day_continuity[SNDK]", False, "listed later"))
        days = pd.date_range(start, end, freq="D")[:2]
        return pd.DataFrame({"date": days, "ticker": "005930"}), report

    result = backfill._backfill_kr(
        "kr_price", fetch, dt.date(2023, 8, 3), dt.date(2026, 8, 3), revise=False
    )
    assert calls["n"] > 1
    assert "STOPPED" not in result


def test_an_existing_date_is_never_overwritten(tmp_path):
    """CLAUDE.md rule 1. A re-run lands beside the original, never on it."""
    day = dt.date(2024, 1, 2)
    backfill.PATHS["kr_price"].mkdir(parents=True, exist_ok=True)
    original = backfill.PATHS["kr_price"] / f"{day.isoformat()}.parquet"
    original.write_bytes(b"original")

    assert backfill._target("kr_price", day, revise=False) is None

    revised = backfill._target("kr_price", day, revise=True)
    assert revised is not None
    assert revised.name == "2024-01-02-v2.parquet"
    assert original.read_bytes() == b"original"


def test_writing_splits_a_multi_day_frame_by_session(tmp_path):
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-02", "2024-01-03"]),
            "ticker": ["005930", "000660", "005930"],
            "close": [1.0, 2.0, 3.0],
        }
    )
    written = backfill._write_by_date("kr_price", df, revise=False)
    assert written == 2
    files = sorted(p.name for p in backfill.PATHS["kr_price"].glob("*.parquet"))
    assert files == ["2024-01-02.parquet", "2024-01-03.parquet"]


def test_dates_already_written_are_not_refetched(tmp_path):
    """Resumability is the design point, not a convenience."""
    backfill.PATHS["kr_price"].mkdir(parents=True, exist_ok=True)
    (backfill.PATHS["kr_price"] / "2026-07-30.parquet").write_bytes(b"x")

    pending = backfill._pending("kr_price", dt.date(2026, 7, 29), dt.date(2026, 7, 31), "KR")
    assert dt.date(2026, 7, 30) not in pending
    assert dt.date(2026, 7, 29) in pending
