"""IC, ICIR and quantile spread. PREREGISTRATION §8.4, read at the §8.5 3-month gate.

This module measures; it does not decide. The gate's thresholds live in
PREREGISTRATION and are quoted here as constants so the report can say whether
each was met, but nothing here chooses what to do about it.

**Written 2026-08-13, before the window it measures had produced a single
session.** That ordering is the point. §8.4's metrics and §8.5's operational
definitions were both fixed while no return, IC, or P&L existed anywhere, so
neither could have been shaped by a result.

Five rules for reading the output, all fixed in PREREGISTRATION before the first
number:

1. **The score is the continuous composite, never the seven-point label.**
   Bucketing discards information and its cut points are a display choice;
   correlating on the label would confound the signal with where the thresholds
   happen to sit (§8.4).
2. **The return is `close(t+1)/open(t+1) − 1`, not close-to-close.** The evening
   run publishes at 21:37 KST, six hours after the close of session `t`, so a
   close-to-close return enters at a price that existed before the rating did.
   §8.5 records this as a departure from §8.4's plain wording and why.
3. **The window is 2026-08-13 → 2026-11-13 and starts there for a reason.** The
   `short_ratio` disclosure-lag fix landed that morning and moved the composite
   for every ticker; the six sessions published before it are excluded, not
   down-weighted.
4. **Singleton sectors take the universe return.** Five of ten KR sectors hold
   one ticker, where the sector excess is identically zero. Dropping them would
   shrink the cross-section for a reason that is a property of the sector table
   rather than of the data.
5. **ICIR is read with its interval.** The criterion is `> 0.3` on the point
   estimate, unchanged. At ~60 sessions its standard error is roughly 0.13, so a
   point estimate that clears the bar with an interval straddling zero passes
   the gate as written and settles nothing.

**Scores are read from the archive, not recomputed.** `data/ratings/` holds what
was actually published on each session, which is both what was knowable that day
and what §8.4 calls "the pipeline's actual output". Recomputing features at a
past `as_of` would additionally depend on `render.py`'s inert look-ahead guard,
which is a separate defect and not one this measurement should inherit.

**`news_polarity` IC is not computed here.** §8.4 asks for it separately, and it
has no producer — SPEC §12 steps 6–8 are unbuilt. Deferred on the record in
§8.5, not dropped.
"""

from __future__ import annotations

import argparse
import datetime as dt
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from src.eval.bakeoff import spearman
from src.features.compute import load_raw
from src.report.render import load_rating_history
from src.util.config import WatchlistEntry, load_watchlist

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"

# PREREGISTRATION §8.5, "The 3-month clock". Not derived from the archive's
# contents — pinned, so the window cannot be chosen once the record is known.
WINDOW_START = dt.date(2026, 8, 13)
WINDOW_END = dt.date(2026, 11, 13)

# §8.5's criterion. Quoted, not enforced: this module reports whether it was met.
ICIR_THRESHOLD = 0.3

# §8.4's quantile spread: top 20% minus bottom 20%.
QUANTILE = 0.2

# Bootstrap settings for the ICIR interval (§8.5). The seed is fixed so two
# readings of the same series agree — a confidence interval that moved between
# runs would be one more thing to argue about at the gate.
BOOTSTRAP_DRAWS = 10_000
BOOTSTRAP_SEED = 20261113


def forward_return(prices: pd.DataFrame) -> pd.DataFrame:
    """Session `t`'s tradeable next-day return, per ticker.

    ``close(t+1) / open(t+1) − 1``, placed on row `t` — the return of entering at
    the first moment the session-`t` rating could have been acted on and leaving
    at that session's close. PREREGISTRATION §8.5 fixes this convention and
    records why it is not close-to-close.

    The shift is by position within each ticker's own sorted history, so a
    session missing from the price archive shortens the series rather than
    silently pairing `t` with `t+2`. The final session has no `t+1` and is left
    ``NaN``: absent, not approximated.
    """
    if prices.empty:
        return pd.DataFrame(columns=["date", "ticker", "forward_return"])

    frame = prices.sort_values(["ticker", "date"]).copy()
    intraday = (
        pd.to_numeric(frame["close"], errors="coerce")
        / pd.to_numeric(frame["open"], errors="coerce").where(lambda s: s != 0)
        - 1.0
    )
    frame["forward_return"] = intraday.groupby(frame["ticker"], observed=True).shift(-1)
    return frame[["date", "ticker", "forward_return"]].reset_index(drop=True)


def excess_return(
    forward: pd.DataFrame,
    sectors: Mapping[str, str],
) -> pd.DataFrame:
    """§8.4's target: the forward return less its sector's, per session.

    Sector membership is the ``sector:`` field on each watchlist entry, resolved
    the same way :mod:`src.features.compute` resolves it.

    **Singleton sectors take the equal-weighted universe return instead.** With
    one member the sector mean *is* the ticker's own return and the excess is
    identically zero, which is why `_sector_returns` yields ``NaN`` there. Five
    of the ten KR sectors are in that position, covering five of 31 tickers, so
    leaving them out would cut the cross-section by 16% for a reason that is a
    property of the sector table's shape rather than of the data. §8.4 wants the
    sector term to remove market beta; the universe return does that too.
    """
    if forward.empty:
        return pd.DataFrame(columns=["date", "ticker", "excess_return"])

    frame = forward.copy()
    frame["sector"] = frame["ticker"].map(sectors).fillna("")

    members = frame.groupby(["date", "sector"], observed=True)["ticker"].transform("nunique")
    sector_mean = frame.groupby(["date", "sector"], observed=True)["forward_return"].transform(
        "mean"
    )
    universe_mean = frame.groupby("date", observed=True)["forward_return"].transform("mean")

    benchmark = sector_mean.where(members > 1, universe_mean)
    frame["excess_return"] = frame["forward_return"] - benchmark
    return frame[["date", "ticker", "excess_return"]].reset_index(drop=True)


def _in_window(frame: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    if frame.empty:
        return frame
    day = pd.to_datetime(frame["date"]).dt.date
    return frame[(day >= start) & (day <= end)]


def paired(
    ratings: pd.DataFrame,
    excess: pd.DataFrame,
    *,
    start: dt.date = WINDOW_START,
    end: dt.date = WINDOW_END,
) -> pd.DataFrame:
    """Published scores joined to their realised excess return, inside the window.

    Rows where either side is missing are dropped here rather than inside the
    correlation, because :func:`spearman` does not handle ``NaN`` — it ranks
    first and lets pandas drop pairs afterwards, which would shrink the sample
    without saying so.
    """
    if ratings.empty or excess.empty:
        return pd.DataFrame(columns=["date", "ticker", "score", "excess_return"])

    left = _in_window(ratings, start, end)[["date", "ticker", "score"]].copy()
    right = excess.copy()
    for side in (left, right):
        side["date"] = pd.to_datetime(side["date"])

    merged = left.merge(right, on=["date", "ticker"], how="inner")
    return merged.dropna(subset=["score", "excess_return"]).reset_index(drop=True)


def daily_ic(pairs: pd.DataFrame) -> pd.DataFrame:
    """Spearman between score and excess return, one row per session.

    A session enters only if it has both a published rating and a realised
    `t+1` return — so the last session of the window never does, and a session
    whose evening run was dropped by the scheduler simply is not there. The
    series is a list of sessions that produced a number, not a calendar.
    """
    if pairs.empty:
        return pd.DataFrame(columns=["date", "ic", "n"])

    rows = []
    for day, group in pairs.groupby("date", observed=True):
        value = spearman(group["score"].tolist(), group["excess_return"].tolist())
        if value is not None:
            rows.append({"date": day, "ic": value, "n": len(group)})
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def icir(
    ic: pd.Series | Sequence[float],
    *,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, float | None]:
    """Mean IC ÷ its standard deviation, with a bootstrap interval.

    §8.4 defines ICIR as the point estimate and §8.5 keeps `> 0.3` on it. The
    interval is reported alongside because at ~60 sessions the standard error is
    roughly 0.13, which is most of the threshold — a reading without it would be
    more confident than the sample supports.

    Resampling is over sessions, which treats the daily ICs as exchangeable. It
    does not model autocorrelation between consecutive sessions; where that
    exists the interval is optimistic, and this is stated rather than corrected
    because a correction chosen after seeing the series is not preregistered.
    """
    values = np.asarray(pd.Series(list(ic)).dropna(), dtype=float)
    if values.size < 2:
        return {
            "icir": None,
            "mean_ic": None,
            "sd_ic": None,
            "low": None,
            "high": None,
            "n": int(values.size),
        }

    mean = float(values.mean())
    sd = float(values.std(ddof=1))
    point = mean / sd if sd else None

    rng = np.random.default_rng(seed)
    sample = rng.choice(values, size=(draws, values.size), replace=True)
    sds = sample.std(axis=1, ddof=1)
    ratios = np.divide(sample.mean(axis=1), sds, out=np.full(draws, np.nan), where=sds > 0)
    ratios = ratios[~np.isnan(ratios)]

    low, high = (
        (float(np.percentile(ratios, 2.5)), float(np.percentile(ratios, 97.5)))
        if ratios.size
        else (None, None)
    )
    return {
        "icir": point,
        "mean_ic": mean,
        "sd_ic": sd,
        "low": low,
        "high": high,
        "n": int(values.size),
    }


def quantile_spread(pairs: pd.DataFrame, *, quantile: float = QUANTILE) -> dict[str, float | None]:
    """§8.4's spread: top-quantile excess return minus bottom-quantile, per session.

    Computed session by session and then averaged, rather than pooling every
    (ticker, session) pair and splitting once. Pooling would let a session with
    an unusually wide score distribution decide which bucket a ticker lands in
    on a different day.
    """
    if pairs.empty:
        return {"spread": None, "top": None, "bottom": None, "n": 0}

    tops, bottoms = [], []
    for _, group in pairs.groupby("date", observed=True):
        size = max(1, int(np.ceil(len(group) * quantile)))
        if len(group) < 2 * size:
            continue
        ordered = group.sort_values("score")
        bottoms.append(ordered["excess_return"].head(size).mean())
        tops.append(ordered["excess_return"].tail(size).mean())

    if not tops:
        return {"spread": None, "top": None, "bottom": None, "n": 0}

    top, bottom = float(np.mean(tops)), float(np.mean(bottoms))
    return {"spread": top - bottom, "top": top, "bottom": bottom, "n": len(tops)}


def _cell(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def load(root: Path = DATA, watchlist: Sequence[WatchlistEntry] | None = None) -> pd.DataFrame:
    """Everything the metrics need, joined: published scores and realised returns."""
    entries = list(watchlist) if watchlist is not None else load_watchlist(market="KR")
    prices = load_raw(root / "raw", "kr/price", key=("date", "ticker"))
    ratings = load_rating_history(root)
    sectors = {e.ticker: (e.sector or "") for e in entries}
    return paired(ratings, excess_return(forward_return(prices), sectors))


def report(pairs: pd.DataFrame, *, start: dt.date = WINDOW_START, end: dt.date = WINDOW_END) -> str:
    """The §8.4 table. It reports whether the §8.5 criterion was met; it does not decide."""
    ic = daily_ic(pairs)
    stats = icir(ic["ic"] if not ic.empty else [])
    spread = quantile_spread(pairs)

    lines = [
        "# Signal evaluation — PREREGISTRATION §8.4",
        "",
        f"Window **{start} → {end}**, read at the §8.5 3-month gate. "
        f"{len(ic)} session(s) produced an IC; {len(pairs)} (ticker, session) pairs entered.",
        "",
        "> **This table does not decide.** §8.5 sets the criterion at "
        f"`ICIR > {ICIR_THRESHOLD}` on the point estimate and asks separately whether the "
        "shadow portfolio beat KODEX 200 buy-and-hold; both parts are read together, and "
        "the response to a miss is written down there rather than chosen here.",
        "",
    ]

    if len(ic) < 2:
        lines += [
            f"**Not enough sessions to report.** The window opened {start} and a session "
            "enters only once its `t+1` return exists. Nothing below would mean anything yet.",
            "",
        ]
        return "\n".join(lines)

    lines += [
        "## ICIR",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| mean IC | {_cell(stats['mean_ic'])} |",
        f"| sd IC | {_cell(stats['sd_ic'])} |",
        f"| **ICIR** | **{_cell(stats['icir'], 2)}** |",
        f"| bootstrap 95% | [{_cell(stats['low'], 2)}, {_cell(stats['high'], 2)}] |",
        f"| sessions | {stats['n']} |",
        f"| §8.5 criterion | > {ICIR_THRESHOLD} |",
        "",
        f"At {stats['n']} sessions the standard error of ICIR is roughly "
        f"{1 / max(stats['n'], 1) ** 0.5:.2f}. **A point estimate above the threshold whose "
        "interval straddles zero passes the gate as written and settles nothing** — §8.5 "
        "fixed both that reading and the threshold before this number existed.",
        "",
        "## Quantile spread",
        "",
        f"Top {QUANTILE:.0%} minus bottom {QUANTILE:.0%} by composite score, per session, "
        "then averaged.",
        "",
        "| bucket | mean excess return |",
        "|---|---:|",
        f"| top | {_cell(spread['top'], 4)} |",
        f"| bottom | {_cell(spread['bottom'], 4)} |",
        f"| **spread** | **{_cell(spread['spread'], 4)}** |",
        f"| sessions | {spread['n']} |",
        "",
        "## Daily IC",
        "",
        "| session | IC | tickers |",
        "|---|---:|---:|",
    ]
    for row in ic.itertuples(index=False):
        lines.append(f"| {pd.Timestamp(row.date):%Y-%m-%d} | {row.ic:+.3f} | {row.n} |")

    lines += [
        "",
        "**`news_polarity` IC is not here.** §8.4 asks for it as a separate diagnostic and "
        "it has no producer (SPEC §12 steps 6–8). Deferred on the record in §8.5, not dropped.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PREREGISTRATION §8.4 signal evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    reporter = sub.add_parser("report", help="print the §8.4 table")
    reporter.add_argument("--start", type=dt.date.fromisoformat, default=WINDOW_START)
    reporter.add_argument("--end", type=dt.date.fromisoformat, default=WINDOW_END)

    args = parser.parse_args(argv)
    if args.command == "report":
        pairs = load()
        pairs = _in_window(pairs, args.start, args.end)
        print(report(pairs, start=args.start, end=args.end))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
