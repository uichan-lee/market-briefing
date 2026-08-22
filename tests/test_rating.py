"""Tests for the directional rating (SPEC §2.2⑥).

Synthetic z-scores only. The rating is pure arithmetic, so all of this is
offline and exactly reproducible — which is the property the rating exists to
have.
"""

from __future__ import annotations

import pytest

from src.report.rating import Rating, RatingConfigError, bucket_from_score, rate
from src.util.config import ConfigError, load_rating

# A deliberately simple config: two features, equal weight, so expected scores
# are obvious by inspection rather than by re-deriving the implementation.
CONFIG = {
    "weights": {"foreign_flow_5d": 0.5, "news_polarity": 0.5},
    "cut_points": {"strong": 2.0, "moderate": 1.0, "weak": 0.4},
    "confidence": {"min_weight_coverage": 0.5, "max_rationale_terms": 4},
}


# --- the scale -----------------------------------------------------------


@pytest.mark.parametrize(
    ("z", "expected"),
    [
        (3.0, Rating.STRONG_BUY),
        (2.0, Rating.STRONG_BUY),  # boundary is inclusive on the outer edge
        (1.5, Rating.BUY),
        (1.0, Rating.BUY),
        (0.6, Rating.WEAK_BUY),
        (0.4, Rating.WEAK_BUY),
        (0.0, Rating.HOLD),
        (-0.3, Rating.HOLD),
        (-0.4, Rating.WEAK_SELL),
        (-1.0, Rating.SELL),
        (-2.0, Rating.STRONG_SELL),
        (-3.5, Rating.STRONG_SELL),
    ],
)
def test_composite_score_maps_to_the_expected_rating(z, expected):
    result = rate("005930", {"foreign_flow_5d": z, "news_polarity": z}, CONFIG)
    assert result.rating is expected


def test_the_scale_is_symmetric():
    """An asymmetric scale would bake in a directional prior invisibly."""
    for z in (0.5, 1.2, 2.4):
        up = rate("005930", {"foreign_flow_5d": z, "news_polarity": z}, CONFIG)
        down = rate("005930", {"foreign_flow_5d": -z, "news_polarity": -z}, CONFIG)
        assert up.score == pytest.approx(-down.score)


def test_score_is_the_weighted_sum():
    result = rate("005930", {"foreign_flow_5d": 2.0, "news_polarity": 0.0}, CONFIG)
    assert result.score == pytest.approx(1.0)  # 0.5*2.0 + 0.5*0.0


def test_a_negative_weight_inverts_the_signal():
    """short_ratio rising is bearish; the sign lives in the config, not the code."""
    config = {**CONFIG, "weights": {"short_ratio": 1.0}}
    assert rate("005930", {"short_ratio": 2.0}, config).score == pytest.approx(2.0)

    inverted = {**CONFIG, "weights": {"short_ratio": -1.0}}
    assert rate("005930", {"short_ratio": 2.0}, inverted).score == pytest.approx(-2.0)


# --- missing data --------------------------------------------------------


def test_a_missing_feature_is_excluded_not_zero_filled():
    """Zero-filling would drag the score toward 관망 while looking fully informed."""
    partial = rate("005930", {"foreign_flow_5d": 2.0, "news_polarity": None}, CONFIG)
    zero_filled = rate("005930", {"foreign_flow_5d": 2.0, "news_polarity": 0.0}, CONFIG)

    assert partial.score == pytest.approx(2.0)  # renormalized over what is present
    assert zero_filled.score == pytest.approx(1.0)
    assert partial.score != zero_filled.score


def test_an_absent_key_is_treated_as_missing():
    result = rate("005930", {"foreign_flow_5d": 2.0}, CONFIG)
    assert result.missing == ("news_polarity",)
    assert result.weight_coverage == pytest.approx(0.5)


def test_missing_features_are_named():
    result = rate("005930", {"foreign_flow_5d": 1.0, "news_polarity": None}, CONFIG)
    assert "news_polarity" in result.missing


def test_thin_evidence_forces_hold_and_flags_it():
    config = {**CONFIG, "confidence": {"min_weight_coverage": 0.9}}
    result = rate("005930", {"foreign_flow_5d": 3.0, "news_polarity": None}, config)
    assert result.rating is Rating.HOLD
    assert result.low_confidence
    assert result.weight_coverage == pytest.approx(0.5)


def test_a_ticker_with_no_data_at_all_is_hold_not_a_confident_zero():
    result = rate("005930", {"foreign_flow_5d": None, "news_polarity": None}, CONFIG)
    assert result.rating is Rating.HOLD
    assert result.low_confidence
    assert result.score == 0.0
    assert result.contributions == ()


def test_full_coverage_is_not_flagged():
    result = rate("005930", {"foreign_flow_5d": 1.5, "news_polarity": 1.5}, CONFIG)
    assert result.weight_coverage == pytest.approx(1.0)
    assert not result.low_confidence


def test_coverage_exactly_on_the_floor_passes():
    """``min_weight_coverage`` is a floor the coverage may sit on: meeting it
    exactly is enough. Found in the first real report on 2026-08-06 — 079550
    covered exactly half its weight, which computes as
    0.55 / 1.10 = 0.49999999999999994, and was demoted to 관망 by float error
    while the page displayed its coverage as 50%."""
    weights = {
        "foreign_flow_5d": 0.30,
        "inst_flow_5d": 0.15,
        "short_ratio": -0.10,
        "news_polarity": 0.20,
        "rev_4w": 0.15,
        "rel_strength_20d": 0.15,
        "valuation_band": 0.05,
    }
    config = {**CONFIG, "weights": weights, "confidence": {"min_weight_coverage": 0.5}}

    result = rate(
        "079550",
        {"foreign_flow_5d": 2.0, "inst_flow_5d": 2.0, "short_ratio": -2.0},
        config,
    )

    assert result.weight_coverage == pytest.approx(0.5)
    assert not result.low_confidence, "coverage sitting exactly on the floor must pass"
    assert result.rating is not Rating.HOLD


def test_coverage_genuinely_below_the_floor_still_fails():
    """The tolerance absorbs float error, not a real shortfall. The smallest
    weight in the committed config moves coverage by 0.045, which is eight
    orders of magnitude above the tolerance."""
    config = {**CONFIG, "confidence": {"min_weight_coverage": 0.6}}
    result = rate("005930", {"foreign_flow_5d": 3.0, "news_polarity": None}, config)
    assert result.weight_coverage == pytest.approx(0.5)
    assert result.low_confidence


# --- the rationale -------------------------------------------------------


def test_rationale_ranks_by_absolute_contribution():
    config = {
        **CONFIG,
        "weights": {"a": 0.1, "b": 0.5, "c": 0.4},
    }
    result = rate("005930", {"a": 3.0, "b": -2.0, "c": 0.1}, config)
    ordered = [c.feature for c in result.rationale()]
    assert ordered == ["b", "a", "c"]  # |−1.0| > |0.3| > |0.04|


def test_rationale_respects_its_limit():
    config = {**CONFIG, "weights": {"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25}}
    result = rate("005930", dict.fromkeys("abcd", 1.0), config)
    assert len(result.rationale(limit=2)) == 2


def test_rationale_omits_missing_features_rather_than_listing_them_as_zero():
    result = rate("005930", {"foreign_flow_5d": 1.0, "news_polarity": None}, CONFIG)
    assert [c.feature for c in result.rationale()] == ["foreign_flow_5d"]


def test_contributions_sum_to_the_unnormalized_score():
    """The rationale must decompose the number, not merely accompany it."""
    z = {"foreign_flow_5d": 1.6, "news_polarity": -0.4}
    result = rate("005930", z, CONFIG)
    assert sum(c.value for c in result.contributions) == pytest.approx(result.score)


def test_contribution_reports_its_own_arithmetic():
    result = rate("005930", {"foreign_flow_5d": 2.0, "news_polarity": 0.0}, CONFIG)
    contribution = next(c for c in result.contributions if c.feature == "foreign_flow_5d")
    assert contribution.value == pytest.approx(contribution.weight * contribution.z_score)


# --- reproducibility ------------------------------------------------------


def test_the_same_inputs_always_give_the_same_rating():
    """The whole point: an LLM-written rating could not pass this test."""
    z = {"foreign_flow_5d": 1.23, "news_polarity": -0.45}
    results = [rate("005930", z, CONFIG) for _ in range(10)]
    assert len({(r.rating, round(r.score, 12)) for r in results}) == 1


# --- config errors --------------------------------------------------------


def test_out_of_order_cut_points_are_rejected():
    config = {**CONFIG, "cut_points": {"strong": 1.0, "moderate": 2.0, "weak": 0.4}}
    with pytest.raises(RatingConfigError, match="strong > moderate > weak"):
        rate("005930", {"foreign_flow_5d": 1.0, "news_polarity": 1.0}, config)


def test_missing_weights_are_rejected():
    with pytest.raises(RatingConfigError, match="weights"):
        rate("005930", {"foreign_flow_5d": 1.0}, {"cut_points": CONFIG["cut_points"]})


def test_missing_cut_points_are_rejected():
    with pytest.raises(RatingConfigError, match="cut_points"):
        rate("005930", {"foreign_flow_5d": 1.0}, {"weights": CONFIG["weights"]})


# --- the committed config -------------------------------------------------


def test_the_committed_rating_config_loads_and_drives_a_rating():
    """A uniform z=2.5 reaches 매수, not 강한 매수, because short_ratio pulls back.

    It reached 강한 매수 until 2026-08-08, when news_polarity and rev_4w moved to
    `deferred_weights`. That number was an artefact: this test fills every key in
    `weights`, so it was crediting two features nothing produces.
    """
    config = load_rating()
    result = rate("005930", dict.fromkeys(config["weights"], 2.5), config)
    assert result.rating is Rating.BUY
    assert not result.low_confidence


def test_the_outer_bucket_needs_a_z_of_two_point_seven():
    """Pins where 강한 매수 begins, because that moved on 2026-08-08 and the cut
    points did not.

    The composite is **not** in z units: with coverage at 1.0 the score is
    ``Σ|weight| × z``, so its scale tracks the total weight rather than z. Taking
    0.35 of never-arriving weight out of `weights` therefore compressed the whole
    scale by 0.75/1.10, and a uniform reading with every sign aligned — the shape
    of a genuinely one-directional tape, short interest *falling* — now needs
    z ≈ 2.67 to reach ±2.0 where it used to need z ≈ 1.82.

    Left as measured rather than corrected: PREREGISTRATION §8.4 allows revising
    cut points for distributional reasons, and doing it here, in the same change
    that moved the scale, would make the two indistinguishable afterwards.
    """
    config = load_rating()

    def aligned(z: float) -> dict[str, float]:
        return {name: z if weight > 0 else -z for name, weight in config["weights"].items()}

    assert rate("005930", aligned(2.5), config).rating is Rating.BUY
    assert rate("005930", aligned(2.7), config).rating is Rating.STRONG_BUY
    assert rate("005930", aligned(2.0), config).score == pytest.approx(1.5)


def test_rate_ignores_deferred_weights():
    """`deferred_weights` is the header's business. If rate() ever read it, the
    phantom-weight defect would come straight back under a new key name."""
    config = {**CONFIG, "deferred_weights": {"rev_4w": 0.15, "news_polarity": 0.20}}
    with_deferred = rate("005930", {"foreign_flow_5d": 1.0, "news_polarity": 1.0}, config)
    without = rate("005930", {"foreign_flow_5d": 1.0, "news_polarity": 1.0}, CONFIG)

    assert with_deferred.weight_coverage == without.weight_coverage == 1.0
    assert with_deferred.score == without.score
    assert with_deferred.missing == ()


def test_committed_config_marks_short_ratio_as_bearish():
    assert load_rating()["weights"]["short_ratio"] < 0


def test_committed_cut_points_are_ordered():
    cut = load_rating()["cut_points"]
    assert cut["strong"] > cut["moderate"] > cut["weak"] > 0


def test_a_config_with_disordered_cut_points_is_rejected_at_load(tmp_path):
    path = tmp_path / "rating.yaml"
    path.write_text(
        "weights:\n  foreign_flow_5d: 1.0\n"
        "cut_points:\n  strong: 0.5\n  moderate: 1.0\n  weak: 0.4\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="strong > moderate > weak"):
        load_rating(path)


def test_a_config_with_impossible_coverage_is_rejected_at_load(tmp_path):
    path = tmp_path / "rating.yaml"
    path.write_text(
        "weights:\n  foreign_flow_5d: 1.0\n"
        "cut_points:\n  strong: 2.0\n  moderate: 1.0\n  weak: 0.4\n"
        "confidence:\n  min_weight_coverage: 1.5\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="min_weight_coverage"):
        load_rating(path)


def test_a_feature_cannot_be_active_and_deferred_at_once(tmp_path):
    """It would be counted as live weight and reported as absent in one run."""
    path = tmp_path / "rating.yaml"
    path.write_text(
        "weights:\n  foreign_flow_5d: 1.0\n"
        "deferred_weights:\n  foreign_flow_5d: 0.2\n"
        "cut_points:\n  strong: 2.0\n  moderate: 1.0\n  weak: 0.4\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="both active and deferred"):
        load_rating(path)


def test_a_non_numeric_deferred_weight_is_rejected(tmp_path):
    path = tmp_path / "rating.yaml"
    path.write_text(
        "weights:\n  foreign_flow_5d: 1.0\n"
        "deferred_weights:\n  rev_4w: soon\n"
        "cut_points:\n  strong: 2.0\n  moderate: 1.0\n  weak: 0.4\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="deferred weight"):
        load_rating(path)


def test_no_deferred_feature_already_has_a_producer():
    """Catches the opposite drift: the feature arrived, the config did not move.
    The header would keep calling it absent while rate() ignored its weight."""
    from src.features.compute import FEATURES

    deferred = load_rating().get("deferred_weights") or {}
    assert not set(deferred) & set(FEATURES)


def test_every_active_weight_has_a_producer():
    """The defect this key was added for, pinned against the committed config."""
    from src.features.compute import FEATURES

    assert set(load_rating()["weights"]) <= set(FEATURES)


# --- bucket_from_score: the public re-export -------------------------------


@pytest.mark.parametrize("score", [3.0, 2.0, 1.999, 1.0, 0.4, 0.399, 0.0, -0.4, -1.0, -2.0, -3.5])
def test_bucket_from_score_agrees_with_rate_across_the_grid(score):
    """The load-bearing guard against the two ever drifting apart:
    `src.eval.rating_calibration` re-buckets archived scores through this
    function rather than a second copy of the cut-point ladder."""
    # CONFIG's two weights sum to 1.0 and only one is supplied, so `rate()`'s
    # renormalized score collapses to exactly `score` — see `rate()`'s
    # `score = raw / coverage`.
    via_rate = rate("005930", {"foreign_flow_5d": score}, CONFIG).rating
    via_export = bucket_from_score(score, CONFIG["cut_points"])
    assert via_export == via_rate
