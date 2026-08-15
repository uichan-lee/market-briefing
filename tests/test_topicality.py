"""Tests for the SPEC §6.1 Stage 1 topicality cut (src/embed/topicality.py).

No real model is loaded — every test injects a fake ``embed`` that maps known
texts to hand-picked vectors, the same dependency-injection pattern
``tests/test_dedup.py`` uses. This keeps the default test run independent of
the optional ``embed`` extra entirely.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.embed.topicality import (
    TopicalityReport,
    build_profile_sentence,
    build_profiles,
    filter_topicality,
)
from src.util.config import WatchlistEntry


def fake_embed(vectors: dict[str, list[float]]):
    """An ``embed`` that looks texts up in a fixed table and L2-normalizes,
    matching the real function's contract so callers computing a dot product
    get true cosine similarity."""

    def embed(texts):
        rows = []
        for text in texts:
            if text not in vectors:
                raise KeyError(f"no fake vector for {text!r} — test forgot to declare it")
            v = np.array(vectors[text], dtype=np.float64)
            rows.append(v / np.linalg.norm(v))
        return np.array(rows) if rows else np.empty((0, 0))

    return embed


def matches(*pairs: tuple[str, str]) -> pd.DataFrame:
    """``pairs`` of (article_id, ticker), matching resolve()'s row shape."""
    return pd.DataFrame(pairs, columns=["article_id", "ticker"])


def article(title: str, description: str = "") -> dict:
    return {"title": title, "description": description}


def entry(ticker: str, name: str, sector: str | None = "전기·전자") -> WatchlistEntry:
    return WatchlistEntry(ticker=ticker, name=name, sector=sector, held=False, market="KR")


# --- build_profile_sentence / build_profiles ---------------------------------


def test_profile_sentence_includes_name_ticker_and_sector():
    sentence = build_profile_sentence(entry("005930", "삼성전자", "전기·전자"))
    assert sentence == "삼성전자 (005930), 전기·전자"


def test_profile_sentence_omits_sector_when_absent():
    sentence = build_profile_sentence(entry("005930", "삼성전자", None))
    assert sentence == "삼성전자 (005930)"


def test_build_profiles_keys_by_ticker():
    profiles = build_profiles([entry("005930", "삼성전자"), entry("000660", "SK하이닉스")])
    assert profiles == {
        "005930": "삼성전자 (005930), 전기·전자",
        "000660": "SK하이닉스 (000660), 전기·전자",
    }


# --- filter_topicality: mechanics --------------------------------------------


def test_an_article_close_to_its_profile_survives():
    df = matches(("a", "005930"))
    articles = {"a": article("삼성전자 3분기 영업이익 급증")}
    profiles = {"005930": "삼성전자 (005930), 전기·전자"}
    embed = fake_embed(
        {"삼성전자 (005930), 전기·전자": [1, 0], "삼성전자 3분기 영업이익 급증": [0.99, 0.14]}
    )

    out, report = filter_topicality(df, articles, profiles, threshold=0.85, embed=embed)

    assert list(out["article_id"]) == ["a"]
    assert report.input_pairs == 1
    assert report.output_pairs == 1
    assert report.dropped == 0
    assert report.kept["005930"] == 1


def test_an_article_far_from_its_profile_is_dropped():
    df = matches(("a", "005930"))
    articles = {"a": article("무관한 기사 제목")}
    profiles = {"005930": "삼성전자 (005930), 전기·전자"}
    embed = fake_embed({"삼성전자 (005930), 전기·전자": [1, 0], "무관한 기사 제목": [0, 1]})

    out, report = filter_topicality(df, articles, profiles, threshold=0.85, embed=embed)

    assert out.empty
    assert report.dropped == 1
    assert report.kept["005930"] == 0


def test_threshold_is_inclusive_at_the_boundary():
    df = matches(("a", "005930"))
    articles = {"a": article("삼성전자 뉴스")}
    profiles = {"005930": "프로필"}
    # cosine similarity between [1, 0] and [0.85, sqrt(1 - 0.85^2)] is exactly 0.85
    embed = fake_embed({"프로필": [1, 0], "삼성전자 뉴스": [0.85, (1 - 0.85**2) ** 0.5]})

    out, _ = filter_topicality(df, articles, profiles, threshold=0.85, embed=embed)

    assert list(out["article_id"]) == ["a"]


def test_each_ticker_is_scored_against_its_own_profile():
    df = matches(("a", "005930"), ("b", "000660"))
    articles = {
        "a": article("삼성전자 실적"),
        "b": article("SK하이닉스 실적"),
    }
    profiles = {
        "005930": "삼성전자 프로필",
        "000660": "SK하이닉스 프로필",
    }
    embed = fake_embed(
        {
            "삼성전자 프로필": [1, 0],
            "SK하이닉스 프로필": [0, 1],
            "삼성전자 실적": [0.99, 0.14],
            "SK하이닉스 실적": [0.14, 0.99],
        }
    )

    out, report = filter_topicality(df, articles, profiles, threshold=0.85, embed=embed)

    assert sorted(out["article_id"]) == ["a", "b"]
    assert report.kept == {"005930": 1, "000660": 1}


def test_a_missing_profile_raises_rather_than_passing_through():
    df = matches(("a", "005930"))
    articles = {"a": article("삼성전자 뉴스")}
    with pytest.raises(KeyError, match="005930"):
        filter_topicality(df, articles, {}, threshold=0.85, embed=fake_embed({}))


def test_an_empty_frame_passes_through_untouched():
    df = matches()
    out, report = filter_topicality(df, {}, {}, threshold=0.85, embed=fake_embed({}))
    assert out.empty
    assert report.input_pairs == 0
    assert report.output_pairs == 0


def test_a_missing_article_id_is_read_as_empty_text_not_a_crash():
    """articles lacks a row for one of matches' ids -- .get(..., {}) already
    handles it, and the resulting empty text should score far from any real
    profile sentence rather than crash."""
    df = matches(("known", "005930"), ("orphan", "005930"))
    articles = {"known": article("삼성전자 실적")}
    profiles = {"005930": "삼성전자 프로필"}
    embed = fake_embed({"삼성전자 프로필": [1, 0], "삼성전자 실적": [0.99, 0.14], "": [0, 1]})

    out, _ = filter_topicality(df, articles, profiles, threshold=0.85, embed=embed)

    assert list(out["article_id"]) == ["known"]


def test_article_text_is_injectable():
    """A custom ``article_text`` -- e.g. title-only -- replaces the default
    title+description join, the same way ``embed`` is injectable."""
    df = matches(("a", "005930"))
    articles = {"a": article("삼성전자 실적", "이 설명은 읽히지 않아야 한다")}
    profiles = {"005930": "삼성전자 프로필"}
    embed = fake_embed({"삼성전자 프로필": [1, 0], "삼성전자 실적": [0.99, 0.14]})

    out, _ = filter_topicality(
        df,
        articles,
        profiles,
        threshold=0.85,
        embed=embed,
        article_text=lambda a: str(a.get("title", "")),
    )

    assert list(out["article_id"]) == ["a"]


# --- TopicalityReport ---------------------------------------------------------


def test_report_summary_states_what_was_dropped():
    report = TopicalityReport(input_pairs=10, output_pairs=6)
    assert report.dropped == 4
    assert "10 pairs -> 6" in report.summary()
    assert "4 dropped" in report.summary()
