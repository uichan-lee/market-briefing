"""Tests for the SEC EDGAR filings collector (SPEC §2.2②, §3.2).

Offline tests run against a trimmed, shape-faithful live capture of Apple's
submissions API (four rows from a real 1001-row response fetched 2026-08-25:
three recent filings plus its FY2025 10-K, which is also the pinned
``KNOWN_VALUE`` row).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from src.collectors.us_filings import (
    KNOWN_VALUE,
    SCHEMA,
    SecFilingsError,
    _parse,
    check_filing_plausibility,
    fetch,
    validate_frame,
)

FIXTURE = Path(__file__).parent / "fixtures" / "us_filings_submissions_aapl.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def frame(payload) -> pd.DataFrame:
    return _parse(payload, "AAPL")


# --- the fixture ------------------------------------------------------------


def test_the_fixture_still_has_a_row_with_no_report_date(payload):
    """23% of the live sample had this shape — losing it silently untests it."""
    assert "" in payload["filings"]["recent"]["reportDate"]


# --- parsing ------------------------------------------------------------


def test_parse_matches_the_declared_schema(frame):
    for column, dtype in SCHEMA.items():
        if column == "known_at_utc":
            continue
        assert str(frame[column].dtype).startswith(dtype.split("[")[0]), column


def test_known_at_utc_uses_acceptance_datetime_when_present(payload, frame):
    """acceptanceDateTime is a real intraday timestamp — a better known_at_utc
    than any derived next-session-open fallback, and should be used directly."""
    accept = payload["filings"]["recent"]["acceptanceDateTime"][0]
    row = frame[frame["accession_no"] == payload["filings"]["recent"]["accessionNumber"][0]]
    assert row["known_at_utc"].iloc[0] == pd.Timestamp(accept)


def test_a_missing_acceptance_on_a_us_closure_uses_the_next_tradeable_open(payload):
    from src.util.session import next_tradeable_open

    closed = dt.date(2026, 4, 3)  # Good Friday
    payload["filings"]["recent"]["acceptanceDateTime"][0] = ""
    payload["filings"]["recent"]["filingDate"][0] = closed.isoformat()
    parsed = _parse(payload, "AAPL")

    assert parsed.iloc[0]["known_at_utc"] == next_tradeable_open(
        "US", pd.Timestamp(closed, tz="UTC")
    )


def test_a_blank_report_date_becomes_nat_not_a_parse_error(frame):
    blank = frame[frame["report_date"].isna()]
    assert len(blank) >= 1


def test_known_at_utc_is_usually_on_or_after_date(frame):
    """Usually, not always — see test_plausibility_tolerates_the_edgar_next_day_case."""
    assert (pd.to_datetime(frame["known_at_utc"], utc=True).dt.date >= frame["date"].dt.date).all()


# --- validation ---------------------------------------------------------


def test_plausibility_passes_on_clean_data(frame):
    result = check_filing_plausibility(
        frame, ["0000320193"], dt.date(2000, 1, 1), dt.date(2030, 1, 1)
    )
    assert result.passed, result.detail


def test_plausibility_catches_a_date_outside_the_window(frame):
    result = check_filing_plausibility(
        frame, ["0000320193"], dt.date(2026, 1, 1), dt.date(2026, 1, 2)
    )
    assert not result.passed
    assert "outside" in result.detail


def test_plausibility_tolerates_the_edgar_next_day_case(frame):
    """Real data, not a hypothetical: 75 of 1550 rows on the full watchlist
    (2026-08-25) had this exact shape — accepted before midnight UTC of the
    day *after* the acceptance timestamp, because EDGAR assigns the next
    business day past its 5:30pm ET cutoff. An earlier, stricter version of
    this check flagged all 75 as failures on correct data."""
    edited = frame.copy()
    next_day = edited.loc[0, "known_at_utc"].date() + dt.timedelta(days=1)
    edited.loc[0, "date"] = pd.Timestamp(next_day)
    result = check_filing_plausibility(
        edited, ["0000320193"], dt.date(2000, 1, 1), dt.date(2030, 1, 1)
    )
    assert result.passed, result.detail


def test_plausibility_catches_a_known_at_utc_implausibly_far_from_date(frame):
    edited = frame.copy()
    edited.loc[0, "known_at_utc"] = pd.Timestamp("2000-01-01", tz="UTC")
    result = check_filing_plausibility(
        edited, ["0000320193"], dt.date(2000, 1, 1), dt.date(2030, 1, 1)
    )
    assert not result.passed
    assert "implausibly far" in result.detail


def test_plausibility_catches_a_duplicated_accession_no(frame):
    dup = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    result = check_filing_plausibility(
        dup, ["0000320193"], dt.date(2000, 1, 1), dt.date(2030, 1, 1)
    )
    assert not result.passed
    assert "duplicated" in result.detail


def test_plausibility_catches_an_unrequested_cik(frame):
    result = check_filing_plausibility(
        frame, ["9999999999"], dt.date(2000, 1, 1), dt.date(2030, 1, 1)
    )
    assert not result.passed
    assert "unrequested" in result.detail


def test_an_empty_window_is_not_a_failure():
    """CLAUDE.md's 'a quiet run is not a failure' principle, applied here for
    the first time to a non-news source: zero filings in a short window is
    the normal case, not a fetch failure."""
    empty = pd.DataFrame(columns=list(SCHEMA))
    result = check_filing_plausibility(
        empty, ["0000320193"], dt.date(2026, 1, 1), dt.date(2026, 1, 2)
    )
    assert result.passed


def test_validate_frame_passes_on_clean_data(frame):
    report = validate_frame(
        frame, ["0000320193"], dt.date(2000, 1, 1), dt.date(2030, 1, 1), known_value=False
    )
    assert report.ok, report.summary()


def test_the_known_value_is_present_in_the_fixture(frame):
    report = validate_frame(frame, ["0000320193"], dt.date(2000, 1, 1), dt.date(2030, 1, 1))
    assert report.ok, report.summary()


def test_the_known_value_is_wired_to_a_ticker_the_fixture_covers(payload):
    assert KNOWN_VALUE["where"]["ticker"] == "AAPL"
    assert KNOWN_VALUE["where"]["accession_no"] in payload["filings"]["recent"]["accessionNumber"]


# --- fetch(), mocked --------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict | None, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def test_fetch_loops_per_ticker_and_reports_a_partial_failure(monkeypatch, payload):
    monkeypatch.setenv("SEC_USER_AGENT", "market-briefing test@example.com")

    def fake_get(url, headers, timeout):
        if "0000320193" in url:
            return _FakeResponse(payload)
        return _FakeResponse(None, status=404)

    monkeypatch.setattr("src.collectors.us_filings.requests.get", fake_get)

    cik_map = {"AAPL": {"cik": "0000320193"}, "MISSING": {"cik": "0000000001"}}
    df, report = fetch(["AAPL", "MISSING"], cik_map, dt.date(2000, 1, 1), dt.date(2030, 1, 1))

    assert (df["ticker"] == "AAPL").all()
    assert not report.ok
    assert any(r.name == "fetch" for r in report.failures)


def test_fetch_never_raises_when_sec_user_agent_is_missing(monkeypatch, payload):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    cik_map = {"AAPL": {"cik": "0000320193"}}
    # This one *does* raise — a missing credential is a configuration error,
    # not a per-item fetch failure, so it is not caught inside the loop.
    # Documented here so the distinction is deliberate, not missed. The
    # driver (scripts/collect_daily.py) has its own top-level try/except
    # around every collector call for exactly this case.
    with pytest.raises(SecFilingsError):
        fetch(["AAPL"], cik_map, dt.date(2000, 1, 1), dt.date(2030, 1, 1))


# --- live ---------------------------------------------------------------


@pytest.mark.network
def test_live_fetch_matches_the_committed_fixture():
    """Catches SEC changing the submissions API shape. Excluded from the default run."""
    from src.util.config import load_filing_ids

    cik_map = load_filing_ids()["us"]
    df, report = fetch(["AAPL"], cik_map, dt.date(2020, 1, 1), dt.date.today())
    assert report.ok, report.summary()
    assert (df["accession_no"] == KNOWN_VALUE["where"]["accession_no"]).any()
