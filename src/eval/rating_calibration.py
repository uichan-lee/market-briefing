"""Calibration support for `config/rating.yaml`. MANUAL-TASKS §6, Ricky's call.

This module measures; it does not decide — the same opening line
`src.eval.ic` uses, and the same reason applies here: cut points and weights
are Ricky's judgment call (`config/rating.yaml`'s own header comment,
PREREGISTRATION §8.4's rule that cut points may be revised only for
distributional reasons and never against outcome data). This tool writes
nothing to `config/rating.yaml` and its `report()` never states a verdict —
no "degenerate", "sane", "reasonable" — only the numbers Ricky reads to
decide from.

Two independent questions, two independent data sources, because one is much
thinner than the other:

**Cut-point sanity** — is the `weak`/`moderate`/`strong` ladder producing a
usable spread of ratings, or is nearly everything landing in 관망? — reads
`data/ratings/`'s archived `score` column via
`src.report.render.load_rating_history`, the same source `src.eval.ic` uses.
Only a handful of sessions exist there today: publishing started
2026-08-03 and a rerun collapses to one file per day (see
`src.report.render.latest_rating_files`). The report states the sample size
next to every table rather than letting a percentile look equally solid
regardless of N.

**Weight sanity** — does each feature's actual pull on the composite match
the share of weight it was assigned? — needs per-feature z-scores, which
`data/ratings/` never archived (`src.report.render.ratings_frame` writes only
the final composite `score`, never `RatingResult.contributions`).
Recomputing `src.features.compute.compute()` against `data/raw/` supplies
them instead, and unlike the archived scores this is **not** capped at the
same handful of days: `compute()` returns one row per (ticker, session) for
every session its raw price/flow inputs cover — the full backfill, not just
the days a rating happened to be archived. `as_of=None` is used deliberately
(not `now_utc()`): this is a retrospective read of realized history, not a
simulation of what a live render would have seen, so no look-ahead boundary
is needed beyond what `data/raw/` already holds.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src.features.compute import FEATURES, compute, load_raw
from src.report.rating import Rating, bucket_from_score
from src.report.render import load_rating_history
from src.util.config import load_rating, load_watchlist

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

# Percentiles shown in the cut-point table. Chosen to bracket where the three
# default cut points (0.4 / 1.0 / 2.0) plausibly sit in a roughly-normal
# composite, not derived from any measurement.
SCORE_PERCENTILES = (50, 75, 90, 95, 99)


# Mirrors src.report.rating._COVERAGE_TOLERANCE — a plain floating-point
# epsilon for a ratio-of-sums comparison, not a business rule, so duplicating
# it here (rather than reaching into a private name) is the cheap kind of
# repeat: it cannot drift because there is nothing about it to decide twice.
_COVERAGE_TOLERANCE = 1e-9


@dataclass(frozen=True)
class CalibrationData:
    scores: pd.DataFrame  # date, ticker, rating, score, weight_coverage, missing
    features: pd.DataFrame  # date, ticker, feature, z (long; empty if not recomputed)
    weights: Mapping[str, float]
    cut_points: Mapping[str, float]
    min_weight_coverage: float = 0.0


def _long_features(wide: pd.DataFrame) -> pd.DataFrame:
    """`compute()`'s wide `{feature}`/`{feature}_z` columns, melted to one row
    per (date, ticker, feature) — the shape :func:`weight_influence_table` reads."""
    z_columns = [f"{f}_z" for f in FEATURES if f"{f}_z" in wide.columns]
    if wide.empty or not z_columns:
        return pd.DataFrame(columns=["date", "ticker", "feature", "z"])
    long = wide.melt(
        id_vars=["date", "ticker"], value_vars=z_columns, var_name="feature", value_name="z"
    )
    long["feature"] = long["feature"].str.removesuffix("_z")
    return long.dropna(subset=["z"]).reset_index(drop=True)


def load(
    root: Path = DATA,
    config: Mapping[str, object] | None = None,
    *,
    recompute: bool = True,
) -> CalibrationData:
    """Archived scores, plus (unless ``recompute=False``) recomputed feature
    z-scores across every session the raw archive covers."""
    config = config if config is not None else load_rating()
    weights = {name: float(w) for name, w in (config.get("weights") or {}).items()}
    cut_points = {name: float(v) for name, v in (config.get("cut_points") or {}).items()}
    min_coverage = float((config.get("confidence") or {}).get("min_weight_coverage", 0.0))

    scores = load_rating_history(root)

    features = pd.DataFrame(columns=["date", "ticker", "feature", "z"])
    if recompute:
        flow = load_raw(root / "raw", "kr/investor_flow")
        prices = load_raw(root / "raw", "kr/price")
        if not flow.empty:
            watchlist = load_watchlist(market="KR")
            wide = compute(flow, prices, watchlist, as_of=None)
            features = _long_features(wide)

    return CalibrationData(
        scores=scores,
        features=features,
        weights=weights,
        cut_points=cut_points,
        min_weight_coverage=min_coverage,
    )


def score_percentiles(scores: pd.DataFrame) -> dict[int, float]:
    """Empirical percentiles of ``|score|`` across every archived row."""
    magnitude = scores["score"].abs().dropna() if not scores.empty else pd.Series(dtype=float)
    if magnitude.empty:
        return {}
    return {p: float(np.percentile(magnitude, p)) for p in SCORE_PERCENTILES}


def cut_point_table(scores: pd.DataFrame, cut_points: Mapping[str, float]) -> pd.DataFrame:
    """For each cut point, the share of archived ``|score|`` rows that clear it.

    Directly answers "is the ladder degenerate" without saying so: a share
    near 0% at ``weak`` means almost everything lands in 관망; a share near
    100% at ``strong`` means almost everything is already 강한 매수/매도.
    """
    magnitude = scores["score"].abs().dropna() if not scores.empty else pd.Series(dtype=float)
    n = len(magnitude)
    rows = []
    for name in ("weak", "moderate", "strong"):
        threshold = cut_points.get(name)
        if threshold is None:
            continue
        share = float((magnitude >= threshold).mean()) if n else None
        rows.append({"cut_point": name, "threshold": threshold, "share_clearing": share, "n": n})
    return pd.DataFrame(rows)


def bucket_counts(
    scores: pd.DataFrame,
    cut_points: Mapping[str, float],
    *,
    min_weight_coverage: float = 0.0,
) -> pd.DataFrame:
    """As-published ``rating`` label counts vs. a re-bucketing of ``score``
    under the *current* ``cut_points``.

    Replicates :func:`src.report.rating.rate`'s low-confidence force-HOLD
    using the archived ``weight_coverage`` column, not just ``bucket_from_score``
    on its own — a first version of this function skipped that and
    consistently under-counted 관망 by exactly the rows that had been forced
    there for thin evidence rather than a small score, which would have read
    as "cut points changed" when nothing had. With coverage accounted for,
    the two columns disagree only where ``cut_points`` or
    ``confidence.min_weight_coverage`` actually changed since a row was
    archived — worth surfacing as a fact, not something this function
    resolves.
    """
    order = [str(r) for r in Rating]
    if scores.empty:
        return pd.DataFrame({"rating": order, "as_published": 0, "re_bucketed": 0})

    published = scores["rating"].value_counts()

    def _rebucket(row: pd.Series) -> str:
        if row["weight_coverage"] < min_weight_coverage - _COVERAGE_TOLERANCE:
            return str(Rating.HOLD)
        return str(bucket_from_score(row["score"], cut_points))

    rebucketed = scores.apply(_rebucket, axis=1).value_counts()
    return pd.DataFrame(
        {
            "rating": order,
            "as_published": [int(published.get(r, 0)) for r in order],
            "re_bucketed": [int(rebucketed.get(r, 0)) for r in order],
        }
    )


def weight_influence_table(features: pd.DataFrame, weights: Mapping[str, float]) -> pd.DataFrame:
    """Per feature: designed weight/share vs. its realized pull on the composite.

    ``mean |z|`` flags a feature whose z-scores aren't actually landing near
    N(0,1) — ``valuation_band`` is the one to check first, since it is a
    rolling *percentile* rather than a rolling z-score (see
    ``src.features.compute``'s ``_NOT_Z_SCORED``). ``realized_share`` is that
    feature's mean ``|weight × z|`` as a share of the average total across all
    features — the concrete answer to "does this feature's actual pull match
    the share of weight it was assigned," which the archived composite
    ``score`` (already a collapsed sum) cannot show on its own.
    """
    total_weight = sum(abs(w) for w in weights.values())
    columns = [
        "feature",
        "weight",
        "designed_share",
        "n",
        "mean_abs_z",
        "mean_abs_contribution",
        "realized_share",
        "delta",
    ]
    if total_weight == 0 or features.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for name, weight in weights.items():
        sub = features[features["feature"] == name]
        n = len(sub)
        mean_abs_z = float(sub["z"].abs().mean()) if n else None
        mean_abs_contribution = abs(weight) * mean_abs_z if mean_abs_z is not None else None
        rows.append(
            {
                "feature": name,
                "weight": weight,
                "designed_share": abs(weight) / total_weight,
                "n": n,
                "mean_abs_z": mean_abs_z,
                "mean_abs_contribution": mean_abs_contribution,
            }
        )

    total_contribution = sum(
        r["mean_abs_contribution"] for r in rows if r["mean_abs_contribution"] is not None
    )
    for row in rows:
        if row["mean_abs_contribution"] is not None and total_contribution:
            row["realized_share"] = row["mean_abs_contribution"] / total_contribution
            row["delta"] = row["realized_share"] - row["designed_share"]
        else:
            row["realized_share"] = None
            row["delta"] = None

    return pd.DataFrame(rows, columns=columns)


def _cell(value: float | None, digits: int = 3, *, pct: bool = False) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "—"
    return f"{value:.1%}" if pct else f"{value:.{digits}f}"


def report(data: CalibrationData) -> str:
    """The calibration tables. States numbers and sample sizes; decides nothing."""
    n_sessions = int(data.scores["date"].nunique()) if not data.scores.empty else 0
    n_rows = len(data.scores)

    lines = [
        "# `config/rating.yaml` calibration support — MANUAL-TASKS §6",
        "",
        "This surfaces real distributions. It does not decide — cut points and "
        "weights are Ricky's call, per `config/rating.yaml`'s own header and "
        "PREREGISTRATION §8.4's rule against calibrating against outcome data.",
        "",
        "## Cut-point distribution",
        "",
        f"From `data/ratings/` — **{n_sessions} archived session(s), {n_rows} "
        "(ticker, session) row(s).** Small by construction: publishing started "
        "2026-08-03 and a rerun collapses to one file per day.",
        "",
    ]

    if data.scores.empty:
        lines += ["No archived ratings found under `data/ratings/`.", ""]
    else:
        pct = score_percentiles(data.scores)
        lines += ["| percentile | \\|score\\| |", "|---:|---:|"]
        for p in SCORE_PERCENTILES:
            lines.append(f"| {p}th | {_cell(pct.get(p))} |")
        lines.append("")

        lines += [
            "| cut point | threshold | share of rows clearing it | n |",
            "|---|---:|---:|---:|",
        ]
        for row in cut_point_table(data.scores, data.cut_points).itertuples(index=False):
            lines.append(
                f"| {row.cut_point} | {row.threshold:g} | "
                f"{_cell(row.share_clearing, pct=True)} | {row.n} |"
            )
        lines.append("")

        lines += [
            "| rating | as published | re-bucketed under current cut_points |",
            "|---|---:|---:|",
        ]
        counts = bucket_counts(
            data.scores, data.cut_points, min_weight_coverage=data.min_weight_coverage
        )
        for row in counts.itertuples(index=False):
            lines.append(f"| {row.rating} | {row.as_published} | {row.re_bucketed} |")
        lines.append("")

    lines += ["## Weight influence", ""]
    if data.features.empty:
        lines += [
            "No recomputed feature history (`recompute=False`, or `data/raw/kr/"
            "investor_flow` is empty).",
            "",
        ]
    else:
        n_feature_sessions = int(data.features["date"].nunique())
        lines += [
            f"Recomputed from `data/raw/` — **{n_feature_sessions} session(s)**, "
            "not capped at the archived-rating count above.",
            "",
            "| feature | weight | designed share | n | mean \\|z\\| | "
            "mean \\|weight×z\\| | realized share | Δ (realized − designed) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for row in weight_influence_table(data.features, data.weights).itertuples(index=False):
            lines.append(
                f"| {row.feature} | {row.weight:+.2f} | {_cell(row.designed_share, pct=True)} "
                f"| {row.n} | {_cell(row.mean_abs_z, 2)} | {_cell(row.mean_abs_contribution, 2)} "
                f"| {_cell(row.realized_share, pct=True)} | {_cell(row.delta, pct=True)} |"
            )
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="config/rating.yaml calibration support")
    sub = parser.add_subparsers(dest="command", required=True)
    reporter = sub.add_parser("report", help="print the calibration tables")
    reporter.add_argument(
        "--no-recompute",
        action="store_true",
        help="skip recomputing features from data/raw/ (cut-point section only)",
    )

    args = parser.parse_args(argv)
    if args.command == "report":
        data = load(recompute=not args.no_recompute)
        print(report(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
