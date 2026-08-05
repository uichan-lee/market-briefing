"""Rolling z-score normalization. SPEC §5.

    z(i,t) = (x(i,t) − μ(i, t-252:t-1)) / σ(i, t-252:t-1)

Every feature in SPEC §5 is normalized this way, per ticker, so that a value can
be compared across tickers whose absolute scales have nothing to do with each
other — 삼성전자's daily trading value and 한성기업's differ by three orders of
magnitude, and the weighted sum in :mod:`src.report.rating` would otherwise be
dominated by whichever ticker happens to be largest.

Three properties of the window are load-bearing.

**It ends at t-1.** SPEC writes the window as ``t-252 : t-1``, and that is not
notation. Including row ``t`` in its own mean and standard deviation leaks the
observation into its own normalization: a genuine outlier inflates the σ it is
then divided by, and every extreme is pulled toward zero exactly when it matters
most. It is also a look-ahead violation in the strict sense CLAUDE.md sets out,
since the statistic at ``t`` would depend on data timestamped at ``t``.

**A short window yields nothing rather than something.** With fewer than the
required observations the result is ``NaN``, not a z-score over whatever is
available. A value normalized over 40 days and one normalized over 252 are not
on the same scale, and averaging them into one composite would be meaningless in
a way no downstream check could detect.

**Zero variance yields ``NaN``, never infinity.** A feature constant across the
whole window has σ = 0. Dividing by it produces ``inf``, which would swamp every
weighted sum it entered; the honest answer is that a constant series carries no
information about how unusual today is.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# SPEC §5. One trading year.
WINDOW = 252


def rolling_z(
    series: pd.Series, *, window: int = WINDOW, min_periods: int | None = None
) -> pd.Series:
    """Trailing z-score of ``series``, excluding the current observation.

    ``series`` must be ordered oldest-first and hold one ticker's history. The
    index is preserved, so the result aligns back onto the frame it came from.

    ``min_periods`` defaults to ``window``: SPEC's window is a requirement, not a
    hint. Lowering it is possible for tests and for features with a deliberately
    shorter definition, but doing so on the production path means emitting
    z-scores that are not comparable to each other.
    """
    if min_periods is None:
        min_periods = window

    values = pd.to_numeric(series, errors="coerce").astype("float64")

    # shift(1) is what makes the window end at t-1. Everything else here is
    # ordinary pandas; this one call is the whole look-ahead guarantee.
    trailing = values.shift(1).rolling(window=window, min_periods=min_periods)

    mean = trailing.mean()
    # ddof=1: the window is a sample of the ticker's history, not its population.
    sigma = trailing.std(ddof=1)

    # A constant window gives sigma == 0. Masking to NaN before the division
    # avoids inf; comparing against 0 exactly is right because the only way to
    # reach it is genuine constancy, not floating-point drift.
    sigma = sigma.where(sigma > 0)

    return (values - mean) / sigma


def rolling_percentile(
    series: pd.Series, *, window: int, min_periods: int | None = None
) -> pd.Series:
    """Where the current value sits within its own trailing window, in [0, 1].

    Used for ``valuation_band``, which SPEC §5 defines as a 3-year PBR band
    rather than a z-score. Unlike :func:`rolling_z` the current observation *is*
    included — "where does today sit in the last three years" is a question
    about today, and excluding it would rank the value against a window it is
    not part of.

    That inclusion is not a look-ahead violation: every observation in the
    window, including the current one, is already knowable at ``t``. The
    look-ahead boundary is enforced upstream, on ``known_at_utc``.
    """
    if min_periods is None:
        min_periods = window

    values = pd.to_numeric(series, errors="coerce").astype("float64")

    def rank(chunk: np.ndarray) -> float:
        current = chunk[-1]
        if np.isnan(current):
            return np.nan
        prior = chunk[~np.isnan(chunk)]
        if len(prior) < 2:
            return np.nan
        return float((prior <= current).sum() - 1) / (len(prior) - 1)

    return values.rolling(window=window, min_periods=min_periods).apply(rank, raw=True)


def by_ticker(
    frame: pd.DataFrame, column: str, *, window: int = WINDOW, min_periods: int | None = None
) -> pd.Series:
    """Apply :func:`rolling_z` per ticker over a long frame.

    The frame is expected to hold one row per (ticker, session). Grouping is
    what keeps 삼성전자's history out of 한성기업's mean; a single ungrouped
    rolling window over a long frame would silently mix them and produce numbers
    that look plausible and mean nothing.
    """
    ordered = frame.sort_values(["ticker", "date"])
    result = ordered.groupby("ticker", group_keys=False, observed=True)[column].apply(
        lambda s: rolling_z(s, window=window, min_periods=min_periods)
    )
    return result.reindex(frame.index)
