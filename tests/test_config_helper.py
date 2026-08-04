"""Tests for the alias worksheet and audit tooling.

The masking order in :func:`match_article` is the part worth testing: it is the
difference between "삼성전자우 news is dropped" and "every 삼성전자 article that
mentions the preferred share is also dropped", and both look identical in a
summary count.
"""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.config_helper import build_scaffold, match_article
from src.util.config import AliasEntry


def entry(ticker, canonical, aliases, exclude=(), parents=()):
    return AliasEntry(
        ticker=ticker,
        canonical=canonical,
        aliases=tuple(aliases),
        exclude=tuple(exclude),
        ambiguous_parents=tuple(parents),
    )


SAMSUNG = entry(
    "005930",
    "삼성전자",
    ["삼성전자", "Samsung Electronics"],
    exclude=["삼성전자우", "삼성물산"],
    parents=["삼성"],
)
HYNIX = entry("000660", "SK하이닉스", ["SK하이닉스", "하이닉스"], parents=["SK"])
ENTRIES = {"005930": SAMSUNG, "000660": HYNIX}


# --- matching ------------------------------------------------------------


def test_a_plain_alias_matches():
    matched, ambiguous = match_article("삼성전자 4분기 실적 발표", ENTRIES)
    assert matched == {"005930"}
    assert ambiguous == set()


def test_an_excluded_superstring_does_not_match_its_base():
    # 삼성전자우 contains 삼성전자. Without masking this article would be
    # attributed to 005930, which is the exact misattribution `exclude` exists
    # to prevent.
    matched, _ = match_article("삼성전자우 배당락", ENTRIES)
    assert matched == set()


def test_a_standalone_mention_survives_an_excluded_form_in_the_same_text():
    # The failure this guards against is over-correction: masking must remove
    # only the excluded span, not veto the whole article.
    matched, _ = match_article("삼성전자와 삼성전자우가 동반 상승", ENTRIES)
    assert matched == {"005930"}


def test_the_longest_excluded_form_is_masked_first():
    entries = {
        "005930": entry("005930", "삼성전자", ["삼성전자"], exclude=["삼성전자", "삼성전자서비스"])
    }
    # Masking "삼성전자" first would leave "서비스" behind and the longer form
    # would never be recognised. Sorting by length descending is load-bearing.
    matched, _ = match_article("삼성전자서비스 채용", entries)
    assert matched == set()


def test_a_group_name_alone_is_ambiguous_not_matched():
    matched, ambiguous = match_article("삼성 계열사 지배구조 개편", ENTRIES)
    assert matched == set()
    assert ambiguous == {"005930"}


def test_an_alias_wins_over_its_own_group_name():
    matched, ambiguous = match_article("삼성 그룹의 삼성전자, 신규 투자", ENTRIES)
    assert matched == {"005930"}
    assert "005930" not in ambiguous


def test_two_tickers_can_match_one_article():
    matched, _ = match_article("삼성전자와 SK하이닉스, HBM 증설", ENTRIES)
    assert matched == {"005930", "000660"}


def test_an_english_alias_matches():
    matched, _ = match_article("Samsung Electronics beats estimates", ENTRIES)
    assert matched == {"005930"}


def test_an_unrelated_article_matches_nothing():
    matched, ambiguous = match_article("국제 유가 하락", ENTRIES)
    assert matched == set()
    assert ambiguous == set()


# --- scaffold ------------------------------------------------------------


@pytest.fixture
def listing():
    return pd.DataFrame(
        {
            "name": [
                "삼성전자",
                "삼성전자우",
                "삼성물산",
                "삼성SDI",
                "삼성전기",
                "SK하이닉스",
                "NAVER",
            ],
            "sector": ["반도체"] * 5 + ["반도체", "서비스"],
            "market": ["KOSPI"] * 7,
        },
        index=["005930", "005935", "028260", "006400", "009150", "000660", "035420"],
    )


def test_superstring_listings_become_mandatory_excludes(listing):
    card = build_scaffold("005930", listing)
    # 삼성전자우 contains 삼성전자; 삼성물산 does not, so it is a sibling hint
    # rather than a mandatory exclude.
    assert card.must_exclude == ["삼성전자우"]
    assert "삼성물산" not in card.must_exclude


def test_the_group_prefix_is_derived_from_the_listing(listing):
    assert build_scaffold("005930", listing).group == "삼성"


def test_siblings_are_group_members_that_are_not_already_excluded(listing):
    card = build_scaffold("005930", listing)
    assert set(card.siblings) == {"삼성물산", "삼성SDI", "삼성전기"}


def test_a_name_with_no_group_gets_no_ambiguous_parent(listing):
    # Only one listing starts with "NA", so no prefix reaches the group
    # threshold and nothing is proposed. Guessing here would drop real articles.
    card = build_scaffold("035420", listing)
    assert card.group is None
    assert card.siblings == []


def test_aliases_are_never_produced(listing):
    # The whole point of the CLAUDE.md carve-out: the scaffold supplies the
    # fields that can only lose coverage, never the one that can misattribute.
    card = build_scaffold("005930", listing)
    assert not hasattr(card, "aliases")
