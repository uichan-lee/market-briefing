"""Tests for the KR price collector (SPEC §3.1).

The offline tests run against a committed pykrx payload in
``tests/fixtures/``, so the normalization and validation paths are exercised
without a network call. The one live test is marked ``network`` and excluded
from the default run.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from src.collectors.kr_price import (
    KNOWN_VALUE,
    SCHEMA,
    _normalize,
    validate_frame,
)
from src.util.session import session_close_utc

FIXTURE = Path(__file__).parent / "fixtures" / "kr_price_005930_202401.csv"

# The fixture covers a full calendar month, so continuity has something to check.
START = dt.date(2024, 1, 2)
END = dt.date(2024, 1, 31)


@pytest.fixture
def raw() -> pd.DataFrame:
    """The payload exactly as pykrx returns it: Korean columns, DatetimeIndex."""
    return pd.read_csv(FIXTURE, index_col="날짜", parse_dates=["날짜"])


@pytest.fixture
def frame(raw: pd.DataFrame) -> pd.DataFrame:
    df = _normalize(raw, "005930")
    return df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})


# --- the fixture itself ---------------------------------------------------


def test_the_fixture_is_in_pykrx_shape(raw):
    """If this fails the fixture was re-captured wrongly and every other test lies."""
    assert list(raw.columns) == ["시가", "고가", "저가", "종가", "거래량", "등락률"]
    assert raw.index.name == "날짜"


# --- normalization --------------------------------------------------------


def test_korean_columns_are_mapped_to_the_committed_schema(frame):
    assert list(frame.columns) == list(SCHEMA)


def test_dtypes_match_the_declared_schema(frame):
    for column, want in SCHEMA.items():
        assert str(frame[column].dtype) == want, column


def test_the_known_value_survives_normalization(frame):
    """005930 closed at 79,600 on 2024-01-02 — cross-checked against Naver
    Finance, which sources independently of KRX."""
    row = frame[frame["date"] == pd.Timestamp("2024-01-02")]
    assert len(row) == 1
    assert int(row["close"].iloc[0]) == 79_600
    assert int(row["volume"].iloc[0]) == 17_142_847


def test_ohlc_relationships_hold(frame):
    """A rename that crossed two columns would still pass a schema check."""
    assert (frame["high"] >= frame["low"]).all()
    assert (frame["high"] >= frame["open"]).all()
    assert (frame["high"] >= frame["close"]).all()
    assert (frame["low"] <= frame["open"]).all()
    assert (frame["low"] <= frame["close"]).all()


def test_known_at_utc_is_the_session_close_not_the_date(frame):
    """The look-ahead rule is enforced against this column, so it must be the
    instant the bar became knowable, not midnight of the trading date."""
    row = frame[frame["date"] == pd.Timestamp("2024-01-02")].iloc[0]
    assert row["known_at_utc"] == session_close_utc("KR", dt.date(2024, 1, 2))
    assert row["known_at_utc"] > pd.Timestamp("2024-01-02", tz="UTC")


def test_known_at_utc_is_timezone_aware(frame):
    assert frame["known_at_utc"].dt.tz is not None


def test_normalizing_an_empty_frame_yields_the_schema_not_a_crash():
    """pykrx returns an empty frame on a failed request, so this path is real."""
    empty = _normalize(pd.DataFrame(), "005930")
    assert list(empty.columns) == list(SCHEMA)
    assert empty.empty


# --- validation -----------------------------------------------------------


def test_a_clean_frame_passes_every_check(frame):
    report = validate_frame(frame, ["005930"], START, END)
    assert report.ok, report.summary()


def test_the_known_value_check_is_wired_to_the_real_column(frame):
    """Guards against KNOWN_VALUE drifting away from the schema."""
    assert KNOWN_VALUE["column"] in frame.columns
    report = validate_frame(frame, ["005930"], START, END)
    assert any(r.name == "known_value" and r.passed for r in report.results)


def test_a_wrong_price_is_caught(frame):
    tampered = frame.copy()
    tampered.loc[tampered["date"] == pd.Timestamp("2024-01-02"), "close"] = 1
    report = validate_frame(tampered, ["005930"], START, END)
    assert not report.ok
    assert any(r.name == "known_value" for r in report.failures)


def test_a_missing_trading_day_is_caught(frame):
    gapped = frame[frame["date"] != pd.Timestamp("2024-01-15")]
    report = validate_frame(gapped, ["005930"], START, END)
    assert not report.ok
    assert any("trading_day_continuity" in r.name for r in report.failures)


def test_an_empty_frame_is_a_failure_not_an_empty_result():
    """The single most important check here. pykrx answers a failed request with
    an empty frame, and CLAUDE.md calls silent failure the worst outcome."""
    empty = _normalize(pd.DataFrame(), "005930")
    report = validate_frame(empty, ["005930"], START, END, known_value=False)

    assert not report.ok
    failure = next(r for r in report.failures if r.name == "not_empty")
    assert "trading days" in failure.detail


def test_an_empty_frame_over_a_market_holiday_range_is_not_flagged():
    """Seollal 2024 ran Feb 9-12; a range inside it has no trading days, so an
    empty result is correct rather than a failure."""
    empty = _normalize(pd.DataFrame(), "005930")
    report = validate_frame(
        empty, ["005930"], dt.date(2024, 2, 10), dt.date(2024, 2, 11), known_value=False
    )
    assert all(r.passed for r in report.results if r.name == "not_empty")


def test_a_missing_close_is_caught(frame):
    holed = frame.copy()
    holed.loc[holed.index[3], "close"] = None
    report = validate_frame(holed, ["005930"], START, END, known_value=False)
    assert not report.ok
    assert any(r.name == "missing_ratio" for r in report.failures)


def test_continuity_is_reported_per_ticker(frame):
    """Two tickers means two rows per day; an aggregate check would read them
    as duplicates and fail a correct frame."""
    other = frame.copy()
    other["ticker"] = "000660"
    both = pd.concat([frame, other], ignore_index=True)

    report = validate_frame(both, ["005930", "000660"], START, END, known_value=False)
    named = [r.name for r in report.results]
    assert "trading_day_continuity[005930]" in named
    assert "trading_day_continuity[000660]" in named
    assert report.ok, report.summary()


# --- live ------------------------------------------------------------------


@pytest.mark.network
def test_live_fetch_matches_the_committed_fixture():
    """The fixture is a snapshot; this is what catches pykrx or KRX changing
    under it. Excluded from the default run."""
    from src.collectors.kr_price import fetch

    df, report = fetch(["005930"], START, END, sleep_seconds=0)
    assert report.ok, report.summary()

    row = df[df["date"] == pd.Timestamp("2024-01-02")].iloc[0]
    assert int(row["close"]) == KNOWN_VALUE["expected"]
