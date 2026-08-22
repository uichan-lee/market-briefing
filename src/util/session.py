"""Market-session and time utilities.

Every collector and feature depends on this module. CLAUDE.md is prescriptive
about the rules it implements:

- Everything is stored in UTC and displayed in KST.
- Market sessions come from ``pandas_market_calendars``. Holidays and DST
  transitions are never hardcoded — in particular, the US close is 05:00 KST
  under DST and 06:00 KST outside it, and that value is *derived* here rather
  than assumed.
- News published during a session is tradeable at the next session's open.

The calendar API used below (calendar names ``XKRX``/``XNYS``, the
``market_open``/``market_close`` schedule columns, and their tz-aware UTC dtype)
was verified empirically against pandas_market_calendars 5.4.0 rather than
written from memory.
"""

from __future__ import annotations

import datetime as dt
import warnings
from functools import cache
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

UTC = ZoneInfo("UTC")
KST = ZoneInfo("Asia/Seoul")

Market = Literal["KR", "US"]

_CALENDAR_NAMES: dict[str, str] = {"KR": "XKRX", "US": "XNYS"}

# A holiday run long enough to matter: Korean Seollal/Chuseok closures can span
# five or more calendar days once weekends are included. Lookahead windows below
# use this so a search never silently falls off the end of a short window.
_LOOKAHEAD_DAYS = 21


class UnknownMarketError(ValueError):
    """Raised for a market code other than 'KR' or 'US'."""


class NoSessionFoundError(RuntimeError):
    """Raised when no trading session exists in the searched window."""


@cache
def _calendar(market: Market):
    """Return the cached market calendar for ``market``.

    Cached because ``mcal.get_calendar`` builds a fresh object on every call,
    which is both wasteful and re-emits XKRX's construction warning each time.
    """
    try:
        name = _CALENDAR_NAMES[market]
    except KeyError:
        raise UnknownMarketError(
            f"unknown market {market!r}; expected one of {sorted(_CALENDAR_NAMES)}"
        ) from None

    with warnings.catch_warnings():
        # XKRX warns that its `break_start`/`break_end` columns are discontinued
        # (KRX dropped the lunch break in 2000). This module never reads those
        # columns, so the warning is noise. Scoped to construction only, and
        # deliberately narrow — it does not suppress anything else.
        warnings.filterwarnings(
            "ignore",
            message=r".*break_start.*break_end.*discontinued.*",
            category=UserWarning,
        )
        return mcal.get_calendar(name)


# --- conversion ----------------------------------------------------------


def now_utc() -> pd.Timestamp:
    """Current instant, tz-aware UTC.

    Feature code must not call this. CLAUDE.md's look-ahead rule requires an
    explicit ``as_of`` boundary; a function that reads "now" instead of taking
    ``as_of`` is a look-ahead bug. This exists for pipeline orchestration and
    logging only.
    """
    return pd.Timestamp.now(tz=UTC)


def to_utc(ts: pd.Timestamp | dt.datetime | str) -> pd.Timestamp:
    """Convert to tz-aware UTC. Naive input is rejected, never assumed."""
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        raise ValueError(
            f"{ts!r} is timezone-naive; supply an explicit timezone rather than "
            "letting one be guessed"
        )
    return stamp.tz_convert(UTC)


def to_kst(ts: pd.Timestamp | dt.datetime | str) -> pd.Timestamp:
    """Convert to tz-aware KST, for display only. Storage stays UTC."""
    return to_utc(ts).tz_convert(KST)


# --- sessions ------------------------------------------------------------


# Sessions the calendar library reports that the exchange did not actually hold.
#
# CLAUDE.md says never to hardcode holidays, and this does not: the calendar
# still supplies every session, and this only *removes* days it is known to have
# wrong. The rule exists so nobody re-derives Seollal and Chuseok by hand, which
# is a different thing from correcting a library that is demonstrably behind.
#
# Removal-only is deliberate and is the safe direction. A spurious extra session
# makes every continuity check report a gap that is not there, and invites a
# feature to be computed for a day with no data behind it. Adding a session the
# library omits would be the dangerous direction, and nothing here does that —
# the diff below found no day KRX traded that the calendar called closed.
#
# Derived on 2026-08-05 by diffing the calendar against KRX itself over
# 2026-01-01..2026-08-04: 146 calendar sessions against 144 real ones, and these
# are the two. Both are confirmed by KRX's own closure notice.
#
#   2026-06-03  전국동시지방선거 — Korean election days close the exchange, and
#               the date moves every cycle, so no library encodes it in advance.
#   2026-07-17  제헌절 — restored as a public holiday in 2026, eighteen years
#               after being dropped in 2008. exchange_calendars 4.13.2 and
#               pandas_market_calendars 5.4.0 both still treat it as a session.
#
# `test_the_calendar_corrections_still_match_krx` re-derives this against live
# KRX data, so the list fails loudly rather than rotting once the libraries
# catch up or a new ad-hoc closure appears.
_CALENDAR_CORRECTIONS: dict[str, frozenset[dt.date]] = {
    "KR": frozenset({dt.date(2026, 6, 3), dt.date(2026, 7, 17)}),
    "US": frozenset(),
}


def _schedule(market: Market, start: dt.date, end: dt.date) -> pd.DataFrame:
    schedule = _calendar(market).schedule(start_date=start, end_date=end)
    wrong = _CALENDAR_CORRECTIONS.get(market)
    if wrong:
        keep = [ts for ts in schedule.index if ts.date() not in wrong]
        if len(keep) != len(schedule.index):
            schedule = schedule.loc[keep]
    return schedule


def trading_days(market: Market, start: dt.date, end: dt.date) -> list[dt.date]:
    """Trading days in ``[start, end]`` inclusive, holidays excluded."""
    schedule = _schedule(market, start, end)
    return [ts.date() for ts in schedule.index]


def is_trading_day(market: Market, day: dt.date) -> bool:
    """Whether ``day`` is a trading day on ``market``."""
    return not _schedule(market, day, day).empty


def session_open_utc(market: Market, day: dt.date) -> pd.Timestamp:
    """Opening instant of ``day``'s session, tz-aware UTC."""
    schedule = _schedule(market, day, day)
    if schedule.empty:
        raise NoSessionFoundError(f"{day} is not a trading day on {market}")
    return schedule["market_open"].iloc[0]


def session_close_utc(market: Market, day: dt.date) -> pd.Timestamp:
    """Closing instant of ``day``'s session, tz-aware UTC.

    Derived from the calendar, which is what makes the US close land at 05:00
    KST during DST and 06:00 KST outside it without either value appearing in
    this codebase.
    """
    schedule = _schedule(market, day, day)
    if schedule.empty:
        raise NoSessionFoundError(f"{day} is not a trading day on {market}")
    return schedule["market_close"].iloc[0]


def previous_trading_day(market: Market, day: dt.date) -> dt.date:
    """Latest trading day strictly before ``day``."""
    start = day - dt.timedelta(days=_LOOKAHEAD_DAYS)
    days = [d for d in trading_days(market, start, day) if d < day]
    if not days:
        raise NoSessionFoundError(
            f"no {market} trading day in the {_LOOKAHEAD_DAYS} days before {day}"
        )
    return days[-1]


def last_closed_session(market: Market, at: pd.Timestamp | dt.datetime) -> dt.date:
    """The most recent session on ``market`` whose close has already passed.

    "Which session does *now* have complete data for?" — the question every
    scheduled run asks, and the one that must never be answered with today's
    date just because today is a trading day.

    The failure this exists to prevent was observed on 2026-08-06. The evening
    report was scheduled for 12:37 UTC (21:37 KST) but GitHub fired it at
    14:56 UTC, two hours and nineteen minutes late — the schedule slippage SPEC
    §1 already documents. By the time it rendered, the clock in Seoul read
    00:00 on the 7th, a Friday and therefore a trading day, so the renderer
    asked for the 7th's session. That session had not opened. Every feature
    came back ``None``, every ticker was rated 관망 at 0% coverage, and the
    report said "오늘은 전 종목이 관망입니다" as though that were a finding.

    Walking back one session at a time rather than subtracting a day: the gap
    between two sessions is a weekend, a holiday, or a 지방선거일, and only the
    calendar knows which.
    """
    at = to_utc(at)
    day = to_kst(at).date() if market == "KR" else at.date()
    for _ in range(_LOOKAHEAD_DAYS):
        if is_trading_day(market, day) and at >= session_close_utc(market, day):
            return day
        day = previous_trading_day(market, day)
    raise NoSessionFoundError(f"no closed {market} session within {_LOOKAHEAD_DAYS} days of {at}")


def next_trading_day(market: Market, day: dt.date) -> dt.date:
    """Earliest trading day strictly after ``day``."""
    end = day + dt.timedelta(days=_LOOKAHEAD_DAYS)
    days = [d for d in trading_days(market, day, end) if d > day]
    if not days:
        raise NoSessionFoundError(
            f"no {market} trading day in the {_LOOKAHEAD_DAYS} days after {day}"
        )
    return days[0]


@cache
def _opens_from(market: Market, start: dt.date) -> pd.Series:
    """Session opens in ``[start, start + _LOOKAHEAD_DAYS]``, oldest first.

    Cached because :func:`next_tradeable_open` is called once per collected
    article — ``kr_news.parse_feed`` stamps ``known_at_utc`` per row — and every
    call was rebuilding a 21-day schedule from the calendar. Measured at
    **2.18 ms**, which is ~3.7 s for one 1,700-article run and ~2 minutes a day
    across the 31 scheduled runs. The articles in a run share a handful of
    publication dates, so the cache collapses that to a handful of builds.

    Returning the ``market_open`` series rather than the schedule frame is
    deliberate: a cached mutable DataFrame handed to every caller is an aliasing
    bug waiting to happen, and this is the only column any caller wanted.
    """
    end = start + dt.timedelta(days=_LOOKAHEAD_DAYS)
    return _schedule(market, start, end)["market_open"]


def next_tradeable_open(market: Market, published_at: pd.Timestamp | dt.datetime) -> pd.Timestamp:
    """Earliest instant at which news published at ``published_at`` is tradeable.

    Implements CLAUDE.md's look-ahead rule: news published during a session is
    assumed tradeable at the *next* session's open, never the current one.

    Concretely this returns the earliest session open at or after
    ``published_at``:

    - published before the open on a trading day → that same day's open
    - published intraday → the next trading day's open (the current session's
      open has already passed)
    - published after the close, or on a holiday/weekend → the next trading
      day's open

    News timestamped exactly at an open is treated as tradeable at that open.
    """
    published = to_utc(published_at)
    start = published.date()

    opens = _opens_from(market, start)
    eligible = opens[opens >= published]
    if eligible.empty:
        raise NoSessionFoundError(
            f"no {market} session open within {_LOOKAHEAD_DAYS} days of {published}"
        )
    return eligible.iloc[0]
