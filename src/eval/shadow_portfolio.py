"""The shadow portfolio. PREREGISTRATION §8.5, read at the 3-month gate.

A hypothetical account that trades exactly what the ⑥ ratings say, tracked so
the rating is falsifiable rather than merely opinionated (SPEC §2.2⑦).

**This places no orders and never will.** SPEC §0 principle 5 and CLAUDE.md's
absolute rule 2 hold without exception; the trigger is pulled by a human, and
not before the 3-month gate. Everything here is arithmetic over stored prices.

The construction is not a choice made here. §8.5 fixed all of it before this
module existed, because "beats KODEX 200 buy-and-hold" named a comparison whose
portfolio nobody had specified — which would have left its construction to be
chosen by whoever first ran it, after seeing which construction won:

| | |
|---|---|
| Universe | the KR watchlist |
| Selection | top 20% by composite score, ``ceil(n × 0.2)`` names |
| Weighting | equal, long-only, no cash, leverage 1.0 |
| Rebalance | every session, executed at the **open of t+1** |
| No rating that session | hold the previous position |
| Benchmark | 069500 buy-and-hold, same window, same execution |
| Costs | zero — §8.5 defers them to the 6-month gate |
| "Beats" | higher cumulative return. Not risk-adjusted |

Long-only equal-weight against a long-only index keeps direction and leverage
the same on both legs, which is what lets a plain return comparison mean
something without a risk model.

**One asymmetry, and it favours this portfolio.** KR prices are split-adjusted
but not distribution-adjusted, so the benchmark's price return understates its
total return by roughly its distribution yield. §8.5 records this. A narrow win
here is not a win.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd

from src.collectors.kr_index import BENCHMARK_TICKER
from src.eval.ic import (
    DATA,
    QUANTILE,
    WINDOW_END,
    WINDOW_START,
    _cell,
    _in_window,
    bucket_size,
    forward_return,
)
from src.features.compute import load_raw
from src.report.render import load_rating_history

_LOW_CONFIDENCE_COVERAGE = 0.5


def holdings(
    ratings: pd.DataFrame,
    *,
    quantile: float = QUANTILE,
    start: dt.date = WINDOW_START,
    end: dt.date = WINDOW_END,
) -> pd.DataFrame:
    """The names held into each session's `t+1`, one row per (date, ticker).

    Sessions are taken from the ratings archive, so a session the scheduler
    dropped simply is not there — and :func:`returns` carries the previous
    position across it, which is what §8.5 means by "hold the previous
    position". Encoding the hold here instead would require inventing rows for
    sessions that produced nothing, and an invented row is indistinguishable
    from a real one once it is in the frame.
    """
    scoped = _in_window(ratings, start, end)
    if scoped.empty:
        return pd.DataFrame(columns=["date", "ticker"])

    rows = []
    for day, group in scoped.dropna(subset=["score"]).groupby("date", observed=True):
        if "low_confidence" in group:
            eligible = group[~group["low_confidence"].fillna(False)]
        elif {"rating", "weight_coverage"}.issubset(group.columns):
            # Archives written before the explicit field still carry enough
            # evidence to recover the same publication guard.
            forced_hold = (group["rating"] == "관망") & (
                group["weight_coverage"] < _LOW_CONFIDENCE_COVERAGE
            )
            eligible = group[~forced_hold]
        else:
            eligible = group
        if eligible.empty:
            continue
        size = bucket_size(len(eligible), quantile)
        for ticker in eligible.nlargest(size, "score")["ticker"]:
            rows.append({"date": pd.Timestamp(day), "ticker": ticker})
    if not rows:
        return pd.DataFrame(columns=["date", "ticker"])
    return pd.DataFrame(rows).sort_values(["date", "ticker"]).reset_index(drop=True)


def returns(held: pd.DataFrame, forward: pd.DataFrame) -> pd.DataFrame:
    """Equal-weighted return of the held names, per session held into.

    A session with no rating inherits the previous session's names, per §8.5.
    The inheritance is over *rated* sessions rather than calendar sessions: the
    return series is driven by the price archive, so a session that exists in
    prices but not in ratings gets the last decided basket.
    """
    if forward.empty:
        return pd.DataFrame(columns=["date", "portfolio_return", "n"])

    basket: list[str] = []
    by_day = (
        {
            pd.Timestamp(day): group["ticker"].tolist()
            for day, group in held.groupby("date", observed=True)
        }
        if not held.empty
        else {}
    )

    rows = []
    for day in sorted(pd.to_datetime(forward["date"]).unique()):
        basket = by_day.get(pd.Timestamp(day), basket)
        if not basket:
            continue  # nothing decided yet; the account is not open
        session = forward[pd.to_datetime(forward["date"]) == day]
        picked = session[session["ticker"].isin(basket)]["forward_return"].dropna()
        if picked.empty:
            continue
        rows.append(
            {"date": pd.Timestamp(day), "portfolio_return": float(picked.mean()), "n": len(picked)}
        )
    # An empty `rows` would otherwise yield a frame with no columns at all, and
    # every caller reads `date` — the same shape of silent failure the
    # collectors return a schema-shaped empty frame to avoid.
    if not rows:
        return pd.DataFrame(columns=["date", "portfolio_return", "n"])
    return pd.DataFrame(rows)


def benchmark_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """KODEX 200's forward return, on the same execution convention.

    Buy-and-hold is expressed as being in the benchmark every session rather
    than as a single entry and exit, which is the same thing compounded and
    keeps both legs on one convention — the point of comparing at all.
    """
    forward = forward_return(prices)
    out = forward[forward["ticker"] == BENCHMARK_TICKER][["date", "forward_return"]]
    return out.rename(columns={"forward_return": "benchmark_return"}).dropna()


def curve(portfolio: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    """Both legs compounded over the sessions they share.

    Restricted to the intersection deliberately: a cumulative comparison over
    two different session sets measures the calendar as much as the signal.
    """
    if portfolio.empty or benchmark.empty:
        return pd.DataFrame(columns=["date", "portfolio", "benchmark"])

    merged = portfolio.merge(benchmark, on="date", how="inner").sort_values("date")
    if merged.empty:
        return pd.DataFrame(columns=["date", "portfolio", "benchmark"])

    merged["portfolio"] = (1.0 + merged["portfolio_return"]).cumprod() - 1.0
    merged["benchmark"] = (1.0 + merged["benchmark_return"]).cumprod() - 1.0
    return merged[["date", "portfolio", "benchmark"]].reset_index(drop=True)


def summary(track: pd.DataFrame) -> dict[str, float | int | None]:
    """Cumulative return of each leg and the difference §8.5's criterion reads."""
    if track.empty:
        return {"portfolio": None, "benchmark": None, "excess": None, "sessions": 0}
    last = track.iloc[-1]
    return {
        "portfolio": float(last["portfolio"]),
        "benchmark": float(last["benchmark"]),
        "excess": float(last["portfolio"] - last["benchmark"]),
        "sessions": len(track),
    }


def load(
    root: Path = DATA,
    *,
    start: dt.date = WINDOW_START,
    end: dt.date = WINDOW_END,
) -> pd.DataFrame:
    """Both legs' compounded curves from the stored archives."""
    prices = load_raw(root / "raw", "kr/price", key=("date", "ticker"))
    bench = load_raw(root / "raw", "kr/benchmark", key=("date", "ticker"))
    ratings = load_rating_history(root)

    held = holdings(ratings, start=start, end=end)
    forward = _in_window(forward_return(prices), start, end)
    bench_forward = _in_window(benchmark_returns(bench), start, end)
    return curve(returns(held, forward), bench_forward)


def report(
    track: pd.DataFrame,
    *,
    start: dt.date = WINDOW_START,
    end: dt.date = WINDOW_END,
) -> str:
    """SPEC §2.2⑦ and the §8.5 comparison. Reports; does not decide."""
    stats = summary(track)
    lines = [
        "# Shadow portfolio — PREREGISTRATION §8.5",
        "",
        f"Window **{start} → {end}**. Top {QUANTILE:.0%} by composite score, equal-weighted, "
        "long-only, rebalanced every session at the next open. Zero costs — §8.5 defers them "
        "to the 6-month gate.",
        "",
        "> **No order was placed.** SPEC §0 principle 5: this system states an opinion and "
        "stops. The account below is arithmetic over stored prices.",
        "",
    ]

    if not stats["sessions"]:
        lines += [
            f"**Nothing to report yet.** The window opened {start}; a session enters only "
            "once a rating and its `t+1` return both exist.",
            "",
        ]
        return "\n".join(lines)

    lines += [
        "| leg | cumulative return |",
        "|---|---:|",
        f"| shadow portfolio | {_cell(stats['portfolio'], 4)} |",
        f"| KODEX 200 buy-and-hold | {_cell(stats['benchmark'], 4)} |",
        f"| **difference** | **{_cell(stats['excess'], 4)}** |",
        f"| sessions | {stats['sessions']} |",
        "",
        "**The benchmark is handicapped.** KR prices here are split-adjusted but not "
        "distribution-adjusted, so KODEX 200's price return understates its total return by "
        "roughly its distribution yield. §8.5 records this asymmetry because it favours the "
        "shadow portfolio: **a narrow win is not a win.**",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PREREGISTRATION §8.5 shadow portfolio")
    sub = parser.add_subparsers(dest="command", required=True)
    reporter = sub.add_parser("report", help="print the §8.5 comparison")
    reporter.add_argument("--start", type=dt.date.fromisoformat, default=WINDOW_START)
    reporter.add_argument("--end", type=dt.date.fromisoformat, default=WINDOW_END)

    args = parser.parse_args(argv)
    if args.command == "report":
        print(report(load(start=args.start, end=args.end), start=args.start, end=args.end))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
