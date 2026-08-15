"""Tests for scripts/topicality_labels.py.

Offline, against synthetic candidates. The interactive prompt loop is exercised
with a monkeypatched ``prompt`` — the same pattern ``tests/test_golden.py``
uses for ``golden._prompt`` — rather than skipped, since it decides which
answer lands in the file.
"""

from __future__ import annotations

import scripts.topicality_labels as topicality_labels


def test_write_candidates_carries_the_fields_label_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(topicality_labels, "CANDIDATES", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(
        "src.util.config.load_watchlist",
        lambda market=None: [_watchlist_entry("005930", "삼성전자")],
    )

    articles = {
        "a": {
            "title": "삼성전자 실적 발표",
            "description": "본문",
            "outlet": "연합뉴스",
            "link": "https://example.com/a",
            "published_at": "2026-08-15T00:00:00+00:00",
        }
    }
    written = topicality_labels.write_candidates([("a", "005930")], articles)

    assert written == 1
    rows = topicality_labels.read_jsonl(topicality_labels.CANDIDATES)
    assert rows == [
        {
            "article_id": "a",
            "ticker": "005930",
            "name": "삼성전자",
            "title": "삼성전자 실적 발표",
            "description": "본문",
            "outlet": "연합뉴스",
            "link": "https://example.com/a",
            "published_at": "2026-08-15T00:00:00+00:00",
        }
    ]


def test_a_missing_watchlist_name_falls_back_to_empty_string(tmp_path, monkeypatch):
    monkeypatch.setattr(topicality_labels, "CANDIDATES", tmp_path / "candidates.jsonl")
    monkeypatch.setattr("src.util.config.load_watchlist", lambda market=None: [])

    topicality_labels.write_candidates([("a", "005930")], {"a": {}})

    rows = topicality_labels.read_jsonl(topicality_labels.CANDIDATES)
    assert rows[0]["name"] == ""


def test_label_records_a_yes_and_a_no(tmp_path, monkeypatch):
    monkeypatch.setattr(topicality_labels, "CANDIDATES", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(topicality_labels, "LABELS", tmp_path / "topicality_v1.jsonl")

    for row in (
        {"article_id": "a", "ticker": "005930", "title": "on-topic"},
        {"article_id": "b", "ticker": "005930", "title": "off-topic"},
    ):
        topicality_labels.append_jsonl(topicality_labels.CANDIDATES, row)

    answers = iter(["y", "n"])
    monkeypatch.setattr(topicality_labels, "prompt", lambda _: next(answers))

    topicality_labels.run_label()

    labelled = topicality_labels.read_jsonl(topicality_labels.LABELS)
    assert {(r["article_id"], r["topical"]) for r in labelled} == {("a", True), ("b", False)}


def test_label_skips_a_row_that_already_has_a_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(topicality_labels, "CANDIDATES", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(topicality_labels, "LABELS", tmp_path / "topicality_v1.jsonl")

    topicality_labels.append_jsonl(
        topicality_labels.CANDIDATES, {"article_id": "a", "ticker": "005930", "title": "t"}
    )
    topicality_labels.append_jsonl(
        topicality_labels.LABELS,
        {"article_id": "a", "ticker": "005930", "topical": True, "labeled_at": "x"},
    )

    monkeypatch.setattr(
        topicality_labels,
        "prompt",
        lambda _: (_ for _ in ()).throw(AssertionError("should not ask")),
    )

    topicality_labels.run_label()  # must not prompt at all


def test_label_with_no_candidates_reports_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(topicality_labels, "CANDIDATES", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(topicality_labels, "LABELS", tmp_path / "topicality_v1.jsonl")

    assert topicality_labels.run_label() == 1
    assert "sample" in capsys.readouterr().out or "먼저" in capsys.readouterr().out


def test_quitting_stops_before_the_remaining_rows_are_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(topicality_labels, "CANDIDATES", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(topicality_labels, "LABELS", tmp_path / "topicality_v1.jsonl")

    for row in (
        {"article_id": "a", "ticker": "005930", "title": "t1"},
        {"article_id": "b", "ticker": "005930", "title": "t2"},
    ):
        topicality_labels.append_jsonl(topicality_labels.CANDIDATES, row)

    monkeypatch.setattr(topicality_labels, "prompt", lambda _: "q")

    topicality_labels.run_label()

    assert topicality_labels.read_jsonl(topicality_labels.LABELS) == []


def _watchlist_entry(ticker: str, name: str):
    from src.util.config import WatchlistEntry

    return WatchlistEntry(ticker=ticker, name=name, sector="전기·전자", held=False, market="KR")
