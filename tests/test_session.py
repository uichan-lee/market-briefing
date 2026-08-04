"""Tests for market-session and time handling.

All offline — the calendar library ships its own holiday data, so none of these
touch the network.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from src.util.session import (
    KST,
    NoSessionFoundError,
    UnknownMarketError,
    is_trading_day,
    next_tradeable_open,
    next_trading_day,
    previous_trading_day,
    session_close_utc,
    session_open_utc,
    to_kst,
    to_utc,
    trading_days,
)

# --- the DST rule CLAUDE.md singles out ----------------------------------


def test_us_close_is_0500_kst_during_dst():
    close = session_close_utc("US", dt.date(2026, 7, 15))
    assert to_kst(close).strftime("%H:%M") == "05:00"


def test_us_close_is_0600_kst_outside_dst():
    close = session_close_utc("US", dt.date(2026, 1, 15))
    assert to_kst(close).strftime("%H:%M") == "06:00"


def test_us_close_kst_hour_actually_differs_across_the_year():
    """Guards against a calendar that silently ignores DST altogether."""
    summer = to_kst(session_close_utc("US", dt.date(2026, 7, 15))).hour
    winter = to_kst(session_close_utc("US", dt.date(2026, 1, 15))).hour
    assert summer != winter


def test_kr_session_is_0900_to_1530_kst():
    day = dt.date(2026, 1, 5)
    assert to_kst(session_open_utc("KR", day)).strftime("%H:%M") == "09:00"
    assert to_kst(session_close_utc("KR", day)).strftime("%H:%M") == "15:30"


# --- Korean holidays, including lunar ones -------------------------------


@pytest.mark.parametrize(
    ("day", "label"),
    [
        (dt.date(2026, 2, 17), "Seollal 2026"),
        (dt.date(2026, 9, 25), "Chuseok 2026"),
        (dt.date(2026, 1, 1), "New Year's Day"),
    ],
)
def test_korean_market_holidays_are_not_trading_days(day, label):
    assert not is_trading_day("KR", day), f"{label} ({day}) should be a market holiday"


def test_seollal_closure_spans_multiple_days():
    """Lunar holidays shift yearly; hardcoding them would rot. Verify the run."""
    days = trading_days("KR", dt.date(2026, 2, 16), dt.date(2026, 2, 18))
    assert days == []


def test_weekend_is_not_a_trading_day():
    assert not is_trading_day("KR", dt.date(2026, 1, 3))  # Saturday
    assert not is_trading_day("US", dt.date(2026, 1, 4))  # Sunday


def test_trading_days_excludes_holidays_within_a_range():
    days = trading_days("KR", dt.date(2026, 1, 1), dt.date(2026, 1, 9))
    assert dt.date(2026, 1, 1) not in days
    assert dt.date(2026, 1, 5) in days


# --- adjacency -----------------------------------------------------------


def test_next_trading_day_skips_the_seollal_run():
    assert next_trading_day("KR", dt.date(2026, 2, 13)) == dt.date(2026, 2, 19)


def test_previous_trading_day_skips_the_seollal_run():
    assert previous_trading_day("KR", dt.date(2026, 2, 19)) == dt.date(2026, 2, 13)


def test_adjacency_is_strict_not_inclusive():
    day = dt.date(2026, 1, 6)
    assert next_trading_day("KR", day) > day
    assert previous_trading_day("KR", day) < day


# --- the look-ahead rule -------------------------------------------------


def test_news_before_the_open_is_tradeable_at_that_days_open():
    published = pd.Timestamp("2026-01-06 06:00", tz=KST)  # 3h before the 09:00 open
    assert to_kst(next_tradeable_open("KR", published)) == pd.Timestamp("2026-01-06 09:00", tz=KST)


def test_intraday_news_is_tradeable_only_at_the_next_sessions_open():
    published = pd.Timestamp("2026-01-06 11:00", tz=KST)  # mid-session
    assert to_kst(next_tradeable_open("KR", published)) == pd.Timestamp("2026-01-07 09:00", tz=KST)


def test_post_close_news_is_tradeable_at_the_next_sessions_open():
    published = pd.Timestamp("2026-01-06 20:00", tz=KST)
    assert to_kst(next_tradeable_open("KR", published)) == pd.Timestamp("2026-01-07 09:00", tz=KST)


def test_news_published_during_a_holiday_run_waits_for_the_reopen():
    published = pd.Timestamp("2026-02-17 10:00", tz=KST)  # Seollal
    assert to_kst(next_tradeable_open("KR", published)) == pd.Timestamp("2026-02-19 09:00", tz=KST)


def test_tradeable_open_is_never_before_publication():
    """The property that actually matters: no look-ahead, ever."""
    for stamp in [
        "2026-01-05 00:01",
        "2026-01-06 09:00",
        "2026-01-06 15:29",
        "2026-02-17 23:59",
        "2026-09-25 12:00",
    ]:
        published = pd.Timestamp(stamp, tz=KST)
        assert next_tradeable_open("KR", published) >= to_utc(published)


# --- timezone discipline -------------------------------------------------


def test_naive_timestamps_are_rejected_rather_than_guessed():
    with pytest.raises(ValueError, match="timezone-naive"):
        to_utc(pd.Timestamp("2026-01-06 09:00"))


def test_naive_timestamp_rejected_by_next_tradeable_open():
    with pytest.raises(ValueError, match="timezone-naive"):
        next_tradeable_open("KR", dt.datetime(2026, 1, 6, 9, 0))


def test_kst_and_utc_describe_the_same_instant():
    stamp = pd.Timestamp("2026-01-06 09:00", tz=KST)
    assert to_utc(stamp) == stamp
    assert to_kst(stamp) == stamp


# --- error surfaces ------------------------------------------------------


def test_unknown_market_raises():
    with pytest.raises(UnknownMarketError):
        is_trading_day("JP", dt.date(2026, 1, 6))


def test_session_times_on_a_holiday_raise_rather_than_return_a_wrong_value():
    with pytest.raises(NoSessionFoundError):
        session_close_utc("KR", dt.date(2026, 2, 17))
    with pytest.raises(NoSessionFoundError):
        session_open_utc("KR", dt.date(2026, 2, 17))


# --- calendar corrections -------------------------------------------------


def test_krx_closures_the_library_misses_are_excluded():
    """2026-06-03 (지방선거) and 2026-07-17 (제헌절) were not sessions.

    Both are days pandas_market_calendars 5.4.0 reports as open. Election days
    move every cycle and 제헌절 was restored in 2026 after eighteen years, so
    neither is encoded upstream. Left uncorrected, every continuity check
    reports a gap that is not there.
    """
    assert not is_trading_day("KR", dt.date(2026, 6, 3))
    assert not is_trading_day("KR", dt.date(2026, 7, 17))
    assert dt.date(2026, 6, 3) not in trading_days("KR", dt.date(2026, 6, 1), dt.date(2026, 6, 5))
    assert dt.date(2026, 7, 17) not in trading_days(
        "KR", dt.date(2026, 7, 13), dt.date(2026, 7, 20)
    )


def test_a_corrected_day_is_skipped_when_stepping_between_sessions():
    """The correction has to reach every navigation helper, not just the list.

    2026-07-17 was a Friday, so the session after Thursday the 16th is Monday
    the 20th. A helper that stepped onto the 17th would stamp news as tradeable
    on a day the market never opened.
    """
    assert next_trading_day("KR", dt.date(2026, 7, 16)) == dt.date(2026, 7, 20)
    assert previous_trading_day("KR", dt.date(2026, 7, 20)) == dt.date(2026, 7, 16)


def test_corrections_only_remove_and_never_add():
    """Removal loses coverage; addition invents a session that never happened.

    Only the first is safe, so the structure must make the second impossible.
    """
    from src.util.session import _CALENDAR_CORRECTIONS, _calendar

    for market, wrong in _CALENDAR_CORRECTIONS.items():
        if not wrong:
            continue
        raw = _calendar(market).schedule(start_date=min(wrong), end_date=max(wrong))
        library_days = {ts.date() for ts in raw.index}
        assert wrong <= library_days, (
            f"{market}: {sorted(wrong - library_days)} are not sessions the library "
            "reports, so removing them corrects nothing and may hide a real bug"
        )


@pytest.mark.network
def test_the_calendar_corrections_still_match_krx():
    """Re-derive the correction list against KRX itself.

    The list is hand-maintained, so it rots: the libraries will eventually ship
    these dates, and Korea will add ad-hoc closures nobody encoded. KRX is the
    only authority, and OHLCV exists only on days it actually traded. This fails
    when the list drifts either way rather than leaving it quietly wrong.
    """
    from pykrx import stock

    start, end = dt.date(2026, 1, 1), dt.date(2026, 8, 4)
    raw = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "005930")
    actual = {d.date() for d in raw.index}
    ours = set(trading_days("KR", start, end))

    assert not (ours - actual), (
        f"days this project calls sessions that KRX did not trade: {sorted(ours - actual)} "
        "— add them to _CALENDAR_CORRECTIONS"
    )
    assert not (actual - ours), (
        f"days KRX traded that this project calls closed: {sorted(actual - ours)} "
        "— a correction has gone stale and is now removing a real session"
    )
