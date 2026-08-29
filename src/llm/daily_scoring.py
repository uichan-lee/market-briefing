"""Production news scoring, run from ``scripts/collect_daily.py``. SPEC §6.2/§6.3
archive, feeding the ``news_polarity`` feature (SPEC §2.2③, §5).

Turns resolved (article, ticker) pairs from ``data/raw/kr/news/`` into scored
records under ``data/scores/{date}__{model_id}__{prompt_version}.jsonl`` — the
archive SPEC §3.3 already names, previously unbuilt. A re-run reads this
archive instead of re-scoring: every LLM call costs real money, so idempotency
is enforced *before* any call is made, not by deduplicating the output after.

**Both scheduled runs call the same driver, unconditionally** — like
``kr_news``/``us_filings``/``calendar`` in ``scripts/collect_daily.py``, there
is no KRX dependency here, and a quiet run (nothing new to score) costs
nothing beyond the fixed golden-set check below.

**Failure is per-article, never per-run.** One bad response must not lose the
rest of a batch, the same discipline every collector in this repository
already holds. A credential problem is the one exception — if the configured
provider has no key, nothing is attempted and the check reports why, rather
than than trying and failing once per candidate.

**The golden-set check (``check_known_scoring``) is this pipeline's only real
"known value" for an LLM call** — there is no hardcoded numeric fact for a
model's opinion the way there is for a stock price, so the closest available
ground truth is Ricky's own label on a fixed article, checked every run.

This module does not touch ``config/rating.yaml``. It computes and archives
``news_polarity``; whether and how it enters the composite rating is a
separate, later, distributionally-informed decision (see the module's
call site in ``src/features/compute.py`` and the comment in
``config/rating.yaml``'s ``deferred_weights``).
"""

from __future__ import annotations

import datetime as dt
import glob
import gzip
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from src.collectors.validate import CheckResult, ValidationReport, check_missing_ratio, check_schema
from src.entity.resolve import resolve
from src.eval.bakeoff import MAX_RATE_LIMIT_RETRIES, Pacer, examples
from src.llm.adapter import AdapterError, SchemaError, is_rate_limit, missing_credential
from src.llm.score import Prompt, load_prompt, out_of_range, score_article
from src.util.config import load_aliases, load_models, load_watchlist
from src.util.session import now_utc, to_utc

ROOT = Path(__file__).resolve().parents[2]

# Candidate + idempotency lookback, in calendar days. Wide enough that a few
# missed collect_daily runs cost nothing (the next run just finds a larger
# backlog), matched by CONTINUITY_MAX_AGE_HOURS below so a pair ages into a
# reported failure well before it could fall outside this window unscored.
SCORE_WINDOW_DAYS = 4

# A resolved-but-unscored pair older than this fails check_scoring_continuity.
# ~2.5 scheduled runs of slack — the same "a check fires when loss is shown"
# principle kr_news.check_feed_continuity uses, sized so the failure fires
# well inside SCORE_WINDOW_DAYS rather than at its edge.
CONTINUITY_MAX_AGE_HOURS = 30.0
CHECKPOINT_SIZE = 25

# Which golden-set example check_known_scoring re-scores every run. Fixed
# rather than random, so a drift is comparable run to run.
KNOWN_SCORING_INDEX = 0
KNOWN_SCORING_TOLERANCE = (0.3, 0.4)  # (relevance, polarity) — generous on purpose

SCORE_SCHEMA = {
    "article_id": "object",
    "ticker": "object",
    "model_id": "object",
    "prompt_version": "object",
    "relevance": "float64",
    "polarity": "float64",
    "intensity": "float64",
    "uncertainty": "float64",
    "forwardness": "float64",
    "rationale": "object",
    # NaN when the stage sent no temperature at all (SPEC §6.3's "record what
    # was actually sent", not what SPEC §6.3 originally asked for).
    "temperature": "float64",
}

# `temperature` deliberately absent — a fully-missing temperature is the
# expected case for `claude-sonnet-5`/similar, and check_missing_ratio only
# checks columns present in this dict.
MISSING_THRESHOLDS = {
    "article_id": 0.0,
    "ticker": 0.0,
    "model_id": 0.0,
    "prompt_version": 0.0,
    "relevance": 0.0,
    "polarity": 0.0,
    "intensity": 0.0,
    "uncertainty": 0.0,
    "forwardness": 0.0,
    "rationale": 0.0,
}


class _RateLimitExhausted(Exception):
    """Backoff ran out on a 429. Carries the vendor's last words."""


def score_path(root: Path, day: dt.date, model_id: str, prompt_version: str) -> Path:
    """SPEC §3.3's exact filename: ``{date}__{model_id}__{prompt_version}.jsonl``."""
    return root / "scores" / f"{day.isoformat()}__{model_id}__{prompt_version}.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def already_scored_pairs(
    root: Path,
    *,
    model_id: str,
    prompt_version: str,
    now: pd.Timestamp | None = None,
    lookback_days: int = SCORE_WINDOW_DAYS,
) -> set[tuple[str, str]]:
    """(article_id, ticker) pairs already archived under this model/prompt,
    over the trailing ``lookback_days`` — checked before any call is made."""
    now = now if now is not None else now_utc()
    pairs: set[tuple[str, str]] = set()
    for offset in range(lookback_days + 1):
        day = (now - pd.Timedelta(days=offset)).date()
        for row in _read_jsonl(score_path(root, day, model_id, prompt_version)):
            pairs.add((str(row["article_id"]), str(row["ticker"])))
    return pairs


def resolved_candidates(
    root: Path, *, window_days: int = SCORE_WINDOW_DAYS, now: pd.Timestamp | None = None
) -> list[dict]:
    """Every (article, ticker) pair resolved from ``data/raw/kr/news/`` inside
    the trailing ``window_days``, in the exact row shape ``score_article``/
    ``render_user`` read, plus ``collected_at_utc``/``known_at_utc`` for the
    continuity check and the feature join. No sampling — production scores
    everything it can, unlike ``src.eval.bakeoff.live_examples``'s stratified
    sample.
    """
    now = now if now is not None else now_utc()
    window_start = (now - pd.Timedelta(days=window_days)).date()

    articles: dict[str, dict] = {}
    news_dir = root / "raw" / "kr" / "news"
    for path in sorted(glob.glob(str(news_dir / "*" / "*.jsonl.gz"))):
        day = dt.date.fromisoformat(Path(path).parent.name)
        if day < window_start:
            continue
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                articles.setdefault(row["article_id"], row)

    if not articles:
        return []

    matches, _ = resolve(articles.values(), load_aliases())
    if matches.empty:
        return []

    names = {e.ticker: e.name for e in load_watchlist(market="KR")}
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for m in matches.itertuples():
        key = (str(m.article_id), str(m.ticker))
        if key in seen:
            continue
        seen.add(key)
        article = articles[m.article_id]
        rows.append(
            {
                "article_id": key[0],
                "ticker": key[1],
                "name": names.get(key[1], ""),
                "title": article.get("title", ""),
                "description": article.get("description", ""),
                "collected_at_utc": article.get("collected_at_utc"),
                "known_at_utc": article.get("known_at_utc"),
            }
        )
    return rows


def write_scores(
    root: Path, day: dt.date, model_id: str, prompt_version: str, records: list[dict]
) -> Path:
    """Append-only. Never truncates; creates ``data/scores/`` on first write."""
    path = score_path(root, day, model_id, prompt_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


def load_news_polarity_frame(
    root: Path,
    *,
    model_id: str | None = None,
    prompt_version: str | None = None,
) -> pd.DataFrame:
    """Every archived score joined against its article's ``known_at_utc``.

    This is ``compute.py``'s ``load_raw`` equivalent for ``news_polarity``: it
    lives here rather than in ``src.features.compute`` because it needs
    ``data/raw/kr/news/``'s storage format, which that module otherwise never
    touches. An archived score whose article cannot be found in the news
    archive is dropped, not a crash — SPEC's stated failure discipline
    applies here too.
    """
    columns = [
        "article_id",
        "ticker",
        "model_id",
        "prompt_version",
        "relevance",
        "polarity",
        "intensity",
        "uncertainty",
        "known_at_utc",
        "title",
        "link",
    ]
    score_files = sorted(glob.glob(str(root / "scores" / "*.jsonl")))
    if not score_files:
        return pd.DataFrame(columns=columns)

    records: list[dict] = []
    for path in score_files:
        records.extend(_read_jsonl(Path(path)))
    if not records:
        return pd.DataFrame(columns=columns)

    scores = pd.DataFrame(records)
    if model_id is not None:
        scores = scores[scores["model_id"] == model_id]
    if prompt_version is not None:
        scores = scores[scores["prompt_version"] == prompt_version]
    if scores.empty:
        return pd.DataFrame(columns=columns)
    scores = scores.drop_duplicates(
        subset=["article_id", "ticker", "model_id", "prompt_version"], keep="last"
    )

    articles: dict[str, dict[str, Any]] = {}
    news_dir = root / "raw" / "kr" / "news"
    for path in sorted(glob.glob(str(news_dir / "*" / "*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                articles.setdefault(row["article_id"], row)

    scores["known_at_utc"] = scores["article_id"].map(
        lambda article_id: articles.get(article_id, {}).get("known_at_utc")
    )
    scores["title"] = scores["article_id"].map(
        lambda article_id: articles.get(article_id, {}).get("title", "")
    )
    scores["link"] = scores["article_id"].map(
        lambda article_id: articles.get(article_id, {}).get("link", "")
    )
    scores = scores.dropna(subset=["known_at_utc"]).copy()
    scores["known_at_utc"] = pd.to_datetime(scores["known_at_utc"], utc=True)
    scores["relevance"] = pd.to_numeric(scores["relevance"], errors="coerce")
    scores["polarity"] = pd.to_numeric(scores["polarity"], errors="coerce")
    scores["intensity"] = pd.to_numeric(scores["intensity"], errors="coerce")
    scores["uncertainty"] = pd.to_numeric(scores["uncertainty"], errors="coerce")
    return scores[columns].reset_index(drop=True)


def check_scoring_continuity(
    outstanding: list[dict], *, now: pd.Timestamp, max_age_hours: float = CONTINUITY_MAX_AGE_HOURS
) -> CheckResult:
    """Fails when a resolved-but-still-unscored pair is older than
    ``max_age_hours`` — the loss is measured, not merely absent, matching
    ``kr_news.check_feed_continuity``'s "fire when loss is shown" shape."""
    if not outstanding:
        return CheckResult("scoring_continuity", True, "no outstanding candidates")

    boundary = now - pd.Timedelta(hours=max_age_hours)
    stale = [c for c in outstanding if to_utc(c["collected_at_utc"]) < boundary]
    if stale:
        shown = ", ".join(f"{c['article_id']}/{c['ticker']}" for c in stale[:5])
        suffix = f" (+{len(stale) - 5} more)" if len(stale) > 5 else ""
        return CheckResult(
            "scoring_continuity",
            False,
            f"{len(stale)} pair(s) unscored past {max_age_hours:.0f}h: {shown}{suffix}",
        )
    return CheckResult(
        "scoring_continuity",
        True,
        f"{len(outstanding)} outstanding, all within {max_age_hours:.0f}h",
    )


def check_known_scoring(
    scorer,
    prompt: Prompt,
    *,
    models: dict[str, Any] | None = None,
    tolerance: tuple[float, float] = KNOWN_SCORING_TOLERANCE,
) -> CheckResult:
    """Re-scores one fixed golden-set article every run and compares
    ``relevance``/``polarity`` against Ricky's label. The closest thing this
    pipeline has to a hardcoded "known value" for an LLM call."""
    fixed = examples()[KNOWN_SCORING_INDEX]
    label = fixed["label"]
    try:
        result = scorer(fixed, prompt=prompt, models=models)
    except Exception as exc:  # noqa: BLE001 — reported, not raised; matches every other check here
        return CheckResult("known_value", False, f"golden-set check call failed: {exc}")

    bad = out_of_range(result.parsed)
    if bad:
        return CheckResult("known_value", False, f"golden-set check out of range: {bad}")

    rel_tol, pol_tol = tolerance
    rel_delta = abs(float(result.parsed["relevance"]) - float(label["relevance"]))
    pol_delta = abs(float(result.parsed["polarity"]) - float(label["polarity"]))
    if rel_delta > rel_tol or pol_delta > pol_tol:
        return CheckResult(
            "known_value",
            False,
            f"golden-set check drifted: relevance Δ{rel_delta:.2f} (±{rel_tol}), "
            f"polarity Δ{pol_delta:.2f} (±{pol_tol})",
        )
    return CheckResult(
        "known_value",
        True,
        f"golden-set check within tolerance (relevance Δ{rel_delta:.2f}, "
        f"polarity Δ{pol_delta:.2f})",
    )


def _score_with_backoff(
    scorer, article: dict, *, prompt: Prompt, provider: str, models, pacer: Pacer
):
    """One scored call, paced, retrying a 429 with backoff. Mirrors
    ``src.eval.bakeoff._call_with_backoff``'s shape for a single configured
    model rather than a list of bake-off candidates — not imported directly
    since that function is private and coupled to bakeoff's ``Attempt``."""
    for attempt_no in range(MAX_RATE_LIMIT_RETRIES + 1):
        pacer.wait(provider)
        try:
            return scorer(article, prompt=prompt, models=models)
        except Exception as exc:  # noqa: BLE001 — re-raised unless it is a 429
            if not is_rate_limit(exc):
                raise
            if attempt_no == MAX_RATE_LIMIT_RETRIES:
                raise _RateLimitExhausted(f"{type(exc).__name__}: {exc}") from exc
            pacer.backoff(provider, attempt_no)
    raise AssertionError("unreachable")


def score_new_articles(
    root: Path,
    *,
    now: pd.Timestamp | None = None,
    prompt_version: str = "v1",
    models: dict[str, Any] | None = None,
    scorer=score_article,
    pacer: Pacer | None = None,
    known_value_check: bool = True,
    checkpoint_size: int = CHECKPOINT_SIZE,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Score every resolved-and-unscored (article, ticker) pair once, archive
    the successes, and report on it. Never raises on a per-article failure —
    only a missing credential stops the run before it starts, since nothing
    can be attempted without one.
    """
    if checkpoint_size < 1:
        raise ValueError("checkpoint_size must be positive")

    now = now if now is not None else now_utc()
    config = models if models is not None else load_models()
    report = ValidationReport(collector="news_scores")

    stage = config.get("scoring")
    if not stage:
        report.add(CheckResult("schema", False, "config/models.yaml has no 'scoring' stage"))
        return pd.DataFrame(), report

    model_id = stage["model"]
    provider = stage["provider"]

    absent = missing_credential(provider)
    if absent:
        report.add(
            CheckResult(
                "known_value",
                False,
                f"{provider} needs {absent} in the environment — scoring skipped this run",
            )
        )
        return pd.DataFrame(), report

    prompt = load_prompt(prompt_version)
    pacer = pacer if pacer is not None else Pacer()

    candidates = resolved_candidates(root, now=now)
    scored = already_scored_pairs(root, model_id=model_id, prompt_version=prompt_version, now=now)
    todo = [c for c in candidates if (c["article_id"], c["ticker"]) not in scored]

    written: list[dict] = []
    checkpoint: list[dict] = []
    outstanding: list[dict] = []
    exhausted = ""
    for candidate in todo:
        if exhausted:
            outstanding.append(candidate)
            continue
        try:
            result = _score_with_backoff(
                scorer, candidate, prompt=prompt, provider=provider, models=config, pacer=pacer
            )
        except _RateLimitExhausted as exc:
            exhausted = str(exc)
            outstanding.append(candidate)
            continue
        except (SchemaError, AdapterError):
            outstanding.append(candidate)
            continue
        except Exception:  # noqa: BLE001 — one article's failure must not stop the rest
            outstanding.append(candidate)
            continue

        bad = out_of_range(result.parsed)
        if bad:
            outstanding.append(candidate)
            continue

        record = {
            "article_id": candidate["article_id"],
            "ticker": candidate["ticker"],
            "model_id": result.model_id,
            "prompt_version": result.prompt_version,
            "relevance": float(result.parsed["relevance"]),
            "polarity": float(result.parsed["polarity"]),
            "intensity": float(result.parsed["intensity"]),
            "uncertainty": float(result.parsed["uncertainty"]),
            "forwardness": float(result.parsed["forwardness"]),
            "rationale": str(result.parsed.get("rationale", "")),
            "temperature": stage.get("temperature"),
        }
        written.append(record)
        checkpoint.append(record)
        if len(checkpoint) >= checkpoint_size:
            write_scores(root, now.date(), model_id, prompt_version, checkpoint)
            checkpoint.clear()

    if checkpoint:
        write_scores(root, now.date(), model_id, prompt_version, checkpoint)

    frame = pd.DataFrame(written)
    if not frame.empty:
        frame["temperature"] = pd.to_numeric(frame["temperature"], errors="coerce")
        report.add(check_schema(frame, SCORE_SCHEMA))
        report.add(check_missing_ratio(frame, MISSING_THRESHOLDS))
    else:
        # A quiet run (nothing new resolved-and-unscored) is not a failure —
        # the same stance kr_news.main() takes on an empty poll.
        report.add(CheckResult("schema", True, "nothing scored this run"))
        report.add(CheckResult("missing_ratio", True, "nothing scored this run"))

    report.add(check_scoring_continuity(outstanding, now=now))

    if known_value_check:
        report.add(check_known_scoring(scorer, prompt, models=config))

    return frame, report
