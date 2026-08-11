"""Tests for the SPEC §7.4 bake-off.

No model is called. `run` takes its scorer as a parameter precisely so the
measurement code can be exercised against models whose answers are known —
which is the only way to tell that a correlation of 0.9 means the model was
good rather than that the arithmetic was wrong.
"""

from __future__ import annotations

import pytest

from scripts.golden import DIMENSIONS, LABELS, key_of, read_jsonl
from src.eval import bakeoff
from src.eval.bakeoff import Attempt, spearman
from src.llm.adapter import Completion, SchemaError

NAMES = [name for name, *_ in DIMENSIONS]


@pytest.fixture(scope="module")
def labels():
    return {key_of(row): row for row in read_jsonl(LABELS)}


def completion(values: dict[str, float], *, model: str, cost: float | None = 0.001) -> Completion:
    return Completion(
        parsed={**values, "rationale": "테스트"},
        model_id=model,
        prompt_version="v1",
        input_tokens=500,
        output_tokens=40,
        cost_usd=cost,
        latency_s=0.4,
    )


def mirror(labels):
    """A model that reproduces Ricky's labels exactly."""

    def scorer(article, *, prompt, model, provider):
        truth = labels[(article["article_id"], article["ticker"])]
        return completion({n: float(truth[n]) for n in NAMES}, model=model)

    return scorer


def constant(article, *, prompt, model, provider):
    """A model that answers the same thing every time."""
    return completion({n: 0.5 if n != "polarity" else 0.0 for n in NAMES}, model=model)


CANDIDATE = [{"provider": "anthropic", "model": "m"}]


# --- the join to article text ----------------------------------------------


def test_every_label_gets_its_article_text():
    """`v1.jsonl` holds only scores. Without the join there is nothing to send
    a model, and the failure would look like a bad correlation."""
    rows = bakeoff.examples()
    assert len(rows) == 100
    assert all(row["title"] and row["label"] for row in rows)
    assert {row["label"]["bucket"] for row in rows} == {
        "positive",
        "negative",
        "ambiguous",
        "irrelevant",
    }


# --- the correlation itself ------------------------------------------------


def test_spearman_is_rank_correlation_not_value_correlation():
    """Monotone but non-linear: Spearman is 1.0 where Pearson would not be."""
    assert spearman([1, 2, 3, 4], [1, 4, 9, 16]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_handles_ties_with_average_ranks():
    assert spearman([1, 1, 2, 2], [1, 1, 2, 2]) == pytest.approx(1.0)


def test_spearman_is_undefined_rather_than_zero_for_a_constant():
    """A model that says 0.5 to everything is not "uncorrelated" — it is not
    discriminating, which is a different and worse finding."""
    assert spearman([0.5, 0.5, 0.5], [0.1, 0.5, 0.9]) is None
    assert spearman([0.1], [0.2]) is None


def test_a_model_that_reproduces_the_labels_correlates_at_one(labels):
    """The regression that would catch a broken join, a mislabelled column, or
    a correlation computed against the wrong rows."""
    attempts = bakeoff.run(CANDIDATE, repeats=1, limit=40, scorer=mirror(labels))
    correlation = bakeoff.golden_correlation(attempts, "m", labels)
    for name in NAMES:
        assert correlation[name] == pytest.approx(1.0), name


def test_a_constant_model_reports_no_correlation(labels):
    attempts = bakeoff.run(CANDIDATE, repeats=1, limit=40, scorer=constant)
    correlation = bakeoff.golden_correlation(attempts, "m", labels)
    assert all(correlation[name] is None for name in NAMES)


# --- self-consistency ------------------------------------------------------


def test_a_deterministic_model_has_zero_spread(labels):
    attempts = bakeoff.run(CANDIDATE, repeats=3, limit=20, scorer=mirror(labels))
    spread = bakeoff.self_consistency(attempts, "m")
    assert all(spread[name] == pytest.approx(0.0) for name in NAMES)


def test_a_wobbling_model_is_measured_across_repeats():
    """SPEC §7.4 measures σ over 5 runs of the same article. One run cannot
    show it, so the metric must come from the repeats and not from the spread
    across different articles."""
    values = iter([0.0, 0.4, 0.8])

    def wobble(article, *, prompt, model, provider):
        return completion({n: next(values) if n == "polarity" else 0.5 for n in NAMES}, model=model)

    attempts = bakeoff.run(CANDIDATE, repeats=3, limit=1, scorer=wobble)
    spread = bakeoff.self_consistency(attempts, "m")
    assert spread["polarity"] == pytest.approx(0.4)
    assert spread["relevance"] == pytest.approx(0.0)


# --- failures are measured, not raised -------------------------------------


def test_a_schema_failure_is_recorded_and_counted(labels):
    """§7.4's compliance rate is the share of calls that parse. A model failing
    3% of articles has to arrive as 97%, not as a crash."""
    calls = {"n": 0}

    def flaky(article, *, prompt, model, provider):
        calls["n"] += 1
        if calls["n"] % 4 == 0:
            raise SchemaError("returned prose")
        truth = labels[(article["article_id"], article["ticker"])]
        return completion({n: float(truth[n]) for n in NAMES}, model=model)

    attempts = bakeoff.run(CANDIDATE, repeats=1, limit=20, scorer=flaky)
    assert bakeoff.schema_compliance(attempts, "m") == pytest.approx(0.75)
    assert any("schema" in a.failure for a in attempts)


def test_an_out_of_range_answer_fails_the_call(labels):
    """The strict schema forbids it, so reaching here means the vendor did not
    enforce what it was given."""

    def over(article, *, prompt, model, provider):
        return completion({**{n: 0.5 for n in NAMES}, "polarity": 3.0}, model=model)

    attempts = bakeoff.run(CANDIDATE, repeats=1, limit=5, scorer=over)
    assert bakeoff.schema_compliance(attempts, "m") == 0.0
    assert all("out of range: polarity" in a.failure for a in attempts)


def test_failed_calls_are_excluded_from_the_correlation(labels):
    """A failure is not a score of zero — including it would drag the
    correlation toward a number the model never produced."""

    def half(article, *, prompt, model, provider):
        if article["label"]["bucket"] == "irrelevant":
            raise SchemaError("nope")
        truth = labels[(article["article_id"], article["ticker"])]
        return completion({n: float(truth[n]) for n in NAMES}, model=model)

    attempts = bakeoff.run(CANDIDATE, repeats=1, limit=100, scorer=half)
    assert bakeoff.golden_correlation(attempts, "m", labels)["polarity"] == pytest.approx(1.0)


# --- cost ------------------------------------------------------------------


def test_cost_per_valid_signal_divides_by_the_models_own_relevance_calls():
    """SPEC §7.4: articles the model called relevant, not articles it was
    handed. A model that calls everything irrelevant is not cheap."""

    def relevant_half(article, *, prompt, model, provider):
        value = 0.9 if article["label"]["bucket"] in {"positive", "negative"} else 0.1
        return completion({**{n: 0.5 for n in NAMES}, "relevance": value}, model=model, cost=0.01)

    attempts = bakeoff.run(CANDIDATE, repeats=1, limit=100, scorer=relevant_half)
    valid = len({(a.article_id, a.ticker) for a in attempts if a.scores["relevance"] > 0.5})
    assert valid == 50
    assert bakeoff.cost_per_valid_signal(attempts, "m") == pytest.approx(100 * 0.01 / 50)


def test_an_unpriced_model_reports_no_cost_rather_than_a_wrong_one(labels):
    def unpriced(article, *, prompt, model, provider):
        truth = labels[(article["article_id"], article["ticker"])]
        return completion({n: float(truth[n]) for n in NAMES}, model=model, cost=None)

    attempts = bakeoff.run(CANDIDATE, repeats=1, limit=10, scorer=unpriced)
    assert bakeoff.cost_per_valid_signal(attempts, "m") is None


# --- the §7.4 gate ---------------------------------------------------------


def test_the_gate_is_correlation_and_consistency_before_cost():
    good = {"relevance": 0.8, "polarity": 0.7}
    steady = {"polarity": 0.05}
    assert bakeoff.passes(good, steady, 1.0) == (True, [])

    ok, reasons = bakeoff.passes({"relevance": 0.5, "polarity": 0.7}, steady, 1.0)
    assert not ok and "relevance correlation" in reasons[0]

    ok, reasons = bakeoff.passes(good, {"polarity": 0.3}, 1.0)
    assert not ok and "polarity σ" in reasons[0]

    ok, reasons = bakeoff.passes(good, steady, 0.95)
    assert not ok and "schema compliance" in reasons[0]


def test_a_model_with_no_measurable_correlation_does_not_pass():
    """`None` means undefined, and undefined must not slip through a `>` test."""
    ok, reasons = bakeoff.passes({"relevance": None, "polarity": None}, {"polarity": None}, 1.0)
    assert not ok
    assert len(reasons) == 3


# --- storage and the report ------------------------------------------------


def test_attempts_survive_a_round_trip(tmp_path, labels):
    attempts = bakeoff.run(CANDIDATE, repeats=1, limit=5, scorer=mirror(labels))
    path = bakeoff.store(attempts, tmp_path / "attempts.jsonl")
    restored = bakeoff.load(path)

    assert len(restored) == len(attempts)
    assert restored[0].scores == attempts[0].scores
    assert restored[0].model == attempts[0].model


def test_the_report_states_the_noise_floor_and_the_disclosure(labels):
    """Both are obligations, not decoration: PREREGISTRATION §8.3 forbids
    reading a forwardness difference below 0.13 as evidence, and §R requires
    saying that Claude helped word two of the definitions."""
    attempts = bakeoff.run(CANDIDATE, repeats=2, limit=20, scorer=mirror(labels))
    text = bakeoff.report(attempts)

    assert "±0.13" in text
    assert "noise floor" in text
    assert "Claude helped word" in text
    assert "does not choose" in text
    assert "unflagged subset" in text


def test_the_report_renders_without_a_single_valid_score():
    """A run where every call failed still has to produce a readable table —
    that is the run whose output matters most."""
    attempts = [
        Attempt(
            article_id="a",
            ticker="005930",
            model="m",
            provider="anthropic",
            repeat=0,
            prompt_version="v1",
            failure="schema: prose",
        )
    ]
    text = bakeoff.report(attempts)
    assert "0.0%" in text
