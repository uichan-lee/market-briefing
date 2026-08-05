"""Tests for entity resolution. SPEC §4.

SPEC calls this the step that makes everything downstream meaningless if it
fails, so the tests target the ways it can be wrong while still looking right:
masking order, a group name treated as an identification, and a ticker code
found inside an unrelated number.
"""

from __future__ import annotations

import pytest

from src.entity.resolve import (
    AMBIGUOUS_THRESHOLD,
    ResolutionReport,
    coverage_gaps,
    dead_aliases,
    resolve,
    resolve_article,
)
from src.util.config import AliasEntry


def entry(ticker, canonical, aliases, exclude=(), parents=()):
    return AliasEntry(
        ticker=ticker,
        canonical=canonical,
        aliases=tuple(aliases),
        exclude=tuple(exclude),
        ambiguous_parents=tuple(parents),
    )


ENTRIES = {
    "005930": entry(
        "005930",
        "삼성전자",
        ["삼성전자", "삼전"],
        exclude=["삼성전자우", "삼성물산"],
        parents=["삼성"],
    ),
    "000660": entry("000660", "SK하이닉스", ["SK하이닉스", "하이닉스"], parents=["SK"]),
    "000150": entry("000150", "두산", ["두산"], exclude=["두산에너빌리티", "두산베어스"]),
}


def article(text, aid="a1"):
    return {"article_id": aid, "title": text, "description": ""}


# --- masking --------------------------------------------------------------


def test_an_excluded_superstring_does_not_match_its_base():
    matches, _ = resolve_article("삼성전자우 배당락", ENTRIES)
    assert matches == []


def test_a_standalone_mention_survives_an_excluded_form_beside_it():
    """The over-correction this guards against.

    Masking must remove only the excluded span. A plain containment test would
    throw away a genuine 삼성전자 article for mentioning the preferred share.
    """
    matches, _ = resolve_article("삼성전자와 삼성전자우가 동반 상승", ENTRIES)
    assert [m.ticker for m in matches] == ["005930"]


def test_the_longest_excluded_form_is_masked_first():
    entries = {"000150": entry("000150", "두산", ["두산"], exclude=["두산", "두산에너빌리티"])}
    # Masking "두산" first would leave "에너빌리티" and the longer form would
    # never be recognised.
    matches, _ = resolve_article("두산에너빌리티 수주", entries)
    assert matches == []


# --- ambiguity ------------------------------------------------------------


def test_a_group_name_alone_is_ambiguous_not_matched():
    """SPEC §4.2 item 3: better to drop a match than get it wrong."""
    matches, ambiguous = resolve_article("삼성 계열사 지배구조 개편", ENTRIES)
    assert matches == []
    assert ambiguous == {"005930"}


def test_an_alias_wins_over_its_own_group_name():
    matches, ambiguous = resolve_article("삼성 그룹의 삼성전자, 신규 투자", ENTRIES)
    assert [m.ticker for m in matches] == ["005930"]
    assert "005930" not in ambiguous


def test_an_unrelated_article_is_neither_matched_nor_ambiguous():
    matches, ambiguous = resolve_article("국제 유가 하락", ENTRIES)
    assert not matches and not ambiguous


# --- ticker codes ---------------------------------------------------------


def test_a_bare_ticker_code_matches():
    matches, _ = resolve_article("005930 목표가 상향", ENTRIES)
    assert [(m.ticker, m.match_type) for m in matches] == [("005930", "code")]


def test_a_code_inside_a_longer_number_does_not_match():
    """Six digits appear constantly in prices and volumes.

    Without digit boundaries, 1005930원 would silently resolve to 삼성전자.
    """
    matches, _ = resolve_article("거래대금 1005930원 기록", ENTRIES)
    assert matches == []
    matches, _ = resolve_article("전일 대비 0059301", ENTRIES)
    assert matches == []


def test_a_code_inside_an_excluded_span_still_does_not_match():
    entries = {"005930": entry("005930", "삼성전자", ["삼성전자"], exclude=["005930우"])}
    matches, _ = resolve_article("005930우 배당", entries)
    assert matches == []


def test_an_alias_is_preferred_over_the_code_for_the_same_ticker():
    matches, _ = resolve_article("삼성전자 005930 실적", ENTRIES)
    assert len(matches) == 1
    assert matches[0].match_type == "alias"


# --- corpus-level ---------------------------------------------------------


def test_one_article_can_attach_to_several_tickers():
    """An article naming both is evidence for both; collapsing it would force a
    choice the text does not support."""
    frame, report = resolve([article("삼성전자와 SK하이닉스, HBM 증설")], ENTRIES)
    assert set(frame["ticker"]) == {"005930", "000660"}
    assert report.multi_ticker_articles == 1


def test_the_report_counts_each_article_once_per_bucket():
    frame, report = resolve(
        [
            article("삼성전자 실적", "a1"),
            article("삼성 계열 지배구조", "a2"),
            article("국제 유가", "a3"),
        ],
        ENTRIES,
    )
    assert (report.articles, report.matched_articles) == (3, 1)
    assert (report.ambiguous_articles, report.unmatched_articles) == (1, 1)


def test_the_ambiguous_ratio_is_over_all_articles_not_over_matches():
    """Dividing by matches would swing the ratio on watchlist size rather than
    on alias quality, which is what SPEC §4.2's 30% threshold is about."""
    _, report = resolve(
        [article("삼성전자 실적", "a1")] + [article("삼성 계열", f"x{i}") for i in range(3)],
        ENTRIES,
    )
    assert report.ambiguous_ratio == pytest.approx(3 / 4)


def test_the_threshold_flag_fires_above_thirty_percent():
    report = ResolutionReport(articles=100, ambiguous_articles=31)
    assert report.needs_attention
    assert "ABOVE THRESHOLD" in report.summary()

    report = ResolutionReport(articles=100, ambiguous_articles=29)
    assert not report.needs_attention
    assert "ABOVE THRESHOLD" not in report.summary()
    assert AMBIGUOUS_THRESHOLD == 0.30


def test_an_empty_corpus_does_not_divide_by_zero():
    frame, report = resolve([], ENTRIES)
    assert frame.empty
    assert report.ambiguous_ratio == 0.0
    assert "no articles" in report.summary()


# --- diagnostics ----------------------------------------------------------


def test_dead_aliases_are_reported():
    _, report = resolve([article("삼성전자 실적")], ENTRIES)
    dead = dict.fromkeys(a for _, a in dead_aliases(ENTRIES, report))
    assert "삼전" in dead
    assert "삼성전자" not in dead


def test_coverage_gaps_name_watchlist_tickers_that_never_matched():
    _, report = resolve([article("삼성전자 실적")], ENTRIES)
    gaps = coverage_gaps(ENTRIES, ["005930", "000660", "999999"], report)
    assert "000660" in gaps  # has an entry, matched nothing
    assert "999999" in gaps  # no entry at all
    assert "005930" not in gaps


# --- the committed config -------------------------------------------------


def test_the_committed_aliases_resolve_the_seeded_tickers():
    from src.util.config import load_aliases

    entries = load_aliases()
    matches, _ = resolve_article("삼성전자와 SK하이닉스 동반 상승", entries)
    assert {m.ticker for m in matches} == {"005930", "000660"}
