"""Tests for the collector validation framework.

Synthetic frames only — no network, no collector.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from src.collectors.validate import (
    ValidationFailedError,
    check_known_value,
    check_missing_ratio,
    check_schema,
    check_trading_day_continuity,
    validate,
)

# A text column is `object` under pandas 2.x and `str` under 3.x. pyproject.toml
# pins pandas <3.0 because pykrx requires it, so `object` is correct here — and
# this line is the first thing to change when that pin is lifted.
SCHEMA = {"date": "datetime64[s]", "ticker": "object", "close": "float64"}


def frame(dates: list[dt.date], ticker: str = "005930", close: float = 55_000.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "ticker": [ticker] * len(dates),
            "close": [close] * len(dates),
        }
    )


# 2026-01-05..09 is a full Mon-Fri KRX trading week (Jan 1 holiday is outside it).
WEEK = [dt.date(2026, 1, d) for d in (5, 6, 7, 8, 9)]


# --- check 1: schema -----------------------------------------------------


def test_schema_passes_on_a_matching_frame():
    assert check_schema(frame(WEEK), SCHEMA).passed


def test_schema_detects_a_missing_column():
    result = check_schema(frame(WEEK).drop(columns=["close"]), SCHEMA)
    assert not result.passed
    assert "missing columns" in result.detail


def test_schema_detects_an_unannounced_extra_column():
    df = frame(WEEK).assign(surprise=1)
    result = check_schema(df, SCHEMA)
    assert not result.passed
    assert "unexpected columns" in result.detail


def test_schema_detects_a_wrong_dtype():
    df = frame(WEEK).astype({"close": "int64"})
    result = check_schema(df, SCHEMA)
    assert not result.passed
    assert "dtype" in result.detail


def test_schema_ignores_datetime_resolution():
    """pandas picks the unit from how the column was built; that isn't a defect."""
    df = frame(WEEK).astype({"date": "datetime64[ns]"})
    assert check_schema(df, SCHEMA).passed
    assert check_schema(df, {**SCHEMA, "date": "datetime64[us]"}).passed


def test_schema_still_rejects_a_naive_column_where_utc_was_declared():
    """Resolution is forgiven; timezone-awareness is not — storage must be UTC."""
    result = check_schema(frame(WEEK), {**SCHEMA, "date": "datetime64[ns, UTC]"})
    assert not result.passed
    assert "date dtype" in result.detail


def test_schema_rejects_a_tz_aware_column_where_naive_was_declared():
    df = frame(WEEK)
    df["date"] = df["date"].dt.tz_localize("UTC")
    result = check_schema(df, SCHEMA)
    assert not result.passed
    assert "date dtype" in result.detail


# --- check 2: missing ratio ----------------------------------------------


def test_missing_ratio_passes_when_complete():
    assert check_missing_ratio(frame(WEEK), {"close": 0.0}).passed


def test_missing_ratio_fails_above_threshold():
    df = frame(WEEK)
    df.loc[0:2, "close"] = None  # 3 of 5 missing
    result = check_missing_ratio(df, {"close": 0.1})
    assert not result.passed
    assert "60.0%" in result.detail


def test_missing_ratio_passes_below_threshold():
    df = frame(WEEK)
    df.loc[0, "close"] = None  # 1 of 5 = 20%
    assert check_missing_ratio(df, {"close": 0.25}).passed


def test_missing_ratio_treats_an_empty_frame_as_failure():
    """An empty frame trivially has no missing values; that must not read as ok."""
    result = check_missing_ratio(frame([]), {"close": 0.0})
    assert not result.passed
    assert "empty" in result.detail


# --- check 3: trading-day continuity -------------------------------------


def test_continuity_passes_on_a_full_week():
    result = check_trading_day_continuity(
        frame(WEEK), "KR", "date", dt.date(2026, 1, 5), dt.date(2026, 1, 9)
    )
    assert result.passed


def test_continuity_detects_a_dropped_day():
    result = check_trading_day_continuity(
        frame([d for d in WEEK if d.day != 7]),
        "KR",
        "date",
        dt.date(2026, 1, 5),
        dt.date(2026, 1, 9),
    )
    assert not result.passed
    assert "2026-01-07" in result.detail


def test_continuity_flags_rows_on_days_the_market_was_closed():
    """A row on Seollal means the collector invented data."""
    result = check_trading_day_continuity(
        frame([dt.date(2026, 2, 13), dt.date(2026, 2, 17), dt.date(2026, 2, 19)]),
        "KR",
        "date",
        dt.date(2026, 2, 13),
        dt.date(2026, 2, 19),
    )
    assert not result.passed
    assert "non-trading days" in result.detail


def test_continuity_ignores_holidays_rather_than_reporting_them_as_gaps():
    """The Seollal run is absent from the data and must not count as missing."""
    result = check_trading_day_continuity(
        frame([dt.date(2026, 2, 13), dt.date(2026, 2, 19), dt.date(2026, 2, 20)]),
        "KR",
        "date",
        dt.date(2026, 2, 13),
        dt.date(2026, 2, 20),
    )
    assert result.passed


def test_continuity_detects_duplicated_dates():
    result = check_trading_day_continuity(
        frame([*WEEK, dt.date(2026, 1, 7)]),
        "KR",
        "date",
        dt.date(2026, 1, 5),
        dt.date(2026, 1, 9),
    )
    assert not result.passed
    assert "duplicated" in result.detail


# --- check 4: known value ------------------------------------------------


def test_known_value_passes_on_an_exact_match():
    result = check_known_value(
        frame(WEEK), {"date": dt.date(2026, 1, 6), "ticker": "005930"}, "close", 55_000.0
    )
    assert result.passed


def test_known_value_fails_on_a_wrong_number():
    result = check_known_value(
        frame(WEEK), {"date": dt.date(2026, 1, 6), "ticker": "005930"}, "close", 61_000.0
    )
    assert not result.passed
    assert "55000" in result.detail


def test_known_value_respects_tolerance():
    where = {"date": dt.date(2026, 1, 6), "ticker": "005930"}
    assert check_known_value(frame(WEEK), where, "close", 55_010.0, tolerance=50).passed
    assert not check_known_value(frame(WEEK), where, "close", 55_010.0, tolerance=1).passed


def test_known_value_fails_when_the_selector_matches_no_rows():
    result = check_known_value(
        frame(WEEK), {"date": dt.date(2026, 1, 1), "ticker": "005930"}, "close", 55_000.0
    )
    assert not result.passed
    assert "matched 0 rows" in result.detail


def test_known_value_fails_on_a_null_rather_than_passing():
    """`nan > tolerance` is False, so the null row used to report *passed*.

    This is check 4, the one CLAUDE.md requires against a collector returning
    well-formed but wrong numbers, and a null is the wrongest number there is.
    """
    df = frame(WEEK)
    df.loc[df["date"] == pd.Timestamp(dt.date(2026, 1, 6)), "close"] = float("nan")
    result = check_known_value(
        df, {"date": dt.date(2026, 1, 6), "ticker": "005930"}, "close", 55_000.0
    )
    assert not result.passed
    assert "null" in result.detail


def test_known_value_still_fails_on_an_infinity():
    """The sibling case, which never regressed: inf compares greater than any
    tolerance on its own. Pinned so the null fix cannot be written in a way that
    accidentally forgives it."""
    df = frame(WEEK)
    df.loc[df["date"] == pd.Timestamp(dt.date(2026, 1, 6)), "close"] = float("inf")
    result = check_known_value(
        df, {"date": dt.date(2026, 1, 6), "ticker": "005930"}, "close", 55_000.0
    )
    assert not result.passed


def test_known_value_fails_when_the_selector_is_ambiguous():
    df = pd.concat([frame(WEEK), frame(WEEK)], ignore_index=True)
    result = check_known_value(
        df, {"date": dt.date(2026, 1, 6), "ticker": "005930"}, "close", 55_000.0
    )
    assert not result.passed
    assert "matched 2 rows" in result.detail


# --- aggregation ---------------------------------------------------------


def test_report_records_failures_instead_of_raising():
    """The property the pipeline depends on: a bad collector reports, not crashes."""
    report = validate(
        "kr_price",
        [
            check_schema(frame(WEEK), SCHEMA),
            check_known_value(
                frame(WEEK), {"date": dt.date(2026, 1, 6), "ticker": "005930"}, "close", 1.0
            ),
        ],
    )
    assert not report.ok
    assert len(report.failures) == 1
    assert "kr_price" in report.summary()
    assert "known_value" in report.summary()


def test_report_ok_when_every_check_passes():
    report = validate("kr_price", [check_schema(frame(WEEK), SCHEMA)])
    assert report.ok
    assert "1 checks passed" in report.summary()


def test_raise_if_failed_is_opt_in():
    passing = validate("kr_price", [check_schema(frame(WEEK), SCHEMA)])
    passing.raise_if_failed()  # no exception

    failing = validate("kr_price", [check_schema(frame(WEEK).drop(columns=["close"]), SCHEMA)])
    with pytest.raises(ValidationFailedError, match="kr_price"):
        failing.raise_if_failed()


def test_empty_report_is_not_silently_ok():
    report = validate("kr_price", [])
    assert "no checks run" in report.summary()
