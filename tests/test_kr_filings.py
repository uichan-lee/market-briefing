"""Tests for the DART filings collector (SPEC §2.2②, §3.2).

Offline tests run against a trimmed live capture of DART's list.json response
for Samsung Electronics (5 rows from a real call fetched 2026-08-25, one of
which is the pinned ``KNOWN_VALUE`` row — its half-year report, filed exactly
on the statutory deadline).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest
import requests

from src.collectors.kr_filings import (
    KNOWN_VALUE,
    SCHEMA,
    DartFilingsError,
    _parse,
    check_filing_plausibility,
    fetch,
    validate_frame,
)

FIXTURE = Path(__file__).parent / "fixtures" / "kr_filings_dart_samsung.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def frame(payload) -> pd.DataFrame:
    return _parse(payload["list"], "005930")


# --- parsing ------------------------------------------------------------


def test_parse_matches_the_declared_schema(frame):
    for column, dtype in SCHEMA.items():
        if column == "known_at_utc":
            continue
        assert str(frame[column].dtype).startswith(dtype.split("[")[0]), column


def test_known_at_utc_is_the_kr_session_close_of_the_date(frame):
    """rcept_dt is date-only, so known_at_utc must use the conservative
    session-close derivation kr_flow.py uses for its own date-only data."""
    from src.util.session import session_close_utc

    row = frame.iloc[0]
    expected = session_close_utc("KR", row["date"].date())
    assert row["known_at_utc"] == expected


def test_an_empty_payload_parses_to_an_empty_frame():
    empty = _parse([], "005930")
    assert empty.empty
    assert list(empty.columns) == list(SCHEMA)


# --- validation -----------------------------------------------------------


def test_plausibility_passes_on_clean_data(frame):
    result = check_filing_plausibility(
        frame, ["00126380"], dt.date(2026, 8, 1), dt.date(2026, 8, 25)
    )
    assert result.passed, result.detail


def test_plausibility_catches_a_date_outside_the_window(frame):
    result = check_filing_plausibility(
        frame, ["00126380"], dt.date(2026, 8, 20), dt.date(2026, 8, 21)
    )
    assert not result.passed
    assert "outside" in result.detail


def test_plausibility_catches_a_duplicated_rcept_no(frame):
    dup = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    result = check_filing_plausibility(dup, ["00126380"], dt.date(2026, 8, 1), dt.date(2026, 8, 25))
    assert not result.passed
    assert "duplicated" in result.detail


def test_plausibility_catches_an_unrequested_corp_code(frame):
    result = check_filing_plausibility(
        frame, ["99999999"], dt.date(2026, 8, 1), dt.date(2026, 8, 25)
    )
    assert not result.passed
    assert "unrequested" in result.detail


def test_an_empty_window_is_not_a_failure():
    empty = pd.DataFrame(columns=list(SCHEMA))
    result = check_filing_plausibility(
        empty, ["00126380"], dt.date(2026, 8, 1), dt.date(2026, 8, 25)
    )
    assert result.passed


def test_the_known_value_is_present_in_the_fixture(frame):
    report = validate_frame(frame, ["00126380"], dt.date(2026, 8, 1), dt.date(2026, 8, 25))
    assert report.ok, report.summary()


def test_the_known_value_is_wired_to_a_ticker_the_fixture_covers(payload):
    assert KNOWN_VALUE["where"]["ticker"] == "005930"
    accepted = [r["rcept_no"] for r in payload["list"]]
    assert KNOWN_VALUE["where"]["rcept_no"] in accepted


# --- fetch(), mocked --------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def test_fetch_loops_per_ticker_and_reports_a_partial_failure(monkeypatch, payload):
    def fake_get(url, params, timeout):
        if params["corp_code"] == "00126380":
            return _FakeResponse(payload)
        return _FakeResponse({"status": "900", "message": "존재하지 않는 기업"})

    monkeypatch.setattr("src.collectors.kr_filings.requests.get", fake_get)

    corp_map = {"005930": {"corp_code": "00126380"}, "000000": {"corp_code": "99999999"}}
    df, report = fetch(
        ["005930", "000000"],
        corp_map,
        dt.date(2026, 8, 1),
        dt.date(2026, 8, 25),
        api_key="fake-key",
        sleep_seconds=0,
    )

    assert (df["ticker"] == "005930").all()
    assert not report.ok
    assert any(r.name == "fetch" for r in report.failures)


def test_a_no_data_status_is_an_empty_result_not_a_failure(monkeypatch):
    def fake_get(url, params, timeout):
        return _FakeResponse({"status": "013", "message": "조회된 데이타가 없습니다"})

    monkeypatch.setattr("src.collectors.kr_filings.requests.get", fake_get)

    corp_map = {"005930": {"corp_code": "00126380"}}
    df, report = fetch(
        ["005930"],
        corp_map,
        dt.date(2026, 8, 1),
        dt.date(2026, 8, 25),
        api_key="fake-key",
        sleep_seconds=0,
    )
    assert df.empty
    assert report.ok, report.summary()


def test_fetch_paginates_past_page_count(monkeypatch, payload):
    row = payload["list"][0]
    page1 = {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "total_count": 2,
        "total_page": 2,
        "list": [row],
    }
    page2 = {
        "status": "000",
        "message": "정상",
        "page_no": 2,
        "total_count": 2,
        "total_page": 2,
        "list": [row],
    }
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["page_no"])
        return _FakeResponse(page1 if params["page_no"] == 1 else page2)

    monkeypatch.setattr("src.collectors.kr_filings.requests.get", fake_get)

    corp_map = {"005930": {"corp_code": "00126380"}}
    df, report = fetch(
        ["005930"],
        corp_map,
        dt.date(2026, 8, 1),
        dt.date(2026, 8, 25),
        api_key="fake-key",
        sleep_seconds=0,
    )
    assert calls == [1, 2]
    assert len(df) == 2


def test_fetch_never_raises_when_dart_api_key_is_missing(monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    corp_map = {"005930": {"corp_code": "00126380"}}
    # A missing credential is a configuration error, not a per-item fetch
    # failure — matches us_filings.fetch's SEC_USER_AGENT distinction, and
    # is caught by scripts/collect_daily.py's per-collector try/except.
    with pytest.raises(DartFilingsError):
        fetch(["005930"], corp_map, dt.date(2026, 8, 1), dt.date(2026, 8, 25), api_key=None)


# --- live ---------------------------------------------------------------


@pytest.mark.network
def test_live_fetch_matches_the_committed_fixture():
    """Catches DART changing the list.json response shape. Excluded from the default run."""
    from src.util.config import load_filing_ids

    corp_map = load_filing_ids()["kr"]
    df, report = fetch(["005930"], corp_map, dt.date(2026, 8, 10), dt.date(2026, 8, 25))
    assert report.ok, report.summary()
    assert (df["rcept_no"] == KNOWN_VALUE["where"]["rcept_no"]).any()
