"""Compute the SPEC §5 rating features. SPEC §12 step 9.

Turns the raw collector output in ``data/raw/`` into the z-scores
:func:`src.report.rating.rate` consumes. The reasoning behind every choice here
is in ``notes/step9-plan.md``; this docstring carries the conclusions.

**Five of the seven rating features are built.** The other two are not, and
neither is an oversight:

* ``news_polarity`` (0.20) comes from the LLM scoring stage in SPEC §6.2, which
  does not exist yet.
* ``rev_4w`` (0.15) is the 4-week change in **consensus** EPS — forward analyst
  estimates. No collector in this repository provides that. pykrx's ``EPS`` is
  *trailing*, and substituting it would produce a number that looks like the
  feature and is not. Doing it properly needs an estimates vendor, which is a
  source decision rather than something to improvise.

The five that are built carry 0.75 of 1.10 total weight, comfortably above
``min_weight_coverage: 0.5``, so :func:`rate` produces real ratings today and
records the gap in ``weight_coverage`` rather than hiding it.

**``valuation_band`` is not z-scored, deliberately.** SPEC §5 says every feature
is a 252-day rolling z-score, but ``valuation_band`` is defined as a 3-year PBR
percentile. Applying both needs 756 + 252 = 1,008 sessions before the first
value, which the stored window will not reach for years, so the feature would be
permanently ``NaN`` and would read as a bug. A percentile is *already*
ticker-relative and bounded,
which is the one thing §5's z-score exists to achieve — so the second
normalization buys nothing here while costing a year of history. It is scaled to
``(0.5 − pct) × 2`` so that cheap is positive, matching its ``+0.05`` weight.

Even so it needs 756 sessions and the stored window is still short — 732 as of
2026-08-07. **The shortfall closes on its own**, one session per KRX day, on
2026-09-11 for 30 tickers and 2026-11-12 for 454910, which listed 40 sessions
late. Extending the backfill to four years only brings that forward. Until then
the feature is absent and the other four carry 0.70 of the 0.75 in ``weights``.

**``short_ratio`` carries a disclosure lag, and that lag is a second look-ahead
boundary.** KRX publishes a session's short-sale balance about three sessions
later, so the balance stored against session ``D`` was not knowable on ``D``.
``known_at_utc`` cannot express that — it is stamped once per row, at the
session close, for every column alike — so :func:`_visible` does not catch it
and never could. The lag is therefore applied here, in the feature definition;
see :func:`compute`. Anyone reading :func:`_visible` and concluding that the
look-ahead boundary is handled in one place is reading half of it.

**Nothing here has been verified against real data by its author.** The tests
are offline and use synthetic frames.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import pandas as pd

from src.collectors.kr_flow import SHORT_LAG_SESSIONS
from src.features.normalize import WINDOW, rolling_percentile, rolling_z
from src.util.config import WatchlistEntry
from src.util.session import to_utc

# SPEC §5 windows, in trading sessions.
FLOW_WINDOW = 5
RETURN_WINDOW = 20
VALUATION_WINDOW = 756  # three years

# The features this module produces. news_polarity and rev_4w are absent by
# design — see the module docstring.
FEATURES = (
    "foreign_flow_5d",
    "inst_flow_5d",
    "short_ratio",
    "rel_strength_20d",
    "valuation_band",
)

# valuation_band arrives already normalized; everything else gets the §5 z-score.
_NOT_Z_SCORED = frozenset({"valuation_band"})


# Prefix for the placeholder sector a sectorless ticker gets, chosen so it cannot
# collide with a real `sector:` value in config/watchlist.yaml.
#
# **Not `\x00`**, which was the first attempt and is silently broken here even
# though `src.entity.resolve` masks with it for good reason. pandas compares and
# hashes object-dtype strings through a path that truncates at the NUL, so
# `"\x00005930"` and `"\x00000660"` are one value: `Series.unique()` returns a
# single entry and `groupby` puts both tickers in one group — re-creating the
# exact pooling this constant exists to prevent, invisibly. Caught by
# `test_a_sectorless_ticker_gets_a_group_of_its_own_not_a_shared_empty_one`.
NO_SECTOR = "__no_sector__:"


def sector_map(entries: Iterable[WatchlistEntry]) -> dict[str, str]:
    """Ticker → sector, with a *unique* placeholder where the entry has none.

    ``config/watchlist.yaml`` requires only ``ticker`` and ``name``, so
    ``sector`` may be absent. Collapsing those onto one shared value — the
    obvious ``e.sector or ""`` — makes every sectorless ticker a member of one
    pseudo-sector, and two of them become each other's benchmark: the sector
    mean of a two-member group is their average, so the excess return is a
    comparison between two companies that have nothing to do with each other.
    It fails silently, because the result is a plausible number rather than
    ``NaN``.

    A per-ticker placeholder instead makes each sectorless ticker a singleton,
    which is the case both consumers already handle and document —
    :func:`_sector_returns` yields ``NaN`` there and
    ``src.eval.ic.excess_return`` substitutes the universe return. The
    placeholder must be *distinct per ticker* and must survive a pandas
    ``groupby``; see :data:`NO_SECTOR` for why the obvious sentinel does not.

    Shared by this module and ``src.eval.ic`` rather than written twice, since
    the live feature and the metric the 3-month gate reads must not disagree
    about what a ticker's sector is.
    """
    return {e.ticker: (e.sector or f"{NO_SECTOR}{e.ticker}") for e in entries}


def _visible(frame: pd.DataFrame, as_of: pd.Timestamp | None) -> pd.DataFrame:
    """Rows knowable strictly before ``as_of``.

    Filtered on ``known_at_utc`` rather than on ``date``. For a KR session the
    two differ by the length of the trading day, and using ``date`` would make
    the whole session's data available from midnight — the look-ahead CLAUDE.md
    exists to prevent. ``<`` rather than ``<=``: a row is not usable at the
    instant it becomes known.
    """
    if frame.empty or as_of is None:
        return frame
    boundary = to_utc(as_of)
    known = pd.to_datetime(frame["known_at_utc"], utc=True)
    return frame[known < boundary]


def _ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Divide, returning NaN rather than inf where the denominator vanishes.

    A zero denominator here means the ticker did not trade across the whole
    window. Zero would assert balanced flow on a ticker that had no flow at all.
    """
    num = pd.to_numeric(numerator, errors="coerce").astype("float64")
    den = pd.to_numeric(denominator, errors="coerce").astype("float64")
    return num / den.where(den != 0)


def _sector_returns(prices: pd.DataFrame, sectors: Mapping[str, str], window: int) -> pd.Series:
    """Equal-weighted mean ``window``-session return of each ticker's sector.

    Returns NaN where a sector holds only one watchlist ticker. Relative
    strength against oneself is identically zero, and a zero would read as "this
    ticker moved with its sector" when the truth is that there is no sector to
    compare against. SPEC §5 defines the feature as a difference; with one
    member the difference is not defined.
    """
    frame = prices.copy()
    frame["sector"] = frame["ticker"].map(sectors)
    frame["ret"] = frame.groupby("ticker", observed=True)["close"].transform(
        lambda s: pd.to_numeric(s, errors="coerce").pct_change(window)
    )

    members = frame.groupby(["date", "sector"], observed=True)["ticker"].transform("nunique")
    sector_mean = frame.groupby(["date", "sector"], observed=True)["ret"].transform("mean")
    return sector_mean.where(members > 1)


def compute(
    flow: pd.DataFrame,
    prices: pd.DataFrame,
    watchlist: Sequence[WatchlistEntry],
    *,
    as_of: pd.Timestamp | None = None,
    window: int = WINDOW,
    valuation_window: int = VALUATION_WINDOW,
    short_lag: int = SHORT_LAG_SESSIONS,
) -> pd.DataFrame:
    """Compute every feature and its z-score, one row per (ticker, session).

    ``as_of`` is the look-ahead boundary and is not optional in spirit: passing
    ``None`` means "no boundary" and suits only a historical rebuild, where the
    boundary is applied when the features are consumed. It is not the *only*
    boundary — ``short_lag`` is the other; see ``short_ratio`` below.

    The returned frame carries both ``{feature}`` and ``{feature}_z``. ``rate()``
    needs only the z-score, but the MANUAL-TASKS §6 calibration cannot judge
    whether the cut points are sane without seeing the raw distributions, and a
    z-score alone cannot be checked against a broker screen.
    """
    flow = _visible(flow, as_of)
    prices = _visible(prices, as_of)
    if not flow.empty:
        flow = flow.sort_values(["ticker", "date"])
    if not prices.empty:
        prices = prices.sort_values(["ticker", "date"])
    sectors = sector_map(watchlist)

    if flow.empty:
        return pd.DataFrame(columns=["date", "ticker", *FEATURES, *(f"{f}_z" for f in FEATURES)])

    out = flow[["date", "ticker"]].copy()
    grouped = flow.groupby("ticker", observed=True, group_keys=False)

    # --- flows ------------------------------------------------------------
    # SPEC §5: cumulative net buying over 5 sessions ÷ cumulative trading value
    # over the same 5. Summing both before dividing, rather than averaging daily
    # ratios, is what makes a heavy session count for more than a quiet one.
    turnover = grouped["trading_value"].apply(
        lambda s: pd.to_numeric(s, errors="coerce").rolling(FLOW_WINDOW).sum()
    )
    for name, column in (("foreign_flow_5d", "foreign_net"), ("inst_flow_5d", "inst_net")):
        net = grouped[column].apply(
            lambda s: pd.to_numeric(s, errors="coerce").rolling(FLOW_WINDOW).sum()
        )
        out[name] = _ratio(net, turnover)

    # --- short interest ---------------------------------------------------
    # KRX discloses a session's short balance about three sessions later, so the
    # balance stored against session D was not knowable on D. Two consequences,
    # and this shift is the single fix for both:
    #
    #   Without it the rated session — always the newest one, whose balance has
    #   not published — divides by a missing numerator and yields NaN. Measured
    #   2026-08-13: short_ratio was absent from all 31 tickers of 5 of the 6
    #   sessions published to that date, the exception being 2026-08-03, which
    #   was computed off the backfill. That is 0.10 of the 0.75 live weight.
    #
    #   And the historical series read the balance on the session it describes
    #   rather than the session it was published on, which is look-ahead — the
    #   backtest kind, invisible today because nothing has computed a return yet.
    #
    # The ratio is formed on its own session and *then* shifted, rather than
    # shifting the numerator alone. A D−3 balance over a D share count mixes two
    # dates and stops corresponding to KRX's own published 비중, which kr_flow
    # stores beside it precisely so the derived value can be checked against it.
    #
    # `short_lag` defaults to kr_flow.SHORT_LAG_SESSIONS, where the same number
    # means "how many recent sessions the missing-ratio check must forgive".
    # One number, two roles, because both descend from one fact about KRX rather
    # than by coincidence — imported rather than copied so they cannot drift. If
    # the two ever need to differ, split them then.
    #
    # Rows inside the first `short_lag` sessions of a ticker's history stay NaN:
    # absent rather than approximated, the same stance valuation_band takes.
    ratio = _ratio(flow["short_balance"], flow["shares_outstanding"])
    out["short_ratio"] = ratio.groupby(flow["ticker"], observed=True).shift(short_lag)

    # --- valuation --------------------------------------------------------
    # Already normalized; see the module docstring. Inverted so cheap is
    # positive, matching the +0.05 weight in config/rating.yaml.
    # Exposed as a parameter rather than fixed, because the default of 756 is
    # more history than the backfill currently holds — the feature is absent
    # until the window extends, and a caller with a longer dataset should be
    # able to turn it on without editing this module.
    percentile = grouped["pbr"].apply(lambda s: rolling_percentile(s, window=valuation_window))
    out["valuation_band"] = (0.5 - percentile) * 2

    # --- relative strength ------------------------------------------------
    if prices.empty:
        out["rel_strength_20d"] = pd.NA
    else:
        returns = prices.copy()
        returns["ret"] = returns.groupby("ticker", observed=True)["close"].transform(
            lambda s: pd.to_numeric(s, errors="coerce").pct_change(RETURN_WINDOW)
        )
        returns["sector_ret"] = _sector_returns(prices, sectors, RETURN_WINDOW)
        returns["rel_strength_20d"] = returns["ret"] - returns["sector_ret"]
        out = out.merge(
            returns[["date", "ticker", "rel_strength_20d"]], on=["date", "ticker"], how="left"
        )

    # --- normalization ----------------------------------------------------
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    for feature in FEATURES:
        if feature in _NOT_Z_SCORED:
            out[f"{feature}_z"] = out[feature]
            continue
        out[f"{feature}_z"] = out.groupby("ticker", observed=True, group_keys=False)[feature].apply(
            lambda s: rolling_z(s, window=window)
        )

    return out[["date", "ticker", *FEATURES, *(f"{f}_z" for f in FEATURES)]]


def z_scores_for(features: pd.DataFrame, ticker: str, day: dt.date) -> dict[str, float | None]:
    """The mapping :func:`src.report.rating.rate` expects, for one ticker-session.

    ``NaN`` becomes ``None`` deliberately. ``rate()`` treats ``None`` as absent
    and renormalizes the remaining weights, whereas a ``NaN`` would propagate
    through the weighted sum and turn the whole composite into ``NaN``.
    """
    rows = features[
        (features["ticker"] == ticker) & (pd.to_datetime(features["date"]).dt.date == day)
    ]
    if rows.empty:
        return dict.fromkeys(FEATURES)

    row = rows.iloc[0]
    scores: dict[str, float | None] = {}
    for feature in FEATURES:
        value = row.get(f"{feature}_z")
        scores[feature] = None if pd.isna(value) else float(value)
    return scores


def load_raw(root: Path, source: str, *, key: Sequence[str] = ("date", "ticker")) -> pd.DataFrame:
    """Read every per-session parquet a collector wrote, oldest first.

    ``-v2`` re-run files are read alongside the originals, which CLAUDE.md rule 1
    guarantees exist rather than replacing them; de-duplication keeps the later
    write, since that is the one a re-run was performed to obtain.

    ``key`` is what identifies a row for de-duplication. It defaults to the
    per-ticker collectors' shape, but ``macro`` is keyed ``("date", "series")``
    and reading it with the default raises ``KeyError: Index(['ticker'])``.
    Parameterizing is better than a second loader: the ``-v2`` ordering rule
    below is the subtle part, and it should exist exactly once.
    """
    directory = root / source
    if not directory.exists():
        return pd.DataFrame()

    # Sorted so a `-v2` revision lands *after* the original it supersedes.
    # Plain filename order does the opposite: "-" sorts before ".", so
    # 2024-01-02-v2.parquet precedes 2024-01-02.parquet and a keep="last"
    # de-duplication would silently discard the re-run rather than the
    # superseded write — the exact inversion of what CLAUDE.md rule 1 intends.
    def order(path: Path) -> tuple[str, int]:
        stem = path.stem.removesuffix(".parquet")
        day, _, version = stem.partition("-v")
        return day, int(version) if version.isdigit() else 1

    frames = [pd.read_parquet(path) for path in sorted(directory.glob("*.parquet"), key=order)]
    if not frames:
        return pd.DataFrame()

    frame = pd.concat(frames, ignore_index=True)
    missing = [column for column in key if column not in frame.columns]
    if missing:
        raise KeyError(f"{source} has no column {missing}; pass key= for this collector's shape")
    return frame.drop_duplicates(subset=list(key), keep="last").reset_index(drop=True)
