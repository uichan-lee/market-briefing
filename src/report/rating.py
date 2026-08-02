"""The directional rating. SPEC §2.2⑥.

The briefing states an opinion — 강한 매수 through 강한 매도 — and this module
produces it. Two things it is not:

**It is not an LLM output.** The rating is a weighted sum of z-scores bucketed by
cut points. An LLM only ever assigned per-article numbers upstream (SPEC §6.2);
it never sees or writes the rating. That is what makes the rating reproducible,
and reproducibility is the precondition for evaluating it at all — a rating
written as prose could not be correlated with forward returns or compared across
model versions (PREREGISTRATION §8.4).

**It is not an instruction to trade.** This system places no orders. The rating
is read by a human who decides, and per PREREGISTRATION §8.5 no real money moves
before the 3-month gate.

The rationale is not commentary either. It is the decomposition of the sum:
each term's ``weight × z`` contribution, largest first. It therefore states
literally what moved the number, and cannot drift from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class Rating(Enum):
    """The seven-point scale. ``value`` is the Korean label used in the report."""

    STRONG_BUY = "강한 매수"
    BUY = "매수"
    WEAK_BUY = "약한 매수"
    HOLD = "관망"
    WEAK_SELL = "약한 매도"
    SELL = "매도"
    STRONG_SELL = "강한 매도"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Contribution:
    """One feature's push on the composite score."""

    feature: str
    z_score: float
    weight: float

    @property
    def value(self) -> float:
        return self.weight * self.z_score

    def __str__(self) -> str:
        return f"{self.feature} z={self.z_score:+.2f} → {self.value:+.2f}"


@dataclass(frozen=True)
class RatingResult:
    """A rating and the evidence that produced it."""

    ticker: str
    rating: Rating
    score: float
    contributions: tuple[Contribution, ...]
    missing: tuple[str, ...]
    weight_coverage: float
    low_confidence: bool

    def rationale(self, limit: int = 4) -> tuple[Contribution, ...]:
        """Largest contributors first — what actually moved the score."""
        ranked = sorted(self.contributions, key=lambda c: abs(c.value), reverse=True)
        return tuple(ranked[:limit])

    def __str__(self) -> str:
        head = f"{self.ticker} — {self.rating} ({self.score:+.2f})"
        if self.low_confidence:
            head += f"  [low confidence: missing {', '.join(self.missing)}]"
        return head


class RatingConfigError(ValueError):
    """Raised for a malformed rating configuration."""


def _bucket(score: float, cut_points: Mapping[str, float]) -> Rating:
    """Map a composite score onto the seven-point scale.

    Symmetric by construction. Boundaries are inclusive on the outer edge, so a
    score of exactly the ``strong`` cut point rates 강한 매수 rather than 매수.
    """
    try:
        strong = float(cut_points["strong"])
        moderate = float(cut_points["moderate"])
        weak = float(cut_points["weak"])
    except KeyError as exc:
        raise RatingConfigError(f"cut_points is missing {exc.args[0]!r}") from None

    if not (strong > moderate > weak > 0):
        raise RatingConfigError(
            f"cut_points must satisfy strong > moderate > weak > 0; "
            f"got strong={strong}, moderate={moderate}, weak={weak}"
        )

    magnitude = abs(score)
    if magnitude < weak:
        return Rating.HOLD

    positive = score > 0
    if magnitude >= strong:
        return Rating.STRONG_BUY if positive else Rating.STRONG_SELL
    if magnitude >= moderate:
        return Rating.BUY if positive else Rating.SELL
    return Rating.WEAK_BUY if positive else Rating.WEAK_SELL


def rate(
    ticker: str,
    z_scores: Mapping[str, float | None],
    config: Mapping[str, object],
) -> RatingResult:
    """Rate one ticker from its feature z-scores.

    ``z_scores`` maps feature name to its 252-day rolling z-score (SPEC §5), with
    ``None`` for a feature that could not be computed. Features absent from the
    mapping are treated the same as ``None``.

    Missing features are **excluded and the weights renormalized over what is
    present**, never silently treated as zero. Zero-filling would be the quiet
    kind of wrong: it drags the composite toward 관망 while the output still
    looks fully informed. Renormalizing keeps the score on the same scale as a
    complete one, and ``weight_coverage`` records how much of the intended
    evidence actually backed it.

    When coverage falls below ``confidence.min_weight_coverage``, the result is
    forced to 관망 and flagged. A confident-looking rating on thin evidence is
    worse than no rating.
    """
    weights = config.get("weights")
    if not isinstance(weights, Mapping) or not weights:
        raise RatingConfigError("rating config has no 'weights'")

    cut_points = config.get("cut_points")
    if not isinstance(cut_points, Mapping):
        raise RatingConfigError("rating config has no 'cut_points'")

    confidence = config.get("confidence") or {}
    min_coverage = float(confidence.get("min_weight_coverage", 0.0))

    contributions: list[Contribution] = []
    missing: list[str] = []
    present_weight = 0.0
    total_weight = 0.0

    for feature, weight in weights.items():
        weight = float(weight)
        total_weight += abs(weight)

        z = z_scores.get(feature)
        if z is None:
            missing.append(feature)
            continue

        present_weight += abs(weight)
        contributions.append(Contribution(feature=feature, z_score=float(z), weight=weight))

    if total_weight == 0:
        raise RatingConfigError("rating weights sum to zero magnitude")

    coverage = present_weight / total_weight

    if not contributions:
        return RatingResult(
            ticker=ticker,
            rating=Rating.HOLD,
            score=0.0,
            contributions=(),
            missing=tuple(missing),
            weight_coverage=0.0,
            low_confidence=True,
        )

    # Renormalize over the features that are present, so a partially-covered
    # ticker is scored on the same scale as a fully-covered one.
    raw = sum(c.value for c in contributions)
    score = raw / coverage

    low_confidence = coverage < min_coverage
    rating = Rating.HOLD if low_confidence else _bucket(score, cut_points)

    return RatingResult(
        ticker=ticker,
        rating=rating,
        score=score,
        contributions=tuple(contributions),
        missing=tuple(missing),
        weight_coverage=coverage,
        low_confidence=low_confidence,
    )
