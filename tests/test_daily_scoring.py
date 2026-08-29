"""Tests for the production news-scoring driver. SPEC §6.2/§6.3 archive,
§2.2③ news_polarity's producer side.

Offline tests mock the scorer directly (an injectable ``scorer=`` callable,
the same pattern ``src.eval.bakeoff.run`` uses) rather than going through
``adapter._call`` — this module's own logic (idempotency, archive shape,
failure isolation, the four validation checks) is what's under test, not
the adapter, which ``tests/test_adapter.py``/``tests/test_synthesize.py``
already cover.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

import pandas as pd
import pytest

from src.llm.adapter import AdapterError, Completion, SchemaError
from src.llm.daily_scoring import (
    CONTINUITY_MAX_AGE_HOURS,
    already_scored_pairs,
    check_known_scoring,
    check_scoring_continuity,
    load_news_polarity_frame,
    resolved_candidates,
    score_new_articles,
    score_path,
    write_scores,
)
from src.util.config import AliasEntry, WatchlistEntry

MODELS = {"scoring": {"provider": "openai", "model": "gpt-5.4", "temperature": None}}


def _article(
    article_id="a1", ticker=None, *, title="삼성전자 실적", collected="2026-08-20T02:00:00+00:00"
):
    return {
        "article_id": article_id,
        "feed": "test",
        "outlet": "test",
        "title": title,
        "link": f"https://example.com/{article_id}",
        "description": "본문",
        "published_at": collected,
        "collected_at_utc": collected,
        "known_at_utc": collected,
    }


def _write_news(root: Path, day: dt.date, articles: list[dict]) -> Path:
    directory = root / "raw" / "kr" / "news" / day.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "0900.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in articles:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _aliases() -> dict[str, AliasEntry]:
    return {
        "005930": AliasEntry(
            ticker="005930",
            canonical="삼성전자",
            aliases=("삼성전자",),
            exclude=(),
            ambiguous_parents=(),
        )
    }


def _watchlist() -> list[WatchlistEntry]:
    return [
        WatchlistEntry(ticker="005930", name="삼성전자", sector="반도체", held=False, market="KR")
    ]


def _score_row(article_id="a1", ticker="005930", *, model_id="gpt-5.4", prompt_version="v1"):
    return {
        "article_id": article_id,
        "ticker": ticker,
        "model_id": model_id,
        "prompt_version": prompt_version,
        "relevance": 0.7,
        "polarity": 0.4,
        "intensity": 0.3,
        "uncertainty": 0.2,
        "forwardness": 0.3,
        "rationale": "실적 호조",
        "temperature": None,
    }


def _completion(**scores) -> Completion:
    parsed = {
        "relevance": 0.7,
        "polarity": 0.4,
        "intensity": 0.3,
        "uncertainty": 0.2,
        "forwardness": 0.3,
        "rationale": "실적 호조",
        **scores,
    }
    return Completion(
        parsed=parsed,
        model_id="gpt-5.4",
        prompt_version="v1",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.001,
        latency_s=0.5,
    )


# --- resolved_candidates / already_scored_pairs -----------------------------


def test_resolved_candidates_finds_a_resolved_pair_inside_the_window(tmp_path, monkeypatch):
    day = dt.date(2026, 8, 20)
    _write_news(tmp_path, day, [_article()])
    monkeypatch.setattr("src.llm.daily_scoring.load_aliases", _aliases)
    monkeypatch.setattr("src.llm.daily_scoring.load_watchlist", lambda market=None: _watchlist())

    now = pd.Timestamp("2026-08-21 00:00", tz="UTC")
    candidates = resolved_candidates(tmp_path, window_days=4, now=now)

    assert len(candidates) == 1
    assert candidates[0]["article_id"] == "a1"
    assert candidates[0]["ticker"] == "005930"
    assert candidates[0]["collected_at_utc"] == "2026-08-20T02:00:00+00:00"


def test_resolved_candidates_excludes_articles_outside_the_window(tmp_path, monkeypatch):
    old_day = dt.date(2026, 8, 1)
    _write_news(tmp_path, old_day, [_article()])
    monkeypatch.setattr("src.llm.daily_scoring.load_aliases", _aliases)
    monkeypatch.setattr("src.llm.daily_scoring.load_watchlist", lambda market=None: _watchlist())

    now = pd.Timestamp("2026-08-21 00:00", tz="UTC")
    assert resolved_candidates(tmp_path, window_days=4, now=now) == []


def test_already_scored_pairs_reads_across_the_lookback_window(tmp_path):
    day = dt.date(2026, 8, 19)
    write_scores(tmp_path, day, "gpt-5.4", "v1", [_score_row()])

    now = pd.Timestamp("2026-08-20 12:00", tz="UTC")
    pairs = already_scored_pairs(
        tmp_path, model_id="gpt-5.4", prompt_version="v1", now=now, lookback_days=4
    )
    assert ("a1", "005930") in pairs


def test_already_scored_pairs_is_specific_to_model_and_prompt_version(tmp_path):
    day = dt.date(2026, 8, 20)
    write_scores(tmp_path, day, "gpt-5.4", "v1", [_score_row()])

    now = pd.Timestamp("2026-08-20 12:00", tz="UTC")
    pairs = already_scored_pairs(tmp_path, model_id="claude-sonnet-5", prompt_version="v1", now=now)
    assert pairs == set()


# --- write_scores / score_path ----------------------------------------------


def test_score_path_matches_specs_exact_filename():
    path = score_path(Path("/data"), dt.date(2026, 8, 20), "gpt-5.4", "v1")
    assert path.name == "2026-08-20__gpt-5.4__v1.jsonl"


def test_write_scores_appends_never_truncates(tmp_path):
    day = dt.date(2026, 8, 20)
    write_scores(tmp_path, day, "gpt-5.4", "v1", [_score_row("a1")])
    write_scores(tmp_path, day, "gpt-5.4", "v1", [_score_row("a2")])

    path = score_path(tmp_path, day, "gpt-5.4", "v1")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    ids = {json.loads(line)["article_id"] for line in lines}
    assert ids == {"a1", "a2"}


# --- score_new_articles: idempotency, failure tolerance ---------------------


def test_score_new_articles_never_rescores_an_already_archived_pair(tmp_path, monkeypatch):
    day = dt.date(2026, 8, 20)
    _write_news(tmp_path, day, [_article("a1")])
    monkeypatch.setattr("src.llm.daily_scoring.load_aliases", _aliases)
    monkeypatch.setattr("src.llm.daily_scoring.load_watchlist", lambda market=None: _watchlist())
    monkeypatch.setattr("src.llm.daily_scoring.missing_credential", lambda provider: None)
    write_scores(tmp_path, day, "gpt-5.4", "v1", [_score_row("a1")])

    def angry_scorer(article, *, prompt, models=None):
        raise AssertionError("must not re-score an already-archived pair")

    now = pd.Timestamp("2026-08-20 12:00", tz="UTC")
    frame, report = score_new_articles(
        tmp_path, now=now, models=MODELS, scorer=angry_scorer, known_value_check=False
    )
    assert frame.empty
    assert report.ok


def test_score_new_articles_isolates_a_per_article_failure(tmp_path, monkeypatch):
    day = dt.date(2026, 8, 20)
    _write_news(tmp_path, day, [_article("a1"), _article("a2", title="삼성전자 신제품 출시")])
    monkeypatch.setattr("src.llm.daily_scoring.load_aliases", _aliases)
    monkeypatch.setattr("src.llm.daily_scoring.load_watchlist", lambda market=None: _watchlist())
    monkeypatch.setattr("src.llm.daily_scoring.missing_credential", lambda provider: None)

    def flaky_scorer(article, *, prompt, models=None):
        if article["article_id"] == "a1":
            raise SchemaError("bad json")
        return _completion()

    now = pd.Timestamp("2026-08-20 12:00", tz="UTC")
    frame, report = score_new_articles(
        tmp_path, now=now, models=MODELS, scorer=flaky_scorer, known_value_check=False
    )
    assert len(frame) == 1
    assert frame.iloc[0]["article_id"] == "a2"
    # a1's failure surfaces as an outstanding candidate, not a crash — and
    # since it's well within CONTINUITY_MAX_AGE_HOURS of `now`, it does not
    # yet fail the continuity check.
    assert report.ok


def test_score_new_articles_writes_the_successful_records(tmp_path, monkeypatch):
    day = dt.date(2026, 8, 20)
    _write_news(tmp_path, day, [_article("a1")])
    monkeypatch.setattr("src.llm.daily_scoring.load_aliases", _aliases)
    monkeypatch.setattr("src.llm.daily_scoring.load_watchlist", lambda market=None: _watchlist())
    monkeypatch.setattr("src.llm.daily_scoring.missing_credential", lambda provider: None)

    now = pd.Timestamp("2026-08-20 12:00", tz="UTC")
    frame, report = score_new_articles(
        tmp_path,
        now=now,
        models=MODELS,
        scorer=lambda article, *, prompt, models=None: _completion(),
        known_value_check=False,
    )
    assert len(frame) == 1
    assert report.ok
    archived = score_path(tmp_path, now.date(), "gpt-5.4", "v1")
    assert archived.exists()
    row = json.loads(archived.read_text(encoding="utf-8").splitlines()[0])
    assert row["article_id"] == "a1"
    assert row["model_id"] == "gpt-5.4"


def test_score_new_articles_checkpoints_before_the_full_batch(tmp_path, monkeypatch):
    import src.llm.daily_scoring as scoring

    day = dt.date(2026, 8, 20)
    _write_news(tmp_path, day, [_article("a1"), _article("a2", title="삼성전자 신제품")])
    monkeypatch.setattr(scoring, "load_aliases", _aliases)
    monkeypatch.setattr(scoring, "load_watchlist", lambda market=None: _watchlist())
    monkeypatch.setattr(scoring, "missing_credential", lambda provider: None)
    original_write = scoring.write_scores
    checkpoints: list[int] = []

    def capture_write(root, date, model_id, prompt_version, records):
        checkpoints.append(len(records))
        return original_write(root, date, model_id, prompt_version, records)

    monkeypatch.setattr(scoring, "write_scores", capture_write)
    frame, report = scoring.score_new_articles(
        tmp_path,
        now=pd.Timestamp("2026-08-20 12:00", tz="UTC"),
        models=MODELS,
        scorer=lambda article, **kwargs: _completion(),
        known_value_check=False,
        checkpoint_size=1,
    )

    assert report.ok, report.summary()
    assert len(frame) == 2
    assert checkpoints == [1, 1]
    assert len(score_path(tmp_path, day, "gpt-5.4", "v1").read_text().splitlines()) == 2


def test_score_new_articles_rejects_a_nonpositive_checkpoint_size(tmp_path):
    with pytest.raises(ValueError, match="checkpoint_size"):
        score_new_articles(tmp_path, checkpoint_size=0)


def test_score_new_articles_skips_entirely_without_a_credential(tmp_path, monkeypatch):
    day = dt.date(2026, 8, 20)
    _write_news(tmp_path, day, [_article("a1")])
    monkeypatch.setattr("src.llm.daily_scoring.load_aliases", _aliases)
    monkeypatch.setattr("src.llm.daily_scoring.load_watchlist", lambda market=None: _watchlist())
    monkeypatch.setattr(
        "src.llm.daily_scoring.missing_credential", lambda provider: "OPENAI_API_KEY"
    )

    def angry_scorer(article, *, prompt, models=None):
        raise AssertionError("must not be called without a credential")

    now = pd.Timestamp("2026-08-20 12:00", tz="UTC")
    frame, report = score_new_articles(
        tmp_path, now=now, models=MODELS, scorer=angry_scorer, known_value_check=False
    )
    assert frame.empty
    assert not report.ok
    assert any("OPENAI_API_KEY" in f.detail for f in report.failures)


def test_a_quiet_run_is_not_a_failure(tmp_path, monkeypatch):
    """No news at all this window — matches kr_news's own stance that an
    empty poll is not a validation failure."""
    monkeypatch.setattr("src.llm.daily_scoring.load_aliases", _aliases)
    monkeypatch.setattr("src.llm.daily_scoring.load_watchlist", lambda market=None: _watchlist())
    monkeypatch.setattr("src.llm.daily_scoring.missing_credential", lambda provider: None)

    now = pd.Timestamp("2026-08-20 12:00", tz="UTC")
    frame, report = score_new_articles(
        tmp_path,
        now=now,
        models=MODELS,
        scorer=lambda article, *, prompt, models=None: _completion(),
        known_value_check=False,
    )
    assert frame.empty
    assert report.ok


# --- the four checks ---------------------------------------------------------


def test_check_scoring_continuity_passes_a_fresh_outstanding_pair():
    now = pd.Timestamp("2026-08-20 12:00", tz="UTC")
    outstanding = [
        {"article_id": "a1", "ticker": "005930", "collected_at_utc": "2026-08-20T11:00:00+00:00"}
    ]
    result = check_scoring_continuity(outstanding, now=now)
    assert result.passed


def test_check_scoring_continuity_fails_a_stale_outstanding_pair():
    now = pd.Timestamp("2026-08-20 12:00", tz="UTC")
    stale_at = (now - pd.Timedelta(hours=CONTINUITY_MAX_AGE_HOURS + 1)).isoformat()
    outstanding = [{"article_id": "a1", "ticker": "005930", "collected_at_utc": stale_at}]
    result = check_scoring_continuity(outstanding, now=now)
    assert not result.passed
    assert "a1/005930" in result.detail


def test_check_known_scoring_passes_within_tolerance(monkeypatch):
    from src.eval import bakeoff

    fixed = bakeoff.examples()[0]
    label = fixed["label"]

    def close_scorer(article, *, prompt, models=None):
        return _completion(relevance=label["relevance"], polarity=label["polarity"])

    result = check_known_scoring(close_scorer, prompt=object())
    assert result.passed


def test_check_known_scoring_fails_a_large_drift(monkeypatch):
    def wild_scorer(article, *, prompt, models=None):
        return _completion(relevance=0.0 if article else 0.0, polarity=-1.0)

    result = check_known_scoring(wild_scorer, prompt=object())
    assert not result.passed


def test_check_known_scoring_reports_a_call_failure_without_raising():
    def broken_scorer(article, *, prompt, models=None):
        raise AdapterError("no credential")

    result = check_known_scoring(broken_scorer, prompt=object())
    assert not result.passed
    assert "call failed" in result.detail


# --- load_news_polarity_frame -------------------------------------------------


def test_load_news_polarity_frame_joins_known_at_utc(tmp_path):
    day = dt.date(2026, 8, 20)
    _write_news(tmp_path, day, [_article("a1")])
    write_scores(tmp_path, day, "gpt-5.4", "v1", [_score_row("a1")])

    frame = load_news_polarity_frame(tmp_path)
    assert len(frame) == 1
    assert frame.iloc[0]["ticker"] == "005930"
    assert pd.notna(frame.iloc[0]["known_at_utc"])


def test_load_news_polarity_frame_drops_a_score_with_no_matching_article(tmp_path):
    day = dt.date(2026, 8, 20)
    write_scores(tmp_path, day, "gpt-5.4", "v1", [_score_row("orphan")])
    frame = load_news_polarity_frame(tmp_path)
    assert frame.empty


def test_load_news_polarity_frame_dedups_on_article_ticker_model_prompt(tmp_path):
    day = dt.date(2026, 8, 20)
    _write_news(tmp_path, day, [_article("a1")])
    write_scores(tmp_path, day, "gpt-5.4", "v1", [_score_row("a1"), _score_row("a1")])

    frame = load_news_polarity_frame(tmp_path)
    assert len(frame) == 1


def test_load_news_polarity_frame_empty_when_nothing_scored(tmp_path):
    frame = load_news_polarity_frame(tmp_path)
    assert frame.empty
    assert list(frame.columns) == ["article_id", "ticker", "relevance", "polarity", "known_at_utc"]


# --- live, manual verification only ------------------------------------------


@pytest.mark.network
def test_a_real_call_round_trips(tmp_path, monkeypatch):
    """Not run by default. Requires the configured scoring stage's credential.
    Confirms score_article → write_scores → load_news_polarity_frame survives
    a real vendor round trip, unmocked.

    Run explicitly: uv run pytest tests/test_daily_scoring.py -m network -v
    """
    day = dt.date.today()
    _write_news(tmp_path, day, [_article("real1", title="삼성전자, 3분기 영업이익 컨센서스 상회")])
    monkeypatch.setattr("src.llm.daily_scoring.load_aliases", _aliases)
    monkeypatch.setattr("src.llm.daily_scoring.load_watchlist", lambda market=None: _watchlist())

    now = pd.Timestamp.now(tz="UTC")
    frame, report = score_new_articles(tmp_path, now=now, known_value_check=True)

    assert not frame.empty
    assert report.ok, report.summary()

    reread = load_news_polarity_frame(tmp_path)
    assert len(reread) == 1
