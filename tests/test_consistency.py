"""Tests for the AI commentary guard (SPEC §2.2⑧).

Synthetic commentary and synthetic ratings. No LLM is called — the point of the
guard is that verifying an LLM's prose requires no LLM.
"""

from __future__ import annotations

import pytest

from src.report.consistency import check_commentary
from src.report.rating import Rating, rate
from src.util.config import AliasEntry, load_aliases

CONFIG = {
    "weights": {"foreign_flow_5d": 0.5, "news_polarity": 0.5},
    "cut_points": {"strong": 2.0, "moderate": 1.0, "weak": 0.4},
    "confidence": {"min_weight_coverage": 0.5},
}

ALIASES = {
    "005930": AliasEntry(
        ticker="005930",
        canonical="삼성전자",
        aliases=("삼성전자", "삼성 전자", "Samsung Electronics"),
        exclude=("삼성전자우", "삼성물산", "삼성SDI"),
        ambiguous_parents=("삼성",),
    ),
    "000660": AliasEntry(
        ticker="000660",
        canonical="SK하이닉스",
        aliases=("SK하이닉스", "하이닉스", "SK Hynix"),
        exclude=(),
        ambiguous_parents=("SK",),
    ),
}


def _rated(ticker: str, z: float):
    return rate(ticker, {"foreign_flow_5d": z, "news_polarity": z}, CONFIG)


BUY = {"005930": _rated("005930", 1.5)}  # 매수
SELL = {"000660": _rated("000660", -1.5)}  # 매도


def test_the_fixtures_are_the_ratings_the_tests_assume():
    assert BUY["005930"].rating is Rating.BUY
    assert SELL["000660"].rating is Rating.SELL


# --- agreement passes ----------------------------------------------------


def test_commentary_restating_the_computed_rating_passes():
    text = "- **005930 삼성전자 (매수, +1.10)** — HBM 계약 확대."
    report = check_commentary(text, BUY, ALIASES)
    assert report.ok
    assert report.checked_lines == 1


def test_commentary_with_no_rating_label_passes():
    text = "- 005930 삼성전자 — 외국인 순매수 3일 연속, 수급 유입."
    report = check_commentary(text, BUY, ALIASES)
    assert report.ok
    assert report.checked_lines == 0


def test_a_market_level_statement_with_no_ticker_is_not_attributed():
    """Nothing to compare against, so nothing to fail."""
    text = "오늘 밤 FOMC. 그 전 신규 매수는 정보가 아니라 도박."
    report = check_commentary(text, BUY, ALIASES)
    assert report.ok
    assert report.checked_lines == 0


def test_a_ticker_absent_from_the_ratings_is_skipped():
    text = "- 012450 한화에어로스페이스 (매수) — 수주 확대."
    report = check_commentary(text, BUY, ALIASES)
    assert report.ok
    assert report.checked_lines == 0


# --- contradiction is caught ---------------------------------------------


def test_a_different_label_on_the_same_ticker_is_a_contradiction():
    text = "- **000660 SK하이닉스 (매수)** — 저가 진입 구간."
    report = check_commentary(text, SELL, ALIASES)

    assert not report.ok
    (found,) = report.contradictions
    assert found.ticker == "000660"
    assert found.stated is Rating.BUY
    assert found.computed is Rating.SELL


def test_the_drift_case_the_guard_exists_for():
    """Formally correct label, then prose pulling the other way on the same line."""
    text = "- **000660 SK하이닉스 (매도, −1.56)** — 과매도 구간이라 약한 매수 관점도 가능."
    report = check_commentary(text, SELL, ALIASES)

    assert not report.ok
    assert [c.stated for c in report.contradictions] == [Rating.WEAK_BUY]


def test_the_ticker_code_alone_is_enough_to_attribute():
    report = check_commentary("000660 관망 권고", SELL, ALIASES)
    assert not report.ok
    assert report.contradictions[0].stated is Rating.HOLD


def test_an_alias_is_enough_to_attribute():
    report = check_commentary("하이닉스는 강한 매수 구간.", SELL, ALIASES)
    assert not report.ok
    assert report.contradictions[0].ticker == "000660"


def test_every_offending_label_on_a_line_is_reported():
    text = "000660 강한 매수 또는 약한 매수."
    report = check_commentary(text, SELL, ALIASES)
    assert {c.stated for c in report.contradictions} == {
        Rating.STRONG_BUY,
        Rating.WEAK_BUY,
    }


def test_contradictions_are_found_across_multiple_lines():
    text = "- 005930 삼성전자 (매수) — 정상.\n- 하이닉스 (매수) — 모순.\n"
    report = check_commentary(text, {**BUY, **SELL}, ALIASES)
    assert len(report.contradictions) == 1
    assert report.contradictions[0].ticker == "000660"


# --- the substring trap --------------------------------------------------


def test_strong_buy_is_not_read_as_buy():
    """강한 매수 contains 매수. Matching shortest-first would report a phantom."""
    ratings = {"005930": _rated("005930", 2.5)}
    assert ratings["005930"].rating is Rating.STRONG_BUY

    report = check_commentary("005930 삼성전자 (강한 매수)", ratings, ALIASES)
    assert report.ok


def test_weak_sell_is_not_read_as_sell():
    ratings = {"000660": _rated("000660", -0.5)}
    assert ratings["000660"].rating is Rating.WEAK_SELL

    report = check_commentary("000660 (약한 매도)", ratings, ALIASES)
    assert report.ok


def test_a_label_written_without_its_space_still_matches():
    report = check_commentary("000660 강한매수", SELL, ALIASES)
    assert not report.ok
    assert report.contradictions[0].stated is Rating.STRONG_BUY


# --- ordinary market vocabulary is not a rating claim --------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "외국인 순매수 3일 연속",  # the prompt's own preferred wording
        "기관 순매도 전환",
        "매수세가 약하다",
        "매도호가 잔량 증가",
        "매수량 급감",
        "차익 실현 매도세",
    ],
)
def test_compound_market_words_are_not_read_as_rating_labels(phrase):
    """Without this the guard would fire on nearly every line and the section
    would be dropped daily — a checker nobody can trust is worse than none."""
    report = check_commentary(f"- 000660 하이닉스 — {phrase}.", SELL, ALIASES)
    assert report.ok
    assert report.checked_lines == 0


@pytest.mark.parametrize("particle", ["는", "가", "로", "를", "의", "도"])
def test_a_label_with_a_trailing_particle_is_still_a_claim(particle):
    """Particles are a closed class, so they can be allowed without opening the
    door to compounds."""
    report = check_commentary(f"- 000660 하이닉스 매수{particle} 유효.", SELL, ALIASES)
    assert not report.ok
    assert report.contradictions[0].stated is Rating.BUY


# --- entity resolution ---------------------------------------------------


def test_an_excluded_name_does_not_attribute_to_the_parent_ticker():
    """삼성전자우 is a different security. Attributing its line to 005930 would
    manufacture a contradiction out of a correct sentence."""
    report = check_commentary("삼성전자우는 매도 구간.", BUY, ALIASES)
    assert report.ok
    assert report.checked_lines == 0


def test_a_line_naming_two_tickers_is_ambiguous_not_a_failure():
    text = "005930 삼성전자는 하이닉스 대비 강세, 매수 우위."
    report = check_commentary(text, {**BUY, **SELL}, ALIASES)

    assert report.ok
    (ambiguous,) = report.ambiguous
    assert set(ambiguous.tickers) == {"005930", "000660"}
    assert report.checked_lines == 0


def test_ambiguous_lines_are_counted_in_the_summary():
    text = "005930 삼성전자는 하이닉스 대비 강세, 매수 우위."
    report = check_commentary(text, {**BUY, **SELL}, ALIASES)
    assert "1 ambiguous" in report.summary()


# --- the report ----------------------------------------------------------


def test_summary_names_the_offending_tickers():
    text = "- 하이닉스 (매수) — 모순."
    summary = check_commentary(text, SELL, ALIASES).summary()
    assert "DROPPED" in summary
    assert "000660" in summary


def test_summary_of_a_clean_commentary_says_so():
    text = "- 005930 삼성전자 (매수) — 정상."
    summary = check_commentary(text, BUY, ALIASES).summary()
    assert "DROPPED" not in summary
    assert "1 attributed lines consistent" in summary


def test_empty_commentary_is_vacuously_ok():
    report = check_commentary("", BUY, ALIASES)
    assert report.ok
    assert report.checked_lines == 0


def test_contradiction_renders_both_labels_for_the_header():
    text = "- 하이닉스 (매수) — 모순."
    (found,) = check_commentary(text, SELL, ALIASES).contradictions
    rendered = str(found)
    assert "매수" in rendered
    assert "매도" in rendered
    assert "000660" in rendered


# --- against the committed alias file ------------------------------------


def test_the_committed_aliases_drive_the_guard():
    """The guard must work with config/aliases.yaml as actually committed."""
    aliases = load_aliases()
    ratings = {"005930": _rated("005930", -1.5)}  # 매도

    report = check_commentary("삼성전자는 매수 의견.", ratings, aliases)
    assert not report.ok
    assert report.contradictions[0].ticker == "005930"


@pytest.mark.parametrize("rating_label", [r.value for r in Rating])
def test_every_label_on_the_scale_is_detectable(rating_label):
    """A label the guard cannot see is a hole in the guard."""
    ratings = {"005930": _rated("005930", 1.5)}  # 매수
    report = check_commentary(f"005930 {rating_label}", ratings, ALIASES)

    if rating_label == Rating.BUY.value:
        assert report.ok
    else:
        assert not report.ok


# --- attribution across lines ---------------------------------------------


def test_a_label_on_the_next_line_inherits_the_paragraph_subject():
    """The hole this guard had until 2026-08-16.

    Attribution was strictly per line, so the most ordinary shape Korean prose
    takes — subject in one sentence, verdict in the next — stated the exact
    opposite of §2.2⑥ and came back `ok` with `checked_lines` at zero.
    """
    ratings = {"005930": _rated("005930", -2.5)}  # 강한 매도
    text = "005930 삼성전자의 수급이 개선됐다.\n따라서 강한 매수 의견이다."

    report = check_commentary(text, ratings, ALIASES)
    assert not report.ok
    assert report.contradictions[0].ticker == "005930"
    assert report.contradictions[0].stated is Rating.STRONG_BUY


def test_a_blank_line_ends_the_paragraph_and_clears_the_subject():
    """Otherwise a subject would leak into a later paragraph about the market."""
    ratings = {"005930": _rated("005930", -2.5)}  # 강한 매도
    text = "005930 삼성전자의 수급이 개선됐다.\n\n전반적으로 강한 매수 국면이다."

    report = check_commentary(text, ratings, ALIASES)
    assert report.ok
    assert report.checked_lines == 0


def test_a_paragraph_naming_two_tickers_attributes_nothing_to_its_bare_lines():
    """Same 'drop rather than guess' stance the per-line case took, one scope out."""
    ratings = {"005930": _rated("005930", -2.5), "000660": _rated("000660", 2.5)}
    text = "005930 삼성전자와 000660 SK하이닉스를 비교하면.\n강한 매수 의견이다."

    report = check_commentary(text, ratings, ALIASES)
    assert report.ok
    assert report.ambiguous


def test_a_line_naming_its_own_ticker_wins_over_the_paragraph_subject():
    ratings = {"005930": _rated("005930", 1.5), "000660": _rated("000660", -2.5)}
    text = "005930 삼성전자는 좋다.\n000660 SK하이닉스는 강한 매수다."

    report = check_commentary(text, ratings, ALIASES)
    assert not report.ok
    assert report.contradictions[0].ticker == "000660"


def test_excluded_forms_are_masked_without_bridging_an_alias():
    """`resolve.py` masks with \\x00 so masking cannot create a match; this
    module used spaces, and 45 committed aliases contain one.

    (Masking is plain `str.replace`, so \\x00 is safe here — unlike as a pandas
    group key, where it truncates. See `src.features.compute.NO_SECTOR`.)"""
    entry = AliasEntry(
        ticker="005930",
        canonical="삼성전자",
        aliases=("삼성 전자",),
        exclude=("X",),
        ambiguous_parents=(),
    )
    ratings = {"005930": _rated("005930", -2.5)}
    # "삼성X전자" is not a mention; blanking X to a space would forge "삼성 전자".
    report = check_commentary("삼성X전자 강한 매수", ratings, {"005930": entry})
    assert report.ok
    assert report.checked_lines == 0
