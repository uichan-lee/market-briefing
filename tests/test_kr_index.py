"""Tests for the KODEX 200 benchmark collector (SPEC §3.1, PREREGISTRATION §8.5).

Offline against a committed pykrx payload captured from the live ETF endpoint on
2026-08-13, so normalization and validation run without a network call. The one
live test is marked ``network`` and excluded from the default run.

This collector exists only to make the 3-month gate readable — nothing in the
pipeline consumes it — so the tests that matter most are the ones pinning that
it stays *out* of the pipeline: its own directory, its own ticker, and a schema
that is deliberately not `kr_price`'s.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from src.collectors import kr_index
from src.collectors.kr_index import (
    BENCHMARK_TICKER,
    KNOWN_VALUE,
    SCHEMA,
    _normalize,
    validate_frame,
)
from src.util.session import session_close_utc

FIXTURE = Path(__file__).parent / "fixtures" / "pykrx_etf_069500.json"

START = dt.date(2024, 1, 2)
END = dt.date(2024, 1, 19)


@pytest.fixture
def raw() -> pd.DataFrame:
    """The payload exactly as pykrx returns it: Korean columns, DatetimeIndex."""
    payload = json.loads(FIXTURE.read_text())
    frame = pd.DataFrame(
        payload["data"],
        columns=payload["columns"],
        index=pd.to_datetime(payload["index"]),
    )
    frame.index.name = "날짜"
    return frame


@pytest.fixture
def frame(raw: pd.DataFrame) -> pd.DataFrame:
    df = _normalize(raw)
    return df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})


# --- the fixture itself ---------------------------------------------------


def test_the_fixture_is_in_pykrx_etf_shape(raw):
    """If this fails the fixture was re-captured wrongly and every other test lies.

    The ETF endpoint's columns are not the stock endpoint's: no 등락률, plus NAV
    and 기초지수. That difference is the whole reason this collector is separate
    from kr_price, so it is asserted rather than assumed.
    """
    assert list(raw.columns) == [
        "NAV",
        "시가",
        "고가",
        "저가",
        "종가",
        "거래량",
        "거래대금",
        "기초지수",
    ]
    assert "등락률" not in raw.columns
    assert len(raw) == 14


# --- normalization --------------------------------------------------------


def test_normalize_produces_exactly_the_committed_schema(frame):
    assert list(frame.columns) == list(SCHEMA)
    assert (frame["ticker"] == BENCHMARK_TICKER).all()


def test_nav_and_trading_value_are_dropped(frame):
    """Neither is needed to compute a return, and carrying a column nobody reads
    invites somebody to start reading it."""
    assert "NAV" not in frame.columns
    assert "거래대금" not in frame.columns


def test_the_index_level_survives_normalization(frame):
    """`기초지수` is the one extra column kept, because the known-value check
    cross-references it against a second KRX endpoint."""
    assert frame.loc[frame["date"] == pd.Timestamp("2024-01-02"), "index_level"].iloc[0] == 360.55


def test_prices_are_integers_not_the_wire_unsigned_types(frame):
    """pykrx hands back uint32/uint64. Left alone, concatenating this with a
    kr_price frame would produce a dtype surprise rather than an error."""
    for column in ("open", "high", "low", "close", "volume"):
        assert frame[column].dtype == "int64"


def test_known_at_is_the_kr_session_close(frame):
    first = frame.iloc[0]
    assert pd.Timestamp(first["known_at_utc"]) == session_close_utc("KR", first["date"].date())


def test_an_empty_payload_yields_the_schema_not_a_crash():
    out = _normalize(pd.DataFrame())
    assert list(out.columns) == list(SCHEMA)
    assert out.empty


# --- validation -----------------------------------------------------------


def test_a_clean_frame_passes_every_check(frame):
    report = validate_frame(frame, START, END)
    assert report.ok, report.summary()


def test_the_known_value_ties_two_krx_endpoints_together():
    """The check reads `index_level`, not `close`, on purpose.

    360.55 is what `get_index_ohlcv_by_date(..., "1028")` publishes as the KOSPI
    200 close for 2024-01-02, and what this ETF endpoint reports as 기초지수 for
    the same session. A value taken from the endpoint it validates would only
    ever catch pykrx changing its mind.
    """
    assert KNOWN_VALUE["column"] == "index_level"
    assert KNOWN_VALUE["expected"] == 360.55
    assert KNOWN_VALUE["where"]["ticker"] == BENCHMARK_TICKER


def test_a_mismapped_rename_is_caught_by_the_known_value(frame):
    """The failure this check mainly exists for: 기초지수 mapped to the wrong
    column would still produce a well-formed frame."""
    broken = frame.copy()
    broken["index_level"] = broken["close"].astype("float64")
    report = validate_frame(broken, START, END)
    assert not report.ok
    assert any("known_value" in check.name for check in report.failures)


def test_an_empty_frame_over_real_sessions_is_a_failure():
    """pykrx fails by returning an empty frame, so absence must be a failure
    rather than 'no data'."""
    empty = pd.DataFrame(columns=list(SCHEMA))
    report = validate_frame(empty, START, END, known_value=False)
    assert not report.ok
    assert any(check.name == "not_empty" for check in report.failures)


def test_a_missing_session_is_reported(frame):
    dropped = frame[frame["date"] != pd.Timestamp("2024-01-08")]
    report = validate_frame(dropped, START, END, known_value=False)
    assert not report.ok
    assert any("trading_day_continuity" in check.name for check in report.failures)


def test_a_hole_in_a_price_column_fails(frame):
    holed = frame.copy()
    holed.loc[holed.index[0], "close"] = pd.NA
    report = validate_frame(holed, START, END, known_value=False)
    assert not report.ok
    assert any("missing_ratio" in check.name for check in report.failures)


# --- the boundary it must not cross --------------------------------------


def test_the_benchmark_is_not_in_the_watchlist():
    """069500 must never reach `compute()`, `rate()`, or the ⑥ table.

    Every KR fetcher takes its symbols from `load_watchlist`, and everything
    downstream reads the same list — so adding the benchmark there to get its
    prices would put an index ETF in the briefing as a stock to hold an opinion
    about. This collector exists so that never has to happen.
    """
    from src.util.config import load_watchlist

    assert BENCHMARK_TICKER not in {entry.ticker for entry in load_watchlist()}


# --- live -----------------------------------------------------------------


@pytest.mark.network
def test_the_live_endpoint_still_answers_in_the_shape_we_parse():
    df, report = kr_index.fetch(START, END)
    assert report.ok, report.summary()
    assert list(df.columns) == list(SCHEMA)
    assert validate_frame(df, START, END).ok
