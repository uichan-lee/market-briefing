"""Tests for the §8.5 shadow portfolio.

Synthetic, with answers known in advance, for the same reason `test_ic.py` is:
the window opened 2026-08-13 and produces nothing readable until November, so
this is the only verification the arithmetic can have before the gate.

The construction is not tested for taste — it is PREREGISTRATION §8.5's and was
fixed before this module existed. What is tested is that the code implements
*that* construction and not a nearby one: the right number of names, entry at
the next open, and the hold-through-a-missing-rating rule that a scheduler drop
will exercise for real.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from src.collectors.kr_index import BENCHMARK_TICKER
from src.eval.ic import bucket_size, forward_return
from src.eval.shadow_portfolio import (
    benchmark_returns,
    curve,
    holdings,
    report,
    returns,
    summary,
)
from src.util.session import trading_days


def sessions(n: int) -> list[dt.date]:
    return trading_days("KR", dt.date(2026, 8, 13), dt.date(2026, 11, 13))[:n]


def price_frame(rows: dict[str, list[tuple[float, float]]], days: list[dt.date]) -> pd.DataFrame:
    out = []
    for ticker, bars in rows.items():
        for day, (open_, close) in zip(days, bars, strict=True):
            out.append({"date": pd.Timestamp(day), "ticker": ticker, "open": open_, "close": close})
    return pd.DataFrame(out)


def rating_frame(scores: dict[str, list[float]], days: list[dt.date]) -> pd.DataFrame:
    out = []
    for ticker, values in scores.items():
        for day, score in zip(days, values, strict=True):
            out.append({"date": pd.Timestamp(day), "ticker": ticker, "score": score})
    return pd.DataFrame(out)


# --- the bucket rule ------------------------------------------------------


def test_the_live_universe_holds_seven_names():
    """PREREGISTRATION §8.5's table said 6 beside `ceil(31 × 0.2)` until the slip
    was caught and logged in §R. 6.2 rounds up to 7."""
    assert bucket_size(31) == 7


def test_the_same_rule_serves_the_quantile_spread_and_the_portfolio():
    """One function, so "top 20%" cannot come to mean two different counts."""
    for n in (5, 10, 31, 40):
        assert bucket_size(n) == max(1, -(-n * 2 // 10))


# --- selection ------------------------------------------------------------


def test_the_highest_scoring_names_are_held():
    days = sessions(1)
    ratings = rating_frame({"A": [0.1], "B": [0.9], "C": [0.5], "D": [-0.2], "E": [0.0]}, days)
    held = holdings(ratings)
    assert bucket_size(5) == 1
    assert held["ticker"].tolist() == ["B"]


def test_selection_happens_per_session_not_once():
    """The portfolio rebalances every session; a name winning on day 1 must not
    be held on day 2 if it stopped winning."""
    days = sessions(2)
    ratings = rating_frame({"A": [0.9, 0.1], "B": [0.1, 0.9]}, days)
    held = holdings(ratings)
    first = held[held["date"] == pd.Timestamp(days[0])]["ticker"].tolist()
    second = held[held["date"] == pd.Timestamp(days[1])]["ticker"].tolist()
    assert first == ["A"]
    assert second == ["B"]


def test_sessions_outside_the_window_are_not_traded():
    days = trading_days("KR", dt.date(2026, 8, 3), dt.date(2026, 8, 20))[:6]
    ratings = rating_frame({"A": [0.9] * 6, "B": [0.1] * 6}, days)
    held = holdings(ratings)
    assert (pd.to_datetime(held["date"]).dt.date >= dt.date(2026, 8, 13)).all()


# --- returns --------------------------------------------------------------


def test_the_portfolio_earns_the_next_sessions_open_to_close():
    """§8.5's execution convention, pinned as arithmetic. A close-to-close
    regression would report a different number from a price the rating did not
    exist at."""
    days = sessions(2)
    prices = price_frame(
        {"A": [(1.0, 999.0), (100.0, 110.0)], "B": [(1.0, 1.0), (100.0, 100.0)]}, days
    )
    ratings = rating_frame({"A": [0.9, 0.9], "B": [0.1, 0.1]}, days)

    out = returns(holdings(ratings), forward_return(prices))
    assert out.iloc[0]["portfolio_return"] == pytest.approx(0.10)


def test_the_basket_is_equal_weighted():
    days = sessions(2)
    prices = price_frame(
        {
            "A": [(100.0, 100.0), (100.0, 120.0)],  # +20%
            "B": [(100.0, 100.0), (100.0, 100.0)],  # 0%
            "C": [(100.0, 100.0), (100.0, 50.0)],  # −50%, not held
        },
        days,
    )
    ratings = rating_frame({"A": [0.9, 0.9], "B": [0.8, 0.8], "C": [-0.9, -0.9]}, days)
    # 3 names, quantile 0.2 -> 1 held. Widen so two are held.
    out = returns(holdings(ratings, quantile=0.6), forward_return(prices))
    assert out.iloc[0]["portfolio_return"] == pytest.approx(0.10)  # mean of +20% and 0%


def test_a_session_with_no_rating_holds_the_previous_position():
    """§8.5's rule for a dropped scheduler run. The account does not go to cash
    because GitHub missed a cron — that is weather, not a decision."""
    days = sessions(3)
    prices = price_frame(
        {"A": [(100.0, 110.0)] * 3, "B": [(100.0, 90.0)] * 3},
        days,
    )
    ratings = rating_frame({"A": [0.9, 0.9, 0.9], "B": [0.1, 0.1, 0.1]}, days)
    holed = ratings[ratings["date"] != pd.Timestamp(days[1])]

    out = returns(holdings(holed), forward_return(prices))
    middle = out[out["date"] == pd.Timestamp(days[1])].iloc[0]
    # A was held on day 0 and must still be held on day 1, earning A's +10%.
    assert middle["portfolio_return"] == pytest.approx(0.10)
    assert middle["n"] == 1


def test_nothing_is_held_before_the_first_rating():
    """An account cannot hold a position decided by a rating that did not exist."""
    days = sessions(3)
    prices = price_frame({"A": [(100.0, 110.0)] * 3}, days)
    ratings = rating_frame({"A": [0.9]}, days[2:])
    out = returns(holdings(ratings), forward_return(prices))
    assert pd.Timestamp(days[0]) not in set(out["date"])


# --- the benchmark --------------------------------------------------------


def test_the_benchmark_uses_the_same_execution_convention():
    days = sessions(2)
    bench = price_frame({BENCHMARK_TICKER: [(1.0, 999.0), (100.0, 105.0)]}, days)
    out = benchmark_returns(bench)
    assert out.iloc[0]["benchmark_return"] == pytest.approx(0.05)


def test_only_the_benchmark_ticker_is_read():
    days = sessions(2)
    frame = price_frame(
        {BENCHMARK_TICKER: [(100.0, 100.0)] * 2, "005930": [(100.0, 200.0)] * 2}, days
    )
    out = benchmark_returns(frame)
    assert len(out) == 1
    assert out.iloc[0]["benchmark_return"] == pytest.approx(0.0)


# --- the comparison -------------------------------------------------------


def test_both_legs_compound_over_the_sessions_they_share():
    portfolio = pd.DataFrame(
        {"date": [pd.Timestamp(d) for d in sessions(2)], "portfolio_return": [0.10, 0.10]}
    )
    benchmark = pd.DataFrame(
        {"date": [pd.Timestamp(d) for d in sessions(2)], "benchmark_return": [0.0, 0.0]}
    )
    track = curve(portfolio, benchmark)
    assert track.iloc[-1]["portfolio"] == pytest.approx(0.21)  # 1.1 * 1.1 − 1
    assert track.iloc[-1]["benchmark"] == pytest.approx(0.0)


def test_a_session_only_one_leg_has_is_excluded():
    """A cumulative comparison over two different session sets measures the
    calendar as much as the signal."""
    days = sessions(3)
    portfolio = pd.DataFrame(
        {"date": [pd.Timestamp(d) for d in days], "portfolio_return": [0.1, 0.1, 0.1]}
    )
    benchmark = pd.DataFrame(
        {"date": [pd.Timestamp(d) for d in days[:2]], "benchmark_return": [0.0, 0.0]}
    )
    assert len(curve(portfolio, benchmark)) == 2


def test_the_summary_reports_the_difference_the_criterion_reads():
    track = pd.DataFrame(
        {
            "date": [pd.Timestamp(d) for d in sessions(1)],
            "portfolio": [0.10],
            "benchmark": [0.04],
        }
    )
    assert summary(track)["excess"] == pytest.approx(0.06)


# --- the report -----------------------------------------------------------


def test_the_report_says_no_order_was_placed():
    """SPEC §0 principle 5 and CLAUDE.md rule 2. A P&L table that does not say
    this reads like a trading record."""
    text = report(pd.DataFrame(columns=["date", "portfolio", "benchmark"]))
    assert "No order was placed" in text


def test_the_report_states_the_dividend_handicap_when_it_has_numbers():
    """The asymmetry favours the shadow portfolio, so it has to be stated
    wherever the comparison is shown — not only in PREREGISTRATION."""
    track = pd.DataFrame(
        {"date": [pd.Timestamp(d) for d in sessions(1)], "portfolio": [0.1], "benchmark": [0.0]}
    )
    text = report(track)
    assert "a narrow win is not a win" in text.lower()
    assert "distribution-adjusted" in text


def test_the_report_refuses_to_show_numbers_before_the_window_has_sessions():
    text = report(pd.DataFrame(columns=["date", "portfolio", "benchmark"]))
    assert "Nothing to report yet" in text
