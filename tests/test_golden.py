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
    find_flags,
    latest_triage,
    parse_score,
    review_influence,
    score_conflicts,
    scored_rows,
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


# --- flags -----------------------------------------------------------------


def _article(article_id, ticker, bucket, title, *, name="삼성전자", desc="", outlet="연합뉴스"):
    return {
        "article_id": article_id,
        "ticker": ticker,
        "name": name,
        "bucket": bucket,
        "title": title,
        "description": desc,
        "outlet": outlet,
    }


def test_the_same_story_labelled_two_ways_is_flagged_on_both_sides():
    """Whatever the right answer is, both cannot be it. Flagging one side only
    would make the answer depend on which outlet happened to be read first."""
    rows = [
        _article("a1", "066570", "ambiguous", "LG전자, IFA 2026서 AI 홈 비전 공개", name="LG전자"),
        _article("a2", "066570", "irrelevant", "LG전자, IFA 2026서 AI 홈 비전 공개", name="LG전자"),
    ]
    flagged = [f for f in find_flags(rows) if f["flag"] == "contradiction"]
    assert {f["article_id"] for f in flagged} == {"a1", "a2"}


def test_the_same_story_labelled_the_same_way_is_not_flagged():
    rows = [
        _article("a1", "066570", "ambiguous", "LG전자, IFA 2026 공개", name="LG전자"),
        _article("a2", "066570", "ambiguous", "LG전자, IFA 2026 공개", name="LG전자"),
    ]
    assert [f for f in find_flags(rows) if f["flag"] == "contradiction"] == []


def test_one_article_giving_two_tickers_the_same_sign_is_flagged():
    """The comparative case: 현대차 losing ground to 기아 is not bad news for
    both, and the rule cannot read which way round it is — so it asks."""
    rows = [
        _article(
            "a1", "005380", "negative", "파업에 발목 잡힌 현대차…기아에 추월 허용", name="현대차"
        ),
        _article(
            "a1", "000270", "negative", "파업에 발목 잡힌 현대차…기아에 추월 허용", name="기아"
        ),
    ]
    flagged = [f for f in find_flags(rows) if f["flag"] == "shared_sign"]
    assert len(flagged) == 2


def test_opposite_signs_on_one_article_are_left_alone():
    """Already the careful answer; flagging it would train the reviewer to
    dismiss the flag."""
    rows = [
        _article("a1", "005930", "negative", "초고수 삼성전자 팔고 원전주 샀다"),
        _article(
            "a1", "034020", "positive", "초고수 삼성전자 팔고 원전주 샀다", name="두산에너빌리티"
        ),
    ]
    assert [f for f in find_flags(rows) if f["flag"] == "shared_sign"] == []


def test_a_brokerage_named_as_the_author_of_a_view_is_flagged():
    rows = [
        _article(
            "a1",
            "039490",
            "negative",
            "키움증권, 디어유 목표가↓",
            name="키움증권",
            desc="키움증권은 5일 디어유에 대해 목표주가를 낮췄다",
        )
    ]
    assert [f["flag"] for f in find_flags(rows)] == ["author_mention"]


def test_a_brokerages_own_results_are_not_flagged():
    """The counter-case that keeps the rule usable — 삼성증권's own earnings are
    genuinely about 삼성증권, and flagging them would bury the real ones."""
    rows = [
        _article(
            "a1",
            "016360",
            "positive",
            "삼성증권, 2분기 영업이익 6758억원…역대 최대",
            name="삼성증권",
            desc="삼성증권이 자산관리와 기업금융 부문 성장에 힘입어 최대 실적을 경신했다",
        )
    ]
    assert find_flags(rows) == []


def test_an_author_mention_already_in_the_irrelevant_bucket_is_silent():
    """Ricky's standing rule sends author mentions to 무관, so a row that
    already agrees has nothing to decide."""
    rows = [
        _article(
            "a1",
            "039490",
            "irrelevant",
            "팔로알토, AI 보안 수요 확대-키움",
            name="키움증권",
            desc="키움증권은 팔로알토에 대해 평가했다",
        )
    ]
    assert find_flags(rows) == []


# --- append-only revisions -------------------------------------------------


def test_a_relabelled_example_keeps_its_place_and_its_newest_bucket():
    """`review` appends rather than rewriting, so the reader resolves it. Order
    must survive: selection walks triage order, and a corrected label jumping to
    the end of its bucket would silently change which examples make the 100."""
    rows = [
        _article("a1", "005930", "positive", "first"),
        _article("a2", "005930", "negative", "second"),
        _article("a1", "005930", "irrelevant", "first"),
    ]
    latest = latest_triage(rows)
    assert [r["article_id"] for r in latest] == ["a1", "a2"]
    assert latest[0]["bucket"] == "irrelevant"


def test_review_influence_is_silent_below_the_declared_limit():
    labelled = [_article(f"a{i}", "005930", "positive", "t") for i in range(20)]
    reviewed = [{"article_id": "a0", "ticker": "005930", "changed": True}]
    assert review_influence(labelled, reviewed) == []


def test_review_influence_reports_when_the_rules_touched_too_much():
    labelled = [_article(f"a{i}", "005930", "positive", "t") for i in range(10)]
    reviewed = [{"article_id": f"a{i}", "ticker": "005930", "changed": True} for i in range(5)]
    (problem,) = review_influence(labelled, reviewed)
    assert "50%" in problem


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


# --- rules over the scores -------------------------------------------------


def _scored(bucket, title, *, name="삼성전자", **values):
    return {
        "article_id": "a1",
        "ticker": "005930",
        "name": name,
        "bucket": bucket,
        "title": title,
        "description": "",
        **values,
    }


def test_a_positive_bucket_scored_neutral_is_flagged():
    """Two of Ricky's own decisions disagreeing. The check names the conflict
    and stops there — which one gives way is not something a rule can know."""
    (conflict,) = score_conflicts([_scored("positive", "삼성전자 2분기 최대 실적", polarity=0.0)])
    assert conflict["dimension"] == "polarity"


def test_a_negative_bucket_scored_negative_is_left_alone():
    assert score_conflicts([_scored("negative", "삼성전자 리콜", polarity=-0.8)]) == []


def test_an_irrelevant_row_may_shade_without_being_flagged():
    """The bucket means the article is not about this company's results, not
    that it must read as exactly neutral. Only a real sign is a conflict."""
    assert score_conflicts([_scored("irrelevant", "업종 동향 정리", polarity=0.2)]) == []
    assert len(score_conflicts([_scored("irrelevant", "업종 동향 정리", polarity=0.6)])) == 1


def test_a_flow_column_scored_as_if_it_touched_earnings_is_flagged():
    """`relevance` is on 손익 as of 2026-08-07, and a 매매동향 column is the one
    place the old summary line and the anchors disagreed."""
    (conflict,) = score_conflicts(
        [_scored("positive", "주식 초고수들, 삼성전자 보통주 팔고 우선주 샀다", relevance=0.9)]
    )
    assert conflict["dimension"] == "relevance"


def test_an_earnings_article_at_high_relevance_is_not_flagged():
    """The keyword list has to be narrow enough to spare what it is not about."""
    assert score_conflicts([_scored("positive", "삼성전자 2분기 영업익 12조", relevance=0.9)]) == []


def test_a_flow_column_already_scored_low_is_not_flagged():
    assert score_conflicts([_scored("positive", "초고수는 삼성전자를 담았다", relevance=0.2)]) == []


def test_a_brokerage_authoring_a_view_must_score_zero_on_its_own_row():
    """Ricky's standing rule (MANUAL-TASKS §4). The ticker on this row is the
    brokerage, so a sign here is the SK하이닉스 reading leaking onto it.

    Bucketed 애매 on purpose: the sign-vs-bucket rules say nothing about that
    bucket, so this is the case only the author rule can reach."""
    (conflict,) = score_conflicts(
        [
            _scored(
                "ambiguous",
                "한투증권, SK하이닉스 목표주가 상향 제시했다",
                name="한국금융지주",
                polarity=0.5,
            )
        ]
    )
    assert "증권사" in conflict["reason"]


def test_a_brokerages_own_results_are_not_caught_by_the_author_rule():
    assert (
        score_conflicts([_scored("ambiguous", "한국투자증권 2분기 순이익 1조", polarity=0.5)]) == []
    )


def test_saying_a_story_misses_the_books_then_scoring_it_large_is_flagged():
    """`relevance` and `intensity` ask the same question at two magnitudes —
    does a line of this company's P&L move. Hit live on 2026-08-08: two
    브로커리지 목표가 rows both at relevance 0.0, one scored intensity 0.8 and
    the other 0.0."""
    (conflict,) = score_conflicts(
        [
            _scored(
                "irrelevant",
                '키움증권 "SK바이오팜 우려 과도"…목표가↓',
                relevance=0.0,
                intensity=0.8,
            )
        ]
    )
    assert conflict["dimension"] == "intensity"


def test_a_low_intensity_story_that_touches_the_books_is_left_alone():
    """The reverse is not a conflict: a 신제품 출시 reaches the business and
    still moves no money this quarter."""
    assert (
        score_conflicts([_scored("positive", "삼성전자 신제품 공개", relevance=0.9, intensity=0.1)])
        == []
    )


def test_a_price_move_caused_by_flow_is_flagged():
    (conflict,) = score_conflicts(
        [_scored("negative", "하이닉스 10%대 폭락…외인·기관 팔았다", relevance=1.0)]
    )
    assert conflict["dimension"] == "relevance"


def test_a_business_event_reported_through_its_price_move_is_not_flagged():
    """The rule needs a market-side cause as well as a price word. Matching
    급등 alone would flag a supply contract wearing a price headline, and a
    check that fires on the wrong rows buries the right ones."""
    assert (
        score_conflicts(
            [_scored("positive", "테슬라 카메라 모듈 공급 싹쓸이에…삼성전기 급등", relevance=0.8)]
        )
        == []
    )


def test_one_event_scored_two_ways_is_flagged_on_both_sides():
    """SK실트론's 신용등급 하향검토, written up twice on 2026-08-08 and scored
    uncertainty 0.0 and 0.7. The pair sits at 0.38 title overlap — under
    `SAME_EVENT_SIMILARITY`, so selection rightly kept both, and the score
    check has to look wider than selection does."""
    rows = [
        {
            **_scored("irrelevant", "나신평, '두산 매각' SK실트론 신용등급 하향검토 등재"),
            "uncertainty": 0.0,
        },
        {
            **_scored("ambiguous", "신평 3사, '매각' SK실트론 신용등급 전망 하향 검토"),
            "uncertainty": 0.7,
        },
    ]
    rows[1]["article_id"] = "a2"
    flagged = [c for c in score_conflicts(rows) if c["dimension"] == "uncertainty"]
    assert {c["article_id"] for c in flagged} == {"a1", "a2"}


def test_two_write_ups_scored_alike_are_left_alone():
    rows = [
        {**_scored("ambiguous", "SK실트론 신용등급 하향검토 등재"), "uncertainty": 0.7},
        {**_scored("ambiguous", "SK실트론 신용등급 전망 하향 검토"), "uncertainty": 0.6},
    ]
    rows[1]["article_id"] = "a2"
    assert score_conflicts(rows) == []


def test_two_different_stories_about_one_ticker_are_not_compared():
    """Without the similarity floor every 삼성전자 pair in the set would flag."""
    rows = [
        {**_scored("positive", "삼성전자 2분기 영업익 12조"), "uncertainty": 0.0},
        {**_scored("ambiguous", "삼성전자, 사내 어린이집 확대 계획 발표"), "uncertainty": 0.7},
    ]
    rows[1]["article_id"] = "a2"
    assert score_conflicts(rows) == []


def test_an_unscored_dimension_raises_nothing():
    """`score_conflicts` runs mid-labelling, where most dimensions are absent."""
    assert score_conflicts([_scored("positive", "삼성전자 2분기 최대 실적")]) == []


def test_partial_progress_is_merged_back_onto_its_article(tmp_path):
    """`collate_scores` drops incomplete rows, which is right for the finished
    set and wrong here: a conflict is worth seeing before all five arrive."""
    progress = tmp_path / "scores.jsonl"
    progress.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in _score_rows("a1", relevance=0.9, polarity=0.2)
        )
        + "\n",
        encoding="utf-8",
    )
    picked = [_article("a1", "005930", "positive", "삼성전자 2분기 최대 실적")]
    (row,) = scored_rows(progress, picked)
    assert (row["relevance"], row["polarity"]) == (0.9, 0.2)
    assert row["title"] == "삼성전자 2분기 최대 실적"


def test_a_rescored_dimension_wins_when_progress_is_merged(tmp_path):
    progress = tmp_path / "scores.jsonl"
    rows = _score_rows("a1", relevance=0.9) + _score_rows("a1", relevance=0.2)
    progress.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    picked = [_article("a1", "005930", "positive", "초고수는 삼성전자를 담았다")]
    (row,) = scored_rows(progress, picked)
    assert row["relevance"] == 0.2
    assert score_conflicts([row]) == []


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
