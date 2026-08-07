"""Tests for the golden-set tooling. SPEC §7.3, §12 step 7.

Offline, against synthetic pairs. The interactive prompts are not tested — they
are I/O — but everything that decides *which* articles Ricky sees, and whether
the finished file is usable, is.

The test that matters most is the stratification one. A golden set that is 29%
삼성전자 measures how well models read Samsung coverage, and the bake-off would
report that as general performance.
"""

from __future__ import annotations

import json

import pytest

from scripts.golden import (
    BUCKETS,
    DIMENSIONS,
    PER_BUCKET,
    InputError,
    collate_scores,
    parse_score,
    select_for_labelling,
    stratified_sample,
)


def pairs(**counts: int) -> list[tuple[str, str]]:
    """`{ticker: n}` → the (article_id, ticker) pairs a corpus would yield."""
    out = []
    for ticker, n in counts.items():
        out += [(f"{ticker}-{i:03d}", ticker) for i in range(n)]
    return out


def articles_for(pairs_: list[tuple[str, str]]) -> dict[str, dict]:
    return {
        article_id: {"published_at": f"2026-08-{(i % 28) + 1:02d}T00:00:00+00:00"}
        for i, (article_id, _) in enumerate(pairs_)
    }


# --- sampling --------------------------------------------------------------


def test_a_dominant_ticker_does_not_dominate_the_sample():
    """Measured on the real corpus 2026-08-06: 삼성전자 held 138 of 479 pairs.
    Uniform sampling would hand that skew straight to the bake-off."""
    source = pairs(A=138, B=119, C=42, D=31, E=18, F=17, G=17)
    chosen = stratified_sample(source, articles_for(source), size=100)

    spread: dict[str, int] = {}
    for _, ticker in chosen:
        spread[ticker] = spread.get(ticker, 0) + 1

    assert len(chosen) == 100
    assert max(spread.values()) <= 100 // len(spread) + 1, spread
    assert set(spread) == {"A", "B", "C", "D", "E", "F", "G"}


def test_sampling_is_reproducible():
    source = pairs(A=40, B=40)
    articles = articles_for(source)
    assert stratified_sample(source, articles, size=30) == stratified_sample(
        source, articles, size=30
    )


def test_a_different_seed_gives_a_different_sample():
    source = pairs(A=40, B=40)
    articles = articles_for(source)
    first = stratified_sample(source, articles, size=30, seed=1)
    second = stratified_sample(source, articles, size=30, seed=2)
    assert first != second


def test_sampling_stops_when_the_corpus_runs_out():
    source = pairs(A=5, B=3)
    chosen = stratified_sample(source, articles_for(source), size=100)
    assert len(chosen) == 8
    assert len(set(chosen)) == 8, "no pair may be handed out twice"


def test_a_thin_ticker_is_not_dropped():
    """Round-robin must reach the tail, or thinly-covered tickers never appear
    and the set silently becomes a large-cap set."""
    source = pairs(A=200, B=1)
    chosen = stratified_sample(source, articles_for(source), size=20)
    assert ("B-000", "B") in chosen


# --- selection after triage ------------------------------------------------


def triaged(**counts: int) -> list[dict]:
    rows = []
    for bucket, n in counts.items():
        rows += [
            {"article_id": f"{bucket}-{i}", "ticker": "005930", "bucket": bucket} for i in range(n)
        ]
    return rows


def test_selection_caps_each_bucket_at_twenty_five():
    picked, counts = select_for_labelling(
        triaged(positive=40, negative=30, ambiguous=25, irrelevant=60)
    )
    assert len(picked) == 4 * PER_BUCKET
    assert set(counts.values()) == {PER_BUCKET}


def test_selection_reports_an_underfilled_bucket_rather_than_padding():
    """SPEC §7.3 wants 25 of each. A short bucket must be visible, not quietly
    topped up from an easier one."""
    _, counts = select_for_labelling(triaged(positive=25, negative=3, ambiguous=25, irrelevant=25))
    assert counts["negative"] == 3


def test_selection_follows_triage_order():
    rows = triaged(positive=30)
    picked, _ = select_for_labelling(rows)
    assert [row["article_id"] for row in picked] == [row["article_id"] for row in rows[:25]]


def test_a_full_bucket_still_records_the_honest_label(tmp_path, monkeypatch, capsys):
    """Hit live on 2026-08-07, 61 articles into the first real triage pass.

    The 긍정 bucket filled at 25 and the next article was plainly positive.
    Triage rejected the keystroke and printed "다른 분류를 고르세요" — asking for
    a label chosen by which counter had room rather than by what the article
    said. A golden set assembled that way measures the quota, not the judgement,
    and it is the same contamination the worked examples were rewritten to
    avoid.

    The cap belongs at selection time, which `select_for_labelling` already
    applies, so triage must record what Ricky actually answered.
    """
    import scripts.golden as golden

    monkeypatch.setattr(golden, "CANDIDATES", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(golden, "TRIAGE", tmp_path / "triage.jsonl")

    pool = [
        {"article_id": f"a{i}", "ticker": "005930", "title": f"t{i}"} for i in range(PER_BUCKET + 1)
    ]
    for row in pool:
        golden.append_jsonl(golden.CANDIDATES, row)
    # Fill 긍정 to exactly the cap, leaving one candidate untriaged.
    for row in pool[:PER_BUCKET]:
        golden.append_jsonl(golden.TRIAGE, {**row, "bucket": "positive"})

    monkeypatch.setattr(golden, "_prompt", lambda _: "1")  # 명백한 긍정
    golden.run_triage()

    recorded = golden.read_jsonl(golden.TRIAGE)
    assert len(recorded) == PER_BUCKET + 1, "the 26th positive must be recorded, not refused"
    assert recorded[-1]["bucket"] == "positive"
    assert recorded[-1]["article_id"] == pool[-1]["article_id"]
    assert "다른 분류를 고르세요" not in capsys.readouterr().out

    # ...and the overflow must not reach scoring.
    picked, counts = select_for_labelling(recorded)
    assert counts["positive"] == PER_BUCKET
    assert len(picked) == PER_BUCKET


# --- score parsing ---------------------------------------------------------


def test_one_number_is_parsed_for_its_own_dimension():
    assert parse_score("0.9", "relevance", 0.0, 1.0) == 0.9
    assert parse_score("-0.4", "polarity", -1.0, 1.0) == -0.4


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("0.9 0.4", "1개"),
        ("", "1개"),
        ("x", "숫자가 아닙니다"),
        ("1.5", "범위"),
        ("-0.2", "범위"),
    ],
)
def test_malformed_input_is_reported_not_stored(text, message):
    """A bad line must return to the prompt. Writing it would put an
    out-of-range score into the file every model is measured against."""
    with pytest.raises(InputError, match=message):
        parse_score(text, "relevance", 0.0, 1.0)


# --- collation -------------------------------------------------------------


def _score_rows(article: str, **values) -> list[dict]:
    return [
        {
            "article_id": article,
            "ticker": "005930",
            "bucket": "positive",
            "dimension": name,
            "value": v,
            "labeled_at": "2026-08-07T10:00:00+00:00",
        }
        for name, v in values.items()
    ]


def test_collation_pivots_per_dimension_records_into_one_row():
    rows = _score_rows(
        "a1", relevance=0.9, polarity=-0.4, intensity=0.6, uncertainty=0.3, forwardness=0.8
    )
    (row,) = collate_scores(rows)
    assert row["article_id"] == "a1"
    assert (row["relevance"], row["polarity"], row["forwardness"]) == (0.9, -0.4, 0.8)


def test_a_partially_scored_example_is_omitted():
    """Scoring runs one dimension at a time, so a quit mid-pass leaves most
    examples holding one or two of five. Writing those into the golden set
    would hand the bake-off rows to be measured on dimensions nobody gave."""
    assert collate_scores(_score_rows("a1", relevance=0.9, polarity=0.2)) == []


def test_collation_survives_a_rescored_dimension():
    """The progress file is append-only, so re-answering a dimension appends a
    second record rather than replacing the first. The later one must win."""
    rows = _score_rows(
        "a1", relevance=0.1, polarity=0.0, intensity=0.0, uncertainty=0.0, forwardness=0.0
    )
    rows += _score_rows("a1", relevance=0.9)
    (row,) = collate_scores(rows)
    assert row["relevance"] == 0.9


def test_polarity_alone_accepts_negative_values():
    """The four other dimensions are magnitudes; polarity is the only signed
    one, and rejecting its sign would make every negative article unlabelable."""
    bounds = {name: (low, high) for name, low, high, _, _ in DIMENSIONS}
    assert bounds["polarity"] == (-1.0, 1.0)
    assert all(low == 0.0 for name, (low, _) in bounds.items() if name != "polarity")


# --- verification ----------------------------------------------------------


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )


def label_rows(bucket: str, n: int, **overrides) -> list[dict]:
    rows = []
    for i in range(n):
        row = {
            "article_id": f"{bucket}-{i}",
            "ticker": "005930",
            "bucket": bucket,
            "relevance": 0.9,
            "polarity": 0.7,
            "intensity": 0.5,
            "uncertainty": 0.3,
            "forwardness": 0.6,
            "labeled_at": "2026-08-07T00:00:00+00:00",
        }
        row.update(overrides)
        rows.append(row)
    return rows


def full_set() -> list[dict]:
    rows = []
    for name, _, _ in BUCKETS.values():
        polarity = {"positive": 0.8, "negative": -0.8, "ambiguous": 0.1, "irrelevant": 0.0}[name]
        rows += label_rows(name, PER_BUCKET, polarity=polarity)
    return rows


def test_a_complete_set_verifies(tmp_path, monkeypatch):
    import scripts.golden as mod

    monkeypatch.setattr(mod, "LABELS", tmp_path / "v1.jsonl")
    monkeypatch.setattr(mod, "RECHECK", tmp_path / "recheck.jsonl")
    _write(mod.LABELS, full_set())

    ok, problems = mod.verify()
    assert ok, problems


def test_an_underfilled_bucket_fails_verification(tmp_path, monkeypatch):
    import scripts.golden as mod

    monkeypatch.setattr(mod, "LABELS", tmp_path / "v1.jsonl")
    monkeypatch.setattr(mod, "RECHECK", tmp_path / "recheck.jsonl")
    _write(mod.LABELS, [row for row in full_set() if row["bucket"] != "negative"])

    ok, problems = mod.verify()
    assert not ok
    assert any("부정" in problem for problem in problems)


def test_a_set_with_no_strong_opinions_is_flagged(tmp_path, monkeypatch):
    """All-mild labels cannot separate models — every model looks equally good,
    which is the failure MANUAL-TASKS §4 warns about when picking easy cases."""
    import scripts.golden as mod

    monkeypatch.setattr(mod, "LABELS", tmp_path / "v1.jsonl")
    monkeypatch.setattr(mod, "RECHECK", tmp_path / "recheck.jsonl")
    rows = [dict(row, polarity=0.1) for row in full_set()]
    _write(mod.LABELS, rows)

    ok, problems = mod.verify()
    assert not ok
    assert any("polarity" in problem for problem in problems)


def test_disagreement_between_the_two_passes_is_reported(tmp_path, monkeypatch, capsys):
    """SPEC §7.3: if Ricky disagrees with himself more than the models disagree
    with each other, the schema is the thing to fix first."""
    import scripts.golden as mod

    monkeypatch.setattr(mod, "LABELS", tmp_path / "v1.jsonl")
    monkeypatch.setattr(mod, "RECHECK", tmp_path / "recheck.jsonl")
    _write(mod.LABELS, full_set())
    _write(mod.RECHECK, [dict(row, polarity=row["polarity"] - 0.6) for row in full_set()[:10]])

    ok, problems = mod.verify()
    assert "재라벨링 일치도" in capsys.readouterr().out
    assert not ok
    assert any("두 회차" in problem for problem in problems)


def test_an_out_of_range_score_fails_verification(tmp_path, monkeypatch):
    import scripts.golden as mod

    monkeypatch.setattr(mod, "LABELS", tmp_path / "v1.jsonl")
    monkeypatch.setattr(mod, "RECHECK", tmp_path / "recheck.jsonl")
    rows = full_set()
    rows[0]["relevance"] = 1.4
    _write(mod.LABELS, rows)

    ok, problems = mod.verify()
    assert not ok
    assert any("범위 밖" in problem for problem in problems)
