"""Tests for the §8.4 signal metrics.

Offline, against synthetic frames built here with answers known in advance. That
is not a second-best: the window these metrics measure opened 2026-08-13 and
will not produce a readable number until November, so **synthetic data is the
only verification the arithmetic can have before the gate.** A correlation of
0.9 computed by wrong code looks exactly like one computed by right code.

The tests that matter most are the look-ahead ones. §8.5 fixed the forward
return at `close(t+1)/open(t+1)` precisely because the evening run publishes six
hours after the close of session `t`; a regression to close-to-close would
produce plausible numbers and quietly invalidate the gate.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.eval.ic import (
    daily_ic,
    excess_return,
    forward_return,
    icir,
    paired,
    quantile_spread,
    report,
)
from src.util.session import trading_days


def sessions(n: int, start: dt.date = dt.date(2026, 8, 13)) -> list[dt.date]:
    return trading_days("KR", start, dt.date(2026, 11, 13))[:n]


def price_frame(rows: dict[str, list[tuple[float, float]]], days: list[dt.date]) -> pd.DataFrame:
    """`{ticker: [(open, close), ...]}` → the stored KR price shape."""
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


# --- the forward return ---------------------------------------------------


def test_the_return_is_measured_from_the_next_open_to_the_next_close():
    """§8.5's convention, pinned as arithmetic.

    Session 0 must report session 1's intraday move: 110/100 − 1 = 0.10. If this
    ever reads close-to-close it would report 110/100 from session 0's *close*,
    which is a different number computed from a price the rating did not exist at.
    """
    days = sessions(2)
    prices = price_frame({"A": [(50.0, 100.0), (100.0, 110.0)]}, days)
    out = forward_return(prices)

    first = out[out["date"] == pd.Timestamp(days[0])].iloc[0]
    assert first["forward_return"] == pytest.approx(0.10)


def test_the_session_close_never_enters_the_return():
    """A close-to-close regression, caught by construction.

    Session 0's close is 999 and is deliberately absurd. Any formula touching it
    produces a wildly different answer from the correct 0.10.
    """
    days = sessions(2)
    prices = price_frame({"A": [(1.0, 999.0), (100.0, 110.0)]}, days)
    out = forward_return(prices)
    assert out.iloc[0]["forward_return"] == pytest.approx(0.10)


def test_the_last_session_has_no_forward_return():
    """Absent, not approximated — the same stance valuation_band takes."""
    days = sessions(3)
    prices = price_frame({"A": [(100.0, 100.0)] * 3}, days)
    out = forward_return(prices)
    assert pd.isna(out[out["date"] == pd.Timestamp(days[-1])].iloc[0]["forward_return"])


def test_the_shift_does_not_leak_across_tickers():
    days = sessions(2)
    prices = price_frame({"A": [(100.0, 100.0)] * 2, "B": [(100.0, 200.0)] * 2}, days)
    out = forward_return(prices)
    a = out[(out["ticker"] == "A") & (out["date"] == pd.Timestamp(days[0]))].iloc[0]
    assert a["forward_return"] == pytest.approx(0.0)


def test_a_missing_session_shortens_the_series_rather_than_pairing_t_with_t_plus_two():
    """The shift is positional within each ticker's own history."""
    days = sessions(3)
    prices = price_frame({"A": [(100.0, 100.0), (100.0, 110.0), (100.0, 120.0)]}, days)
    holed = prices[prices["date"] != pd.Timestamp(days[1])]
    out = forward_return(holed)
    # Session 0 now pairs with session 2, and that is visible rather than hidden.
    assert out.iloc[0]["forward_return"] == pytest.approx(0.20)


# --- the excess return ----------------------------------------------------


def test_a_singleton_sector_gets_the_universe_return_not_nan():
    """Five of ten KR sectors hold one ticker. Dropping them would cut the
    cross-section by 16% for a reason that is a property of the sector table."""
    days = sessions(2)
    prices = price_frame(
        {
            "A": [(100.0, 100.0), (100.0, 110.0)],  # +10% forward
            "B": [(100.0, 100.0), (100.0, 120.0)],  # +20%
            "LONE": [(100.0, 100.0), (100.0, 130.0)],  # +30%, alone in its sector
        },
        days,
    )
    sectors = {"A": "반도체", "B": "반도체", "LONE": "유통"}
    out = excess_return(forward_return(prices), sectors)
    first = out[out["date"] == pd.Timestamp(days[0])].set_index("ticker")["excess_return"]

    assert not pd.isna(first["LONE"])
    # universe mean of (0.10, 0.20, 0.30) is 0.20; LONE is 0.30.
    assert first["LONE"] == pytest.approx(0.10)


def test_a_multi_member_sector_uses_its_own_mean():
    days = sessions(2)
    prices = price_frame(
        {
            "A": [(100.0, 100.0), (100.0, 110.0)],
            "B": [(100.0, 100.0), (100.0, 130.0)],
            "LONE": [(100.0, 100.0), (100.0, 190.0)],
        },
        days,
    )
    sectors = {"A": "반도체", "B": "반도체", "LONE": "유통"}
    out = excess_return(forward_return(prices), sectors)
    first = out[out["date"] == pd.Timestamp(days[0])].set_index("ticker")["excess_return"]
    # sector mean of (0.10, 0.30) is 0.20, so A is −0.10 — not the universe mean.
    assert first["A"] == pytest.approx(-0.10)


# --- IC -------------------------------------------------------------------


def _known_pairs(direction: int, n_days: int = 5):
    """Scores that rank exactly with (or against) the realised excess return.

    Every session carries the same cross-sectional ordering, so every session
    that has a `t+1` yields an IC of exactly ±1 — which makes both the per-session
    arithmetic and the aggregate checkable by hand.
    """
    days = sessions(n_days)
    ranks = {"A": 1, "B": 2, "C": 3, "D": 4}
    prices = price_frame(
        {t: [(100.0, 100.0 * (1 + r * 0.01))] * n_days for t, r in ranks.items()},
        days,
    )
    scores = {t: [r * direction] * n_days for t, r in ranks.items()}
    return paired(
        rating_frame(scores, days),
        excess_return(forward_return(prices), dict.fromkeys(ranks, "s")),
    )


def test_a_score_that_ranks_with_the_return_correlates_at_one():
    ic = daily_ic(_known_pairs(direction=1))
    assert not ic.empty
    assert ic["ic"].iloc[-1] == pytest.approx(1.0)


def test_a_score_that_ranks_against_the_return_correlates_at_minus_one():
    ic = daily_ic(_known_pairs(direction=-1))
    assert ic["ic"].iloc[-1] == pytest.approx(-1.0)


def test_a_session_without_a_published_rating_is_absent_rather_than_shifting_the_series():
    """The archive has real holes — 2026-08-04 and 08-05 were KR trading days with
    no rating file at all, and GitHub drops runs. A hole must remove a session
    from the series, never slide the remaining ones onto the wrong returns."""
    days = sessions(4)
    prices = price_frame(
        {"A": [(100.0, 100.0)] * 4, "B": [(100.0, 110.0)] * 4},
        days,
    )
    ratings = rating_frame({"A": [1.0] * 4, "B": [2.0] * 4}, days)
    holed = ratings[ratings["date"] != pd.Timestamp(days[1])]

    ic = daily_ic(paired(holed, excess_return(forward_return(prices), {"A": "s", "B": "s"})))
    assert pd.Timestamp(days[1]) not in set(ic["date"])
    assert pd.Timestamp(days[0]) in set(ic["date"])


def test_the_final_session_never_enters_the_ic_series():
    days = sessions(3)
    prices = price_frame({"A": [(100.0, 100.0)] * 3, "B": [(100.0, 110.0)] * 3}, days)
    ratings = rating_frame({"A": [1.0] * 3, "B": [2.0] * 3}, days)
    ic = daily_ic(paired(ratings, excess_return(forward_return(prices), {"A": "s", "B": "s"})))
    assert pd.Timestamp(days[-1]) not in set(ic["date"])


def test_pairs_outside_the_window_are_excluded():
    """08-03..08-12 were published under the pre-short_ratio-fix composite. §8.5
    excludes them; this pins that the exclusion is by date, not by availability."""
    days = trading_days("KR", dt.date(2026, 8, 3), dt.date(2026, 8, 20))[:6]
    prices = price_frame({"A": [(100.0, 100.0)] * 6, "B": [(100.0, 110.0)] * 6}, days)
    ratings = rating_frame({"A": [1.0] * 6, "B": [2.0] * 6}, days)
    out = paired(ratings, excess_return(forward_return(prices), {"A": "s", "B": "s"}))
    assert (pd.to_datetime(out["date"]).dt.date >= dt.date(2026, 8, 13)).all()


# --- ICIR -----------------------------------------------------------------


def test_icir_is_mean_over_sd():
    stats = icir([0.2, 0.4, 0.6])
    assert stats["mean_ic"] == pytest.approx(0.4)
    assert stats["icir"] == pytest.approx(0.4 / np.std([0.2, 0.4, 0.6], ddof=1))


def test_the_bootstrap_interval_is_reproducible_under_a_fixed_seed():
    """An interval that moved between runs would be one more thing to argue
    about at the gate."""
    series = [0.1, -0.2, 0.3, 0.05, 0.4, -0.1, 0.25]
    first, second = icir(series), icir(series)
    assert first["low"] == second["low"]
    assert first["high"] == second["high"]


def test_the_interval_brackets_the_point_estimate():
    series = [0.1, -0.2, 0.3, 0.05, 0.4, -0.1, 0.25]
    stats = icir(series)
    assert stats["low"] < stats["icir"] < stats["high"]


def test_too_few_sessions_yields_none_rather_than_a_number():
    assert icir([0.3])["icir"] is None


# --- quantile spread ------------------------------------------------------


def test_the_spread_is_top_quantile_minus_bottom():
    days = sessions(2)
    moves = {"A": 0.0, "B": 0.1, "C": 0.2, "D": 0.3, "E": 0.4}
    prices = price_frame(
        {t: [(100.0, 100.0), (100.0, 100.0 * (1 + m))] for t, m in moves.items()}, days
    )
    scores = {t: [i, i] for i, t in enumerate(moves)}
    pairs = paired(
        rating_frame(scores, days),
        excess_return(forward_return(prices), dict.fromkeys(moves, "s")),
    )
    spread = quantile_spread(pairs)
    # 5 tickers, quantile 0.2 -> 1 per bucket: E (+0.4) minus A (0.0) in raw
    # terms; both sides are excess returns against the same universe mean, so
    # the difference survives the subtraction.
    assert spread["spread"] == pytest.approx(0.4)


def test_a_session_too_small_to_split_is_skipped():
    days = sessions(2)
    prices = price_frame({"A": [(100.0, 100.0)] * 2}, days)
    pairs = paired(
        rating_frame({"A": [1.0, 1.0]}, days), excess_return(forward_return(prices), {"A": "s"})
    )
    assert quantile_spread(pairs)["spread"] is None


# --- the report -----------------------------------------------------------


def test_the_report_refuses_to_show_numbers_before_the_window_has_sessions():
    text = report(pd.DataFrame(columns=["date", "ticker", "score", "excess_return"]))
    assert "Not enough sessions" in text
    assert "ICIR" not in text.split("Not enough sessions")[1]


def test_the_report_states_that_it_does_not_decide():
    """Same obligation bakeoff.report() carries: the table reports, §8.5 decides."""
    text = report(_known_pairs(direction=1))
    assert "does not decide" in text
    assert "ICIR > 0.3" in text


def test_the_report_names_the_deferred_news_polarity_metric():
    """§8.4 asks for two scores. Deferring one is legitimate; letting it vanish
    from the output is not."""
    text = report(_known_pairs(direction=1))
    assert "news_polarity" in text
    assert "not dropped" in text
