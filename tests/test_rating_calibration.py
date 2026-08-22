"""Tests for the `config/rating.yaml` calibration support tool.

Offline, against hand-built synthetic frames — the same "answers known in
advance" stance `tests/test_ic.py` takes, and for the same reason: this
module's job is arithmetic over real archived/recomputed data, and the only
way to check the arithmetic before trusting it against that data is a case
where the expected numbers were worked out by hand first.

`load()`'s disk I/O (reading `data/ratings/` and recomputing from
`data/raw/`) is not tested here, matching `src.eval.ic`'s own tests — every
function below takes a DataFrame directly, so the untested part is a thin
wiring layer with nothing to compute wrong.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.eval.rating_calibration import (
    CalibrationData,
    _long_features,
    bucket_counts,
    cut_point_table,
    report,
    score_percentiles,
    weight_influence_table,
)
from src.report.rating import Rating, bucket_from_score

CUT_POINTS = {"strong": 2.0, "moderate": 1.0, "weak": 0.4}


def scores_frame(rows: list[dict]) -> pd.DataFrame:
    """`date, ticker, rating, score, weight_coverage, missing` — the shape
    `load_rating_history()` returns."""
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp(r.get("date", "2026-08-13")),
                "ticker": r.get("ticker", "005930"),
                "rating": r["rating"],
                "score": r["score"],
                "weight_coverage": r.get("weight_coverage", 1.0),
                "missing": r.get("missing", ""),
            }
            for r in rows
        ]
    )


def features_frame(rows: list[tuple[str, str, str, float]]) -> pd.DataFrame:
    """`(date, ticker, feature, z)` tuples → the shape `_long_features` produces."""
    return pd.DataFrame(rows, columns=["date", "ticker", "feature", "z"])


# --- score_percentiles -------------------------------------------------------


def test_score_percentiles_matches_numpy_on_a_small_sample():
    scores = scores_frame([{"rating": "관망", "score": s} for s in (0.1, -0.5, 1.2, -2.0, 0.9)])
    magnitude = [0.1, 0.5, 1.2, 2.0, 0.9]
    result = score_percentiles(scores)
    for p in (50, 90):
        assert result[p] == pytest.approx(float(np.percentile(magnitude, p)))


def test_score_percentiles_on_empty_frame_is_empty_not_a_crash():
    assert score_percentiles(scores_frame([])) == {}


# --- cut_point_table ---------------------------------------------------------


def test_cut_point_table_share_clearing_is_the_fraction_at_or_above_threshold():
    # |score| values: 0.1, 0.4, 1.0, 2.0, 3.0 — one at each boundary exactly.
    scores = scores_frame([{"rating": "관망", "score": s} for s in (0.1, 0.4, -1.0, 2.0, -3.0)])
    table = cut_point_table(scores, CUT_POINTS).set_index("cut_point")
    # weak=0.4: 4 of 5 rows clear it (0.4, 1.0, 2.0, 3.0).
    assert table.loc["weak", "share_clearing"] == pytest.approx(4 / 5)
    # moderate=1.0: 3 of 5 (1.0, 2.0, 3.0) — inclusive boundary.
    assert table.loc["moderate", "share_clearing"] == pytest.approx(3 / 5)
    # strong=2.0: 2 of 5 (2.0, 3.0).
    assert table.loc["strong", "share_clearing"] == pytest.approx(2 / 5)
    assert (table["n"] == 5).all()


def test_cut_point_table_on_empty_scores_reports_n_zero_not_a_crash():
    table = cut_point_table(scores_frame([]), CUT_POINTS)
    assert (table["n"] == 0).all()
    assert table["share_clearing"].isna().all()


# --- bucket_counts -------------------------------------------------------------


def test_bucket_counts_agrees_when_cut_points_are_unchanged():
    rows = [
        {"rating": str(bucket_from_score(s, CUT_POINTS)), "score": s}
        for s in (2.5, 1.5, 0.5, 0.1, -2.5)
    ]
    scores = scores_frame(rows)
    table = bucket_counts(scores, CUT_POINTS).set_index("rating")
    for row in rows:
        assert table.loc[row["rating"], "as_published"] >= 1
    assert (table["as_published"] == table["re_bucketed"]).all()


def test_bucket_counts_diverges_when_cut_points_changed():
    # Published under the original ladder: 0.5 → 약한 매수.
    original = str(bucket_from_score(0.5, CUT_POINTS))
    scores = scores_frame([{"rating": original, "score": 0.5}])

    tightened = {"strong": 2.0, "moderate": 1.0, "weak": 0.6}  # 0.5 now misses weak
    table = bucket_counts(scores, tightened).set_index("rating")
    assert table.loc[original, "as_published"] == 1
    assert table.loc[original, "re_bucketed"] == 0
    assert table.loc[str(Rating.HOLD), "re_bucketed"] == 1


def test_bucket_counts_replicates_the_low_confidence_force_hold():
    """A row published as 관망 only because its coverage sat below
    `min_weight_coverage` — not because its score was small — must still
    re-bucket to 관망 under an unchanged config. A version of this function
    that re-bucketed on `score` alone would have shown this as a spurious
    "cut points changed" divergence for every such row."""
    scores = scores_frame([{"rating": str(Rating.HOLD), "score": 1.5, "weight_coverage": 0.3}])
    table = bucket_counts(scores, CUT_POINTS, min_weight_coverage=0.5).set_index("rating")
    assert table.loc[str(Rating.HOLD), "as_published"] == 1
    assert table.loc[str(Rating.HOLD), "re_bucketed"] == 1
    assert table.loc[str(Rating.BUY), "re_bucketed"] == 0


def test_bucket_counts_on_empty_scores_lists_every_rating_at_zero():
    table = bucket_counts(scores_frame([]), CUT_POINTS)
    assert set(table["rating"]) == {str(r) for r in Rating}
    assert (table["as_published"] == 0).all()
    assert (table["re_bucketed"] == 0).all()


# --- weight_influence_table ----------------------------------------------------


def test_weight_influence_table_designed_and_realized_share():
    weights = {"a": 0.6, "b": 0.4}
    features = features_frame(
        [
            ("2026-08-13", "005930", "a", 1.0),
            ("2026-08-14", "005930", "a", -1.0),
            ("2026-08-15", "005930", "a", 2.0),
            ("2026-08-13", "005930", "b", 0.5),
            ("2026-08-14", "005930", "b", -0.5),
        ]
    )
    table = weight_influence_table(features, weights).set_index("feature")

    assert table.loc["a", "designed_share"] == pytest.approx(0.6)
    assert table.loc["b", "designed_share"] == pytest.approx(0.4)
    assert table.loc["a", "n"] == 3
    assert table.loc["a", "mean_abs_z"] == pytest.approx((1.0 + 1.0 + 2.0) / 3)
    assert table.loc["b", "mean_abs_z"] == pytest.approx(0.5)
    # mean |weight*z|: a = 0.6*1.3333=0.8, b = 0.4*0.5=0.2, total = 1.0.
    assert table.loc["a", "realized_share"] == pytest.approx(0.8)
    assert table.loc["b", "realized_share"] == pytest.approx(0.2)
    assert table.loc["a", "delta"] == pytest.approx(0.8 - 0.6)
    assert table.loc["b", "delta"] == pytest.approx(0.2 - 0.4)


def test_weight_influence_table_a_feature_with_no_rows_reports_none_not_zero():
    """A feature the recompute never produced a z-score for (e.g. weight
    listed but the raw archive lacks it) must not read as "zero pull" —
    that's a different, false claim from "no data"."""
    weights = {"a": 0.5, "b": 0.5}
    features = features_frame([("2026-08-13", "005930", "a", 1.0)])
    table = weight_influence_table(features, weights).set_index("feature")
    assert table.loc["b", "n"] == 0
    # Stored as None in the row dict; pandas coerces a mixed None/float column
    # to NaN on read-back, which is what a plain `== 0` comparison would miss.
    assert pd.isna(table.loc["b", "mean_abs_z"])
    assert pd.isna(table.loc["b", "realized_share"])


def test_weight_influence_table_on_empty_features_returns_empty_frame():
    table = weight_influence_table(features_frame([]), {"a": 1.0})
    assert table.empty


# --- _long_features ------------------------------------------------------------


def test_long_features_melts_and_strips_the_z_suffix():
    wide = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-08-13")],
            "ticker": ["005930"],
            "foreign_flow_5d": [0.1],
            "foreign_flow_5d_z": [1.5],
            "valuation_band": [0.2],
            "valuation_band_z": [-0.3],
        }
    )
    long = _long_features(wide)
    got = dict(zip(long["feature"], long["z"], strict=True))
    assert got == {"foreign_flow_5d": 1.5, "valuation_band": -0.3}


def test_long_features_drops_nan_z_rows():
    wide = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-08-13")],
            "ticker": ["005930"],
            "foreign_flow_5d": [0.1],
            "foreign_flow_5d_z": [np.nan],
        }
    )
    assert _long_features(wide).empty


def test_long_features_on_empty_wide_frame_is_empty_not_a_crash():
    assert _long_features(pd.DataFrame(columns=["date", "ticker"])).empty


# --- report: surfaces, never decides -------------------------------------------

_VERDICT_WORDS = (
    "degenerate",
    "sane",
    "reasonable",
    "pass",
    "fail",
    "good",
    "bad",
    "should",
    "recommend",
)


def test_report_never_states_a_verdict():
    """The line this deliverable exists to respect: cut points and weights
    are Ricky's call, never this tool's. A verdict word here would be the
    first step toward a second one silently doing the deciding."""
    scores = scores_frame(
        [{"rating": str(bucket_from_score(s, CUT_POINTS)), "score": s} for s in (2.5, 0.1)]
    )
    features = features_frame([("2026-08-13", "005930", "foreign_flow_5d", 1.0)])
    data = CalibrationData(
        scores=scores, features=features, weights={"foreign_flow_5d": 1.0}, cut_points=CUT_POINTS
    )
    text = report(data).lower()
    for word in _VERDICT_WORDS:
        assert word not in text, f"report used verdict language: {word!r}"


def test_report_states_the_sample_size():
    scores = scores_frame([{"rating": "관망", "score": 0.1, "date": "2026-08-13"}])
    data = CalibrationData(
        scores=scores, features=features_frame([]), weights={}, cut_points=CUT_POINTS
    )
    text = report(data)
    assert "1 archived session" in text
    assert "1 (ticker, session) row" in text


def test_report_on_entirely_empty_data_does_not_crash():
    data = CalibrationData(
        scores=scores_frame([]), features=features_frame([]), weights={}, cut_points=CUT_POINTS
    )
    text = report(data)
    assert "No archived ratings" in text
    assert "No recomputed feature history" in text
