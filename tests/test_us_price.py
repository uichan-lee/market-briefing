"""Tests for the US price collector (SPEC §3.2).

The fixture is a real Tiingo response for SPY over 2024-01-02..2024-01-19,
committed rather than mocked. That window spans two market holidays — New Year's
Day and MLK Day — so continuity is checked against holidays the calendar library
actually knows about, not invented ones.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from src.collectors.us_price import (
    INDEX_ETFS,
    KNOWN_VALUE,
    SCHEMA,
    TiingoError,
    fetch,
    normalize,
    validate_frame,
)
from src.util.session import session_close_utc

FIXTURES = Path(__file__).parent / "fixtures"
START = dt.date(2024, 1, 2)
END = dt.date(2024, 1, 19)


@pytest.fixture
def rows() -> list[dict]:
    return json.loads((FIXTURES / "tiingo_spy.json").read_text())


@pytest.fixture
def frame(rows) -> pd.DataFrame:
    df = normalize(rows, "SPY")
    return df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})


# --- normalization --------------------------------------------------------


def test_the_committed_schema_is_produced(frame):
    assert list(frame.columns) == list(SCHEMA)
    assert len(frame) == 13


def test_a_date_is_a_date_not_a_midnight_instant(frame):
    """Tiingo sends 2024-01-02T00:00:00.000Z for a session that ran 14:30–21:00
    UTC. Keeping that instant would place every bar 21 hours before the session
    it describes."""
    assert frame["date"].iloc[0] == pd.Timestamp("2024-01-02")
    assert frame["date"].dtype == "datetime64[s]"


def test_known_at_is_the_session_close_not_the_date(frame):
    """The look-ahead rule is enforced on this column. It must land after the
    bar exists, which is the close — not midnight, when it does not."""
    expected = session_close_utc("US", dt.date(2024, 1, 2))
    assert frame["known_at_utc"].iloc[0] == expected
    assert expected.hour == 21  # 16:00 ET in January, outside DST


def test_adjusted_and_unadjusted_closes_are_both_kept(frame):
    """They differ by accumulated dividends. Returns need the adjusted one; the
    known-value check needs the one that never gets restated."""
    assert frame["close"].iloc[0] != frame["adj_close"].iloc[0]


def test_a_response_missing_adj_close_is_an_error_not_a_gap(rows):
    """Filling it silently would understate every return by the next dividend."""
    stripped = [{k: v for k, v in row.items() if k != "adjClose"} for row in rows]
    with pytest.raises(TiingoError, match="adj_close"):
        normalize(stripped, "SPY")


def test_an_empty_response_normalizes_rather_than_raising():
    assert normalize([], "SPY").empty


# --- validation -----------------------------------------------------------


def test_a_clean_frame_passes_every_check(frame):
    report = validate_frame(frame, ["SPY"], START, END)
    assert report.ok, report.summary()


def test_the_pinned_known_value_matches_the_fixture(frame):
    """KNOWN_VALUE is the fourth check. If this fails, either the pin is wrong or
    the column mapping moved — both are the failure it exists to catch."""
    row = frame[frame["date"] == pd.Timestamp(KNOWN_VALUE["where"]["date"])]
    assert row[KNOWN_VALUE["column"]].iloc[0] == pytest.approx(KNOWN_VALUE["expected"])


def test_a_shifted_price_column_is_caught(frame):
    tampered = frame.copy()
    tampered.loc[tampered.index[0], "close"] = 1.0
    report = validate_frame(tampered, ["SPY"], START, END)
    assert not report.ok


def test_a_missing_trading_day_is_caught(frame):
    holed = frame.drop(index=frame.index[3]).reset_index(drop=True)
    report = validate_frame(holed, ["SPY"], START, END, known_value=False)
    assert not report.ok


def test_market_holidays_are_not_reported_as_gaps(frame):
    """MLK Day falls inside the window and is absent from the data. The calendar
    has to account for it, or every US holiday reads as missing data."""
    assert dt.date(2024, 1, 15) not in set(frame["date"].dt.date)
    report = validate_frame(frame, ["SPY"], START, END, known_value=False)
    assert report.ok, report.summary()


def test_an_empty_frame_over_trading_days_is_a_failure():
    empty = pd.DataFrame(columns=list(SCHEMA))
    report = validate_frame(empty, ["SPY"], START, END, known_value=False)
    assert not report.ok
    assert any(r.name == "not_empty" and not r.passed for r in report.results)


def test_continuity_is_checked_per_ticker(frame):
    """Two tickers over the same days would look like duplicated dates in
    aggregate, so each is checked against the calendar separately."""
    other = frame.copy()
    other["ticker"] = "QQQ"
    both = pd.concat([frame, other], ignore_index=True)
    report = validate_frame(both, ["SPY", "QQQ"], START, END, known_value=False)
    assert report.ok, report.summary()
    assert sum("continuity" in r.name for r in report.results) == 2


# --- fetching -------------------------------------------------------------


def test_a_missing_key_is_raised_not_reported(monkeypatch):
    """Every ticker would fail identically, so this is a configuration error
    rather than the partial-data condition the report exists to describe."""
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    with pytest.raises(TiingoError, match="TIINGO_API_KEY"):
        fetch(["SPY"], START, END)


def test_the_transmission_map_covers_what_the_report_reads_across():
    """SPEC §2.2① names these explicitly. A dropped entry silently removes a
    row from the first section of the briefing."""
    assert {"SPY", "QQQ", "IWM", "SMH", "XLK", "XLE", "XLF", "XBI"} <= set(INDEX_ETFS)
    assert all(INDEX_ETFS.values())


# --- live -----------------------------------------------------------------


@pytest.mark.network
def test_the_live_api_still_answers_in_the_shape_we_parse():
    df, report = fetch(["SPY"], START, END)
    assert report.ok, report.summary()
    assert len(df) == 13
