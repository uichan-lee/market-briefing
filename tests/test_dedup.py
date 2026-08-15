"""Tests for the SPEC §6.1 Stage 1 dedup cut (src/embed/dedup.py).

No real model is loaded — every test injects a fake ``embed`` that maps
known titles to hand-picked vectors, the same dependency-injection pattern
``tests/test_bakeoff.py`` uses for ``scorer``. This keeps the default test
run independent of the optional ``embed`` extra entirely; the real bge-m3
wrapper (src/embed/encode.py) has its own smoke tests, marked network.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.embed.dedup import DedupReport, _cluster_indices, cluster_duplicates


def fake_embed(vectors: dict[str, list[float]]):
    """An ``embed`` that looks titles up in a fixed table and L2-normalizes,
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


# --- clustering arithmetic ---------------------------------------------------


def test_two_near_identical_vectors_cluster_together():
    similarity = np.array([[1.0, 0.95], [0.95, 1.0]])
    ids = _cluster_indices(similarity, threshold=0.85)
    assert ids[0] == ids[1]


def test_two_dissimilar_vectors_stay_apart():
    similarity = np.array([[1.0, 0.10], [0.10, 1.0]])
    ids = _cluster_indices(similarity, threshold=0.85)
    assert ids[0] != ids[1]


def test_transitive_closure_merges_a_chain():
    """A~B and B~C both clear the bar; A~C alone would not. All three must
    land in one cluster — the reason union-find is used over greedy pairwise
    linking, per the module's own docstring."""
    similarity = np.array(
        [
            [1.00, 0.90, 0.40],
            [0.90, 1.00, 0.90],
            [0.40, 0.90, 1.00],
        ]
    )
    ids = _cluster_indices(similarity, threshold=0.85)
    assert ids[0] == ids[1] == ids[2]


def test_threshold_is_inclusive_at_the_boundary():
    similarity = np.array([[1.0, 0.85], [0.85, 1.0]])
    assert _cluster_indices(similarity, threshold=0.85)[0] == _cluster_indices(similarity, 0.85)[1]


# --- cluster_duplicates: representative selection ----------------------------


def test_a_duplicate_pair_collapses_to_the_longer_description():
    df = matches(("short", "005930"), ("long", "005930"))
    articles = {
        "short": article("삼성전자 실적 발표", "짧음"),
        "long": article("삼성전자, 실적 발표 - 영업이익 급증", "훨씬 긴 본문" * 10),
    }
    embed = fake_embed(
        {"삼성전자 실적 발표": [1, 0], "삼성전자, 실적 발표 - 영업이익 급증": [0.99, 0.14]}
    )

    out, report = cluster_duplicates(df, articles, threshold=0.85, embed=embed)

    assert list(out["article_id"]) == ["long"]
    assert report.input_pairs == 2
    assert report.output_pairs == 1
    assert report.dropped == 1
    assert report.clusters["005930"] == 1


def test_a_tie_in_description_length_breaks_on_article_id_deterministically():
    """Whichever direction the tie-break goes, it must be a function of the
    article_id alone -- not of row order -- so the same input always picks
    the same survivor."""
    articles = {
        "b_article": article("삼성전자 A", "같은 길이"),
        "a_article": article("삼성전자 B", "같은 길이"),
    }
    embed = fake_embed({"삼성전자 A": [1, 0], "삼성전자 B": [0.99, 0.14]})

    forward, _ = cluster_duplicates(
        matches(("b_article", "005930"), ("a_article", "005930")),
        articles,
        threshold=0.85,
        embed=embed,
    )
    reversed_, _ = cluster_duplicates(
        matches(("a_article", "005930"), ("b_article", "005930")),
        articles,
        threshold=0.85,
        embed=embed,
    )

    assert list(forward["article_id"]) == list(reversed_["article_id"])


def test_dissimilar_same_ticker_articles_both_survive():
    df = matches(("a", "005930"), ("b", "005930"))
    articles = {
        "a": article("삼성전자 실적 발표"),
        "b": article("삼성전자 신제품 출시"),
    }
    embed = fake_embed({"삼성전자 실적 발표": [1, 0], "삼성전자 신제품 출시": [0, 1]})

    out, report = cluster_duplicates(df, articles, threshold=0.85, embed=embed)

    assert sorted(out["article_id"]) == ["a", "b"]
    assert report.dropped == 0
    assert report.clusters["005930"] == 2


def test_similar_titles_across_different_tickers_are_not_clustered():
    """Clustering is scoped per ticker — two companies genuinely named in the
    same wire story are two separate judgments (SPEC §6.2 scores per ticker),
    not a duplicate, even if their titles embed identically."""
    df = matches(("a", "005930"), ("b", "000660"))
    articles = {
        "a": article("반도체 업황 호조"),
        "b": article("반도체 업황 호조"),
    }
    embed = fake_embed({"반도체 업황 호조": [1, 0]})

    out, report = cluster_duplicates(df, articles, threshold=0.85, embed=embed)

    assert sorted(out["article_id"]) == ["a", "b"]
    assert report.clusters == {"005930": 1, "000660": 1}


def test_a_single_article_ticker_group_is_kept_trivially():
    df = matches(("a", "005930"))
    articles = {"a": article("삼성전자 단독 기사")}
    embed = fake_embed({"삼성전자 단독 기사": [1, 0]})

    out, report = cluster_duplicates(df, articles, threshold=0.85, embed=embed)

    assert list(out["article_id"]) == ["a"]
    assert report.clusters["005930"] == 1


def test_an_empty_frame_passes_through_untouched():
    df = matches()
    out, report = cluster_duplicates(df, {}, embed=fake_embed({}))
    assert out.empty
    assert report.input_pairs == 0
    assert report.output_pairs == 0


def test_a_missing_article_id_is_read_as_empty_text_not_a_crash():
    """articles lacks a row for one of matches' ids -- .get(..., {}) already
    handles it, this pins the behavior rather than assuming it."""
    df = matches(("known", "005930"), ("orphan", "005930"))
    articles = {"known": article("삼성전자 실적")}
    embed = fake_embed({"삼성전자 실적": [1, 0], "": [0, 1]})

    out, report = cluster_duplicates(df, articles, threshold=0.85, embed=embed)

    assert sorted(out["article_id"]) == ["known", "orphan"]


# --- DedupReport --------------------------------------------------------------


def test_report_summary_states_what_was_dropped():
    report = DedupReport(input_pairs=10, output_pairs=7)
    assert report.dropped == 3
    assert "10 pairs -> 7" in report.summary()
    assert "3 dropped" in report.summary()


# --- config sanity ------------------------------------------------------------


def test_dedup_threshold_clears_the_lowest_known_duplicate():
    """0.85 was picked to clear the lowest known cross-outlet duplicate score
    (0.875) -- SPEC's own placeholder 0.92 does not. See the module docstring
    for why a tighter lower bound is not asserted here: hand-inspection found
    every high-scoring "non-duplicate" checked was actually a duplicate the
    ground truth missed, not a confirmed negative example."""
    from src.embed.dedup import DEDUP_THRESHOLD

    assert DEDUP_THRESHOLD < 0.875
