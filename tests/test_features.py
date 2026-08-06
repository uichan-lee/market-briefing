"""Tests for feature computation. SPEC §5, SPEC §12 step 9.

Offline, against synthetic frames built here. Nothing in this file touches the
real backfill — that lives outside the repository — so these prove the logic and
not the data.

The tests that matter most are the look-ahead ones. A feature that quietly uses
information from its own timestamp still produces plausible numbers, passes
every schema check, and invalidates the whole PREREGISTRATION evaluation.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.features.compute import (
    FEATURES,
    compute,
    load_raw,
    z_scores_for,
)
from src.features.normalize import rolling_percentile, rolling_z
from src.util.config import WatchlistEntry
from src.util.session import session_close_utc, trading_days


def watchlist(*pairs):
    return [WatchlistEntry(ticker=t, name=t, sector=s, held=False, market="KR") for t, s in pairs]


def sessions(n: int) -> list[dt.date]:
    days = trading_days("KR", dt.date(2023, 1, 1), dt.date(2026, 8, 3))
    return days[:n]


def flow_frame(tickers, days, **overrides) -> pd.DataFrame:
    rows = []
    for ticker in tickers:
        for i, day in enumerate(days):
            rows.append(
                {
                    "date": pd.Timestamp(day),
                    "ticker": ticker,
                    "foreign_net": 1_000 + i,
                    "inst_net": -500,
                    "trading_value": 1_000_000,
                    "short_balance": 100 + i,
                    "shares_outstanding": 10_000,
                    "pbr": 1.0 + i * 0.01,
                    "known_at_utc": session_close_utc("KR", day),
                }
            )
    frame = pd.DataFrame(rows)
    for column, value in overrides.items():
        frame[column] = value
    return frame


def price_frame(tickers, days, closes=None) -> pd.DataFrame:
    rows = []
    for ticker in tickers:
        for i, day in enumerate(days):
            rows.append(
                {
                    "date": pd.Timestamp(day),
                    "ticker": ticker,
                    "close": (closes or {}).get(ticker, [100.0 + i] * len(days))[i],
                    "known_at_utc": session_close_utc("KR", day),
                }
            )
    return pd.DataFrame(rows)


# --- the z-score window ---------------------------------------------------


def test_the_window_ends_before_the_current_row():
    """SPEC §5 writes the window as t-252:t-1, and that is load-bearing.

    Including row t in its own mean and sigma lets an outlier inflate the sigma
    it is divided by, pulling every extreme toward zero exactly when it matters.
    """
    series = pd.Series([1.0] * 252 + [1.0])
    # A constant history and a constant current value: sigma is 0, so NaN.
    assert pd.isna(rolling_z(series, window=252).iloc[252])

    ramp = pd.Series(list(np.arange(252, dtype=float)) + [1000.0])
    z = rolling_z(ramp, window=252).iloc[252]
    # If row 252 were inside its own window the 1000 would raise the mean and
    # sigma and the score would land far lower.
    assert z > 10


def test_fewer_observations_than_the_window_give_nothing():
    """Not a shorter window. A z over 40 days and one over 252 are not on the
    same scale, and the weighted sum would silently mix them."""
    z = rolling_z(pd.Series(np.arange(300, dtype=float)), window=252)
    assert z.iloc[:252].isna().all()
    assert not pd.isna(z.iloc[252])


def test_zero_variance_gives_nan_not_infinity():
    z = rolling_z(pd.Series([5.0] * 260), window=252)
    assert pd.isna(z.iloc[-1])
    assert not np.isinf(z.dropna()).any()


def test_a_gap_in_the_series_does_not_shift_the_window():
    """Rows are sessions, not calendar days; a missing session is simply a
    shorter history, never a silently re-indexed one."""
    values = pd.Series([1.0] * 100 + [np.nan] + [1.0] * 200)
    z = rolling_z(values, window=252)
    assert len(z) == len(values)


# --- percentile -----------------------------------------------------------


def test_the_percentile_includes_the_current_observation():
    """'Where does today sit in the last three years' is a question about today.

    Excluding it would rank the value against a window it is not part of. This
    is not look-ahead: every observation in the window is knowable at t, and the
    boundary is enforced upstream on known_at_utc.
    """
    p = rolling_percentile(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), window=5)
    assert p.iloc[-1] == pytest.approx(1.0)

    p = rolling_percentile(pd.Series([5.0, 4.0, 3.0, 2.0, 1.0]), window=5)
    assert p.iloc[-1] == pytest.approx(0.0)


# --- look-ahead on the boundary ------------------------------------------


def test_as_of_excludes_rows_knowable_only_at_that_instant():
    days = sessions(300)
    flow = flow_frame(["005930"], days)
    prices = price_frame(["005930"], days)

    boundary = session_close_utc("KR", days[-1])
    out = compute(flow, prices, watchlist(("005930", "반도체")), as_of=boundary)
    # `<` not `<=`: a row is not usable at the instant it becomes known.
    assert pd.Timestamp(days[-1]) not in set(out["date"])
    assert pd.Timestamp(days[-2]) in set(out["date"])


def test_the_boundary_is_known_at_utc_not_date():
    """They differ by the length of the trading day. Filtering on `date` would
    make a whole session available from midnight."""
    days = sessions(300)
    flow = flow_frame(["005930"], days)
    prices = price_frame(["005930"], days)

    midday = pd.Timestamp(f"{days[-1]} 03:00", tz="UTC")  # noon KST, before close
    out = compute(flow, prices, watchlist(("005930", "반도체")), as_of=midday)
    assert pd.Timestamp(days[-1]) not in set(out["date"])


# --- the features ---------------------------------------------------------


def test_flow_features_sum_before_dividing():
    """SPEC §5: cumulative net buying ÷ cumulative trading value.

    Averaging daily ratios instead would give a quiet session the same say as a
    heavy one.
    """
    days = sessions(10)
    flow = flow_frame(["005930"], days)
    flow.loc[flow.index[-1], "trading_value"] = 9_000_000  # one heavy session

    out = compute(flow, pd.DataFrame(), watchlist(("005930", "반도체")), window=5)
    last = out.iloc[-1]
    net = sum(1_000 + i for i in range(5, 10))
    turnover = 1_000_000 * 4 + 9_000_000
    assert last["foreign_flow_5d"] == pytest.approx(net / turnover)


def test_a_ticker_that_did_not_trade_gives_none_not_zero():
    """0/0. Zero would assert balanced flow on a ticker with no flow at all."""
    days = sessions(10)
    flow = flow_frame(["005930"], days)
    flow["trading_value"] = 0
    flow["foreign_net"] = 0

    out = compute(flow, pd.DataFrame(), watchlist(("005930", "반도체")), window=5)
    assert out["foreign_flow_5d"].isna().all()


def test_short_ratio_is_balance_over_shares_outstanding():
    days = sessions(5)
    flow = flow_frame(["005930"], days)
    out = compute(flow, pd.DataFrame(), watchlist(("005930", "반도체")), window=2)
    assert out.iloc[0]["short_ratio"] == pytest.approx(100 / 10_000)


def test_valuation_band_is_positive_when_pbr_is_cheap():
    """Inverted so cheap is positive, matching the +0.05 weight."""
    days = sessions(20)
    cheap = flow_frame(["005930"], days)
    cheap["pbr"] = [2.0] * 19 + [0.5]  # today is the cheapest of the window

    out = compute(
        cheap, pd.DataFrame(), watchlist(("005930", "반도체")), window=5, valuation_window=20
    )
    band = out.iloc[-1]["valuation_band"]
    assert band == pytest.approx(1.0)


def test_valuation_band_is_not_z_scored_again():
    """The deliberate deviation from §5's blanket rule.

    Applying both the 3-year percentile and the 252-day z-score needs 1,008
    sessions before a first value; the backfill holds 728.
    """
    days = sessions(20)
    flow = flow_frame(["005930"], days)
    out = compute(
        flow, pd.DataFrame(), watchlist(("005930", "반도체")), window=5, valuation_window=10
    )
    assert out["valuation_band"].equals(out["valuation_band_z"])
    assert out["valuation_band"].notna().any()  # and it actually produced values


# --- relative strength ----------------------------------------------------


def test_relative_strength_is_the_gap_to_the_sector():
    days = sessions(30)
    closes = {
        "005930": [100.0 * (1.10 ** (i / 20)) for i in range(30)],  # outperformer
        "000660": [100.0] * 30,  # flat
    }
    prices = price_frame(["005930", "000660"], days, closes)
    flow = flow_frame(["005930", "000660"], days)

    out = compute(flow, prices, watchlist(("005930", "반도체"), ("000660", "반도체")), window=5)
    row = out[(out["ticker"] == "005930") & (out["date"] == pd.Timestamp(days[-1]))].iloc[0]
    assert row["rel_strength_20d"] > 0


def test_a_sector_with_one_member_has_no_relative_strength():
    """Relative strength against oneself is identically zero, which would read
    as 'moved with its sector' when the truth is there is no sector."""
    days = sessions(30)
    prices = price_frame(["005930"], days)
    flow = flow_frame(["005930"], days)

    out = compute(flow, prices, watchlist(("005930", "반도체")), window=5)
    assert out["rel_strength_20d"].isna().all()


# --- handing off to rate() ------------------------------------------------


def test_nan_becomes_none_for_the_rating():
    """rate() treats None as absent and renormalizes; a NaN would propagate
    through the weighted sum and make the whole composite NaN."""
    days = sessions(10)
    out = compute(
        flow_frame(["005930"], days), pd.DataFrame(), watchlist(("005930", "반도체")), window=5
    )
    scores = z_scores_for(out, "005930", days[-1])
    assert set(scores) == set(FEATURES)
    assert scores["rel_strength_20d"] is None
    assert all(v is None or isinstance(v, float) for v in scores.values())


def test_an_unknown_ticker_or_day_yields_all_none():
    days = sessions(10)
    out = compute(
        flow_frame(["005930"], days), pd.DataFrame(), watchlist(("005930", "반도체")), window=5
    )
    assert z_scores_for(out, "999999", days[-1]) == dict.fromkeys(FEATURES)


def test_the_composite_survives_two_missing_features():
    """news_polarity and rev_4w are absent by design; rate() must still work."""
    from src.report.rating import rate
    from src.util.config import load_rating

    result = rate(
        "005930",
        {
            "foreign_flow_5d": 1.0,
            "inst_flow_5d": 0.5,
            "short_ratio": -0.2,
            "rel_strength_20d": 0.3,
            "valuation_band": 0.1,
        },
        load_rating(),
    )
    assert result.weight_coverage > 0.5
    assert "news_polarity" in result.missing
    assert "rev_4w" in result.missing


# --- loading --------------------------------------------------------------


def test_a_rerun_file_supersedes_the_original(tmp_path):
    """CLAUDE.md rule 1 keeps both; the later write is the one a re-run was
    performed to obtain."""
    directory = tmp_path / "kr" / "investor_flow"
    directory.mkdir(parents=True)
    base = pd.DataFrame({"date": [pd.Timestamp("2024-01-02")], "ticker": ["005930"], "v": [1]})
    base.to_parquet(directory / "2024-01-02.parquet", index=False)
    base.assign(v=[2]).to_parquet(directory / "2024-01-02-v2.parquet", index=False)

    loaded = load_raw(tmp_path, "kr/investor_flow")
    assert len(loaded) == 1
    assert loaded.iloc[0]["v"] == 2


def test_a_missing_directory_loads_as_empty(tmp_path):
    assert load_raw(tmp_path, "kr/investor_flow").empty


def test_a_collector_keyed_on_series_loads_with_an_explicit_key(tmp_path):
    """macro is keyed (date, series), not (date, ticker). The renderer reads it,
    so the loader has to serve both shapes rather than growing a second copy of
    the -v2 ordering rule."""
    directory = tmp_path / "macro"
    directory.mkdir(parents=True)
    day = pd.Timestamp("2024-01-02")
    pd.DataFrame({"date": [day], "series": ["vix"], "value": [13.0]}).to_parquet(
        directory / "2024-01-02.parquet", index=False
    )
    pd.DataFrame({"date": [day], "series": ["vix"], "value": [14.0]}).to_parquet(
        directory / "2024-01-02-v2.parquet", index=False
    )

    loaded = load_raw(tmp_path, "macro", key=("date", "series"))
    assert len(loaded) == 1
    assert loaded.iloc[0]["value"] == 14.0


def test_the_wrong_key_names_the_missing_column(tmp_path):
    """Silent is the failure mode to avoid: the default key raised a bare
    KeyError from inside pandas, which does not say which collector was read."""
    directory = tmp_path / "macro"
    directory.mkdir(parents=True)
    pd.DataFrame({"date": [pd.Timestamp("2024-01-02")], "series": ["vix"]}).to_parquet(
        directory / "2024-01-02.parquet", index=False
    )

    with pytest.raises(KeyError, match="ticker"):
        load_raw(tmp_path, "macro")


def test_an_empty_flow_frame_yields_the_schema():
    out = compute(pd.DataFrame(), pd.DataFrame(), watchlist(("005930", "반도체")))
    assert out.empty
    for feature in FEATURES:
        assert feature in out.columns
        assert f"{feature}_z" in out.columns


def test_valuation_band_is_absent_until_the_window_has_enough_history():
    """The documented consequence: 756 sessions needed, backfill holds 728.

    Absent rather than approximated. A percentile over a short window ranks a
    value against a band that is not three years wide, which is a different
    feature wearing the same name.
    """
    days = sessions(100)
    out = compute(
        flow_frame(["005930"], days),
        pd.DataFrame(),
        watchlist(("005930", "반도체")),
        window=5,
        valuation_window=756,
    )
    assert out["valuation_band"].isna().all()
