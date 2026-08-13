"""The model bake-off. SPEC §7.4.

Runs each candidate model over the 100 hand-labelled golden-set examples and
measures the six things §7.4 names. **It produces a comparison table; it does
not choose.** MANUAL-TASKS §5 makes the choice Ricky's, and the decision rule is
his too: among models that pass golden-set correlation and self-consistency,
adopt the lowest cost per valid signal.

``run`` and ``report`` are separate commands on purpose. A run is 1,500 calls
and money; re-rendering the table from stored attempts is neither. Every call's
raw result is appended to ``data/bakeoff/attempts.jsonl`` so the analysis can be
redone — including differently — without paying again.

**Three rules for reading the output, fixed before the first run.**

1. A ``forwardness`` difference below **0.13** is not evidence. That is the
   golden set's own noise floor, measured 2026-08-10 and recorded in
   PREREGISTRATION §8.3; the other four sit at 0.03–0.07. The table prints each
   floor beside its column so the comparison cannot be read without it.
2. **A narrow spread is the expected result, not a failed run.** Live
   multi-model trading benchmarks find architecture dominates model choice
   (RESEARCH.md §3.4). When candidates cluster, §7.4's decision rule already
   says what to do — take the cheaper one — and re-running until something
   separates is the defect PREREGISTRATION exists to prevent.
3. **The report discloses that Claude helped word two dimension definitions**
   (PREREGISTRATION §R, 2026-08-08). A bake-off that ranks Claude against a
   schema Claude helped write has to say so.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import time
from collections.abc import Collection
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.golden import (
    DIMENSIONS,
    LABELS,
    REVIEW,
    TRIAGE,
    key_of,
    read_jsonl,
    select_for_labelling,
)
from src.llm.adapter import AdapterError, SchemaError, is_rate_limit
from src.llm.score import load_prompt, out_of_range, score_article
from src.util.config import load_models

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "data" / "bakeoff"
ATTEMPTS = RESULTS / "attempts.jsonl"

# SPEC §7.4 self-consistency is measured across repeated runs of the same
# article. Five is what §9.1 prices the bake-off at (100 × 3 × 5 = 1,500 calls).
REPEATS = 5

# SPEC §7.4 passing bars.
BARS = {"relevance": 0.7, "polarity": 0.6}
MAX_POLARITY_SIGMA = 0.1
MIN_SCHEMA_COMPLIANCE = 0.99

# PREREGISTRATION §8.3, **re-measured 2026-08-13**. A per-dimension difference
# smaller than this is inside the golden set's disagreement with itself.
#
# All five values were replaced at once, superseding the 2026-08-10 set. Two of
# those (relevance, forwardness) had already been withdrawn to ``None`` by §R;
# the other three were never withdrawn but came from the same superseded recheck,
# so keeping them while replacing only the withdrawn pair would have left the row
# a mixture of two measurements against two different label sets. The 2026-08-13
# `golden recheck --fresh` answers all five against the corrected labels, in the
# right order (first pass 2026-08-12, second 2026-08-13 — `verify` raised no
# staleness warning), so it replaces the row entire.
#
# Three of the five got **worse**, which is the point of re-measuring rather than
# assuming: polarity 0.07 → 0.095, intensity 0.07 → 0.140, forwardness 0.13 →
# 0.205. Only relevance improved (0.07 → 0.040), and that one is flattered by its
# gap: `--redo-all` rewrote relevance on 2026-08-12, one day before the recheck,
# while the other four dimensions' first-pass answers date from 2026-08-07/08.
# MANUAL-TASKS §4 is explicit that a short gap measures memory rather than
# standard, so 0.040 should be read as a lower bound, not a tight floor.
#
# **The set failed `verify` at this measurement** — mean per-article worst-
# dimension gap 0.30 against the 0.25 threshold, up from 0.16 — and `forwardness`
# alone caused it: excluding that one dimension the same statistic is 0.195, a
# pass. The floors below are still the honest measure of how far the standard
# disagrees with itself, and a failing set makes them more necessary rather than
# less; what the failure forbids is reading any `forwardness` ranking at all. See
# §R, 2026-08-13, for why the schema was not revised to fix it.
#
# `scripts/golden.py verify` prints the per-dimension gaps that measurement
# produces; nothing here reads them, so the values below are updated by hand and
# only against a §R row.
NOISE_FLOOR: dict[str, float | None] = {
    "uncertainty": 0.070,
    "relevance": 0.040,
    "polarity": 0.095,
    "intensity": 0.140,
    "forwardness": 0.205,
}

# SPEC §7.4: cost per valid signal counts articles the model itself called
# relevant, not articles it was handed.
VALID_SIGNAL_RELEVANCE = 0.5

# Calls per minute to hold each provider to. Only providers that need pacing
# appear; anything absent runs unthrottled.
#
# 2026-08-11's 8 was a trial-and-error guess from hitting the API directly —
# it was wrong. Google AI Studio's dashboard (aistudio.google.com/rate-limit,
# Market Briefing project) states the free tier's actual quota for
# `gemini-3.5-flash` outright: **5 RPM, 20 RPD**. Its own 28-day peak-usage
# view shows prior runs at 9/5 RPM and 31/20 RPD — both limits breached, which
# is why runs stalled past what any backoff could clear; that stall had no
# name until this screenshot (2026-08-12). Enabling billing raises the real
# limit and this number can go with it; it is not a property of the model.
RATE_LIMITS = {"gemini": 5}

# The free tier's daily cap, from the same dashboard. Not enforced by the
# Pacer — a day's remaining quota cannot be waited out, only stopped short of.
# `bakeoff run --model gemini-3.5-flash --limit N` with N growing by this much
# each day is how a >20-call target gets collected without burning hours on
# backoff against a wall that will not open until midnight Pacific.
RATE_LIMIT_DAILY = {"gemini": 20}

# A 429 is retried with exponential backoff. Past this many attempts the run
# stops asking that candidate at all.
#
# The point is to tell a per-minute limit from a per-day one without the
# operator watching. A per-minute quota is gone after one backoff; a daily cap
# survives every backoff, and continuing would spend an hour to collect 500
# identical failures. Hitting the ceiling is therefore reported as the daily-cap
# diagnosis it almost certainly is.
MAX_RATE_LIMIT_RETRIES = 4
BACKOFF_SECONDS = 15.0


class Pacer:
    """Holds calls to a provider's rate, sleeping only when one is due.

    ``sleep`` and ``clock`` are injected so the tests can drive this without
    spending a real minute per assertion.
    """

    def __init__(
        self,
        limits: dict[str, int] | None = None,
        *,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.limits = RATE_LIMITS if limits is None else limits
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def wait(self, provider: str) -> None:
        """Block until another call to ``provider`` is within its rate."""
        rate = self.limits.get(provider)
        if not rate:
            return
        interval = 60.0 / rate
        previous = self._last.get(provider)
        now = self._clock()
        if previous is not None:
            due = previous + interval - now
            if due > 0:
                self._sleep(due)
                now = self._clock()
        self._last[provider] = now

    def backoff(self, provider: str, attempt: int) -> None:
        """Wait out a 429, longer each time, then let the caller retry."""
        self._sleep(BACKOFF_SECONDS * (2**attempt))
        self._last.pop(provider, None)


@dataclass
class Attempt:
    """One call: what was asked, what came back, and what it cost."""

    article_id: str
    ticker: str
    model: str
    provider: str
    repeat: int
    prompt_version: str
    scores: dict[str, float] = field(default_factory=dict)
    rationale: str = ""
    failure: str = ""
    # True when the call failed *before the model answered* — quota, credit,
    # outage, a rejected parameter. Kept apart from a schema failure because
    # SPEC §7.4's compliance rate is a measure of the model's structured-output
    # maturity, and a 429 says nothing about that. Gemini's free tier is 5
    # RPM / 20 RPD (AI Studio dashboard, confirmed 2026-08-12), so a 500-call
    # run against it would report the model at ~4% compliance when the model
    # never spoke.
    transport: bool = False
    # The temperature actually sent, or None when the candidate was called
    # without one. Recorded per call rather than assumed from SPEC §6.3,
    # because as of 2026-08-11 the frontier models disagree about whether the
    # parameter exists at all — see `candidate_settings`.
    temperature: float | None = None
    # What the model actually returned, kept only when the call failed. The
    # 2026-08-11 run recorded 13 out-of-range failures and discarded the values
    # that caused them, so diagnosing it needed a fresh paid call to reproduce
    # what was already on disk once. A failure nobody can read is a failure
    # that gets re-bought.
    raw: dict[str, Any] = field(default_factory=dict)
    # Which invocation of `run` produced this call, stamped by `store`. Carried
    # on the record because `attempts.jsonl` is append-only and a `--limit 3`
    # dry run would otherwise be averaged into the real table forever.
    run_at: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failure

    @property
    def answered(self) -> bool:
        """The model returned something — well-formed or not."""
        return not self.transport


def examples() -> list[dict[str, Any]]:
    """The 100 labelled examples, each carrying its article text.

    ``v1.jsonl`` holds only scores — the text lives in the triage file. This is
    the join ``scripts.golden.verify`` uses, reused rather than re-derived.
    """
    context = {key_of(row): row for row in select_for_labelling(read_jsonl(TRIAGE))[0]}
    joined = []
    for label in read_jsonl(LABELS):
        article = context.get(key_of(label))
        if article is None:
            raise RuntimeError(f"no article text for {key_of(label)}; triage and labels disagree")
        joined.append({**article, "label": label})
    return joined


def flagged_keys() -> set[tuple[str, str]]:
    """Examples a rule sent back for a second look (`review.jsonl`).

    `review_influence` in scripts/golden.py asks the bake-off to check whether
    the ranking holds on the unflagged subset too — 8% of the set had its bucket
    changed after flagging, and a ranking that depends on those is a ranking
    that depends on the flagging rules.
    """
    return {key_of(row) for row in read_jsonl(REVIEW) if row.get("changed")}


class _RateLimitExhausted(Exception):
    """Backoff ran out on a 429. Carries the vendor's last words."""

    def __init__(self, last: str) -> None:
        super().__init__(last)
        self.last = last


def _call_with_backoff(scorer, article, *, prompt, candidate, models, pacer: Pacer):
    """One scored call, paced, retrying a 429 until the budget is spent."""
    provider = candidate["provider"]
    for attempt_no in range(MAX_RATE_LIMIT_RETRIES + 1):
        pacer.wait(provider)
        try:
            return scorer(
                article,
                prompt=prompt,
                model=candidate["model"],
                provider=provider,
                models=models,
            )
        except Exception as exc:  # noqa: BLE001 — re-raised unless it is a 429
            if not is_rate_limit(exc):
                raise
            if attempt_no == MAX_RATE_LIMIT_RETRIES:
                raise _RateLimitExhausted(f"{type(exc).__name__}: {exc}") from exc
            pacer.backoff(provider, attempt_no)
    raise AssertionError("unreachable")


def candidate_settings(candidate: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    """The ``scoring`` stage settings as this candidate needs them sent.

    `config/models.yaml` says temperature 0 for the scoring stage, which is what
    SPEC §6.3 asked for outright until 2026-08-11. It is no longer something
    every model accepts. Measured that day against the live APIs:

    * ``gpt-5.1`` takes 0.
    * ``claude-sonnet-5`` refuses it — Anthropic answers HTTP 400 with
      ``temperature is deprecated for this model``. Omitted or 1 are the options.
    * ``gemini-3.5-flash`` takes 0, but litellm warns Gemini 3+ has the
      parameter slated for removal.

    So a candidate may carry its own ``temperature``, and ``temperature: null``
    means send none at all. Whatever results is written onto every Attempt, so
    the report states the temperature each model was actually scored at instead
    of implying they shared one. Dropping the parameter silently — which
    ``litellm.drop_params`` would do — is the thing this exists to prevent.

    SPEC §6.3 was amended the same day to ask for the most deterministic setting
    each vendor still offers, rather than for a number none of them share, and
    PREREGISTRATION §8.3 makes §7.4's self-consistency σ the instrument that
    replaces the lost guarantee.
    """
    settings = {**stage, **{k: v for k, v in candidate.items() if k not in ("provider", "model")}}
    if settings.get("temperature") is None:
        settings.pop("temperature", None)
    return settings


def run(
    candidates: list[dict[str, Any]],
    *,
    repeats: int = REPEATS,
    limit: int | None = None,
    prompt_version: str = "v1",
    scorer=score_article,
    pacer: Pacer | None = None,
    stage: dict[str, Any] | None = None,
    sink=None,
    done: Collection[tuple[str, str, str, int, str]] = (),
) -> list[Attempt]:
    """Score every example with every candidate, ``repeats`` times each.

    Failures are recorded rather than raised: SPEC §7.4 measures the share of
    calls that comply, so a model that returns garbage on 3% of articles has to
    reach the table as 97% rather than as a crash.

    Calls are paced per provider and a 429 is retried with backoff, because
    Google's free tier serves 5 calls a minute (and 20 a day, which no backoff
    clears) and an unpaced run would collect 490 quota errors instead of a
    comparison. When the backoffs are exhausted the candidate is abandoned
    rather than retried for an hour — see ``MAX_RATE_LIMIT_RETRIES``.

    ``sink`` is called with each Attempt the moment it is finished, and is how
    a run survives its own interruption. Pacing Google at 5 calls/min makes a
    full run over an hour long, and the money is spent per call rather than at
    the end: writing only after the last one means a laptop sleeping at call
    1,400 of 1,500 discards every one of them. The module docstring's promise
    that the analysis can be redone "without paying again" is only true if the
    calls reach disk as they happen.

    ``done`` holds :func:`answered_keys` from an interrupted run, which are
    skipped. Only calls that *reached the model* belong in it — a transport
    failure is retried, because that is what an abandoned candidate leaves
    behind, while a schema failure is kept, because re-rolling it would quietly
    improve the compliance rate SPEC §7.4 measures.
    """
    prompt = load_prompt(prompt_version)
    rows = examples()[:limit]
    attempts: list[Attempt] = []
    pacer = pacer if pacer is not None else Pacer()
    stage = stage if stage is not None else load_models()["scoring"]
    done = set(done)

    def record(attempt: Attempt) -> None:
        attempts.append(attempt)
        if sink is not None:
            sink(attempt)

    for candidate in candidates:
        settings = candidate_settings(candidate, stage)
        models = {
            "scoring": {**settings, "provider": candidate["provider"], "model": candidate["model"]}
        }
        temperature = settings.get("temperature")
        exhausted = ""
        for article in rows:
            for repeat in range(repeats):
                attempt = Attempt(
                    article_id=article["article_id"],
                    ticker=article["ticker"],
                    model=candidate["model"],
                    provider=candidate["provider"],
                    repeat=repeat,
                    prompt_version=prompt_version,
                    temperature=temperature,
                )
                if key_of_attempt(attempt) in done:
                    continue
                if exhausted:
                    attempt.failure = exhausted
                    attempt.transport = True
                    record(attempt)
                    continue
                try:
                    result = _call_with_backoff(
                        scorer,
                        article,
                        prompt=prompt,
                        candidate=candidate,
                        models=models,
                        pacer=pacer,
                    )
                except _RateLimitExhausted as exc:
                    # Backoff could not clear it, so this is not the per-minute
                    # quota pacing was built for. Stop asking: 500 more calls
                    # would take an hour to reproduce the same answer.
                    exhausted = (
                        f"rate limit survived {MAX_RATE_LIMIT_RETRIES} backoffs, so pacing "
                        f"cannot clear it — a per-day cap or an unfunded account, not the "
                        f"per-minute limit. Remaining calls skipped. Last: {exc.last}"
                    )
                    attempt.failure = exhausted
                    attempt.transport = True
                    record(attempt)
                    continue
                except SchemaError as exc:
                    attempt.failure = f"schema: {exc}"
                except AdapterError as exc:
                    # A missing key or an unknown provider. The model was never
                    # asked, so this is transport, not non-compliance.
                    attempt.failure = f"adapter: {exc}"
                    attempt.transport = True
                except Exception as exc:  # noqa: BLE001
                    # A vendor's own exception — rejected parameter, rate limit,
                    # outage. Recorded rather than raised for the same reason
                    # the schema failures are: a run is 1,500 calls, and one
                    # candidate refusing a parameter must not destroy the two
                    # that answered. The failure is in the table, not swallowed.
                    attempt.failure = f"{type(exc).__name__}: {exc}"
                    attempt.transport = True
                else:
                    bad = out_of_range(result.parsed)
                    if bad:
                        attempt.failure = f"out of range: {', '.join(bad)}"
                        attempt.raw = result.parsed
                    else:
                        attempt.scores = {
                            name: float(result.parsed[name]) for name, *_ in DIMENSIONS
                        }
                        attempt.rationale = str(result.parsed.get("rationale", ""))
                    attempt.input_tokens = result.input_tokens
                    attempt.output_tokens = result.output_tokens
                    attempt.cost_usd = result.cost_usd
                    attempt.latency_s = result.latency_s
                record(attempt)
    return attempts


def key_of_attempt(attempt: Attempt) -> tuple[str, str, str, int, str]:
    """What identifies one call, for resuming an interrupted run.

    ``prompt_version`` is part of it because SPEC §6.3 makes scores from
    different prompts non-comparable — resuming across a prompt change must
    re-score, not inherit.
    """
    return (
        attempt.model,
        attempt.article_id,
        attempt.ticker,
        attempt.repeat,
        attempt.prompt_version,
    )


def answered_keys(attempts: list[Attempt]) -> set[tuple[str, str, str, int, str]]:
    """Calls a resumed run should not pay for again.

    Only the ones that reached the model. A transport failure is deliberately
    absent, so an abandoned candidate — the whole tail a quota cap leaves behind
    — is retried on resume rather than inherited as 500 permanent failures.
    """
    return {key_of_attempt(a) for a in attempts if a.answered}


def store(attempts: list[Attempt], path: Path = ATTEMPTS, *, run_at: str | None = None) -> Path:
    """Append every call to disk so the analysis never needs the money twice.

    ``run_at`` lets a caller stamp several writes as one run, which is what
    incremental persistence and ``--resume`` both need: a resumed run continues
    into the run it is resuming, so :func:`latest_run` still sees one run per
    model rather than two halves it would have to choose between.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = run_at or dt.datetime.now(dt.UTC).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        for attempt in attempts:
            row = {**asdict(attempt), "run_at": stamped}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


def load(path: Path = ATTEMPTS) -> list[Attempt]:
    rows = read_jsonl(path)
    fields = set(Attempt.__dataclass_fields__)
    return [Attempt(**{k: v for k, v in row.items() if k in fields}) for row in rows]


def latest_run(attempts: list[Attempt]) -> list[Attempt]:
    """Only the most recent invocation's calls, per model.

    ``attempts.jsonl`` is append-only, so it accumulates dry runs and re-runs
    alongside the real one. Averaging them together would let a `--limit 3`
    smoke test move a decision that cost $6 to make.

    Selection is per model rather than one global timestamp on purpose: Google's
    20-a-day free-tier cap spreads its 500 calls over several invocations on
    several days, so running that candidate across many invocations is the
    expected workflow, not an accident.
    Taking the newest run *of each model* keeps that workflow working while
    still dropping superseded attempts.
    """
    newest: dict[str, str] = {}
    for attempt in attempts:
        if attempt.run_at > newest.get(attempt.model, ""):
            newest[attempt.model] = attempt.run_at
    return [a for a in attempts if a.run_at == newest[a.model]]


def spearman(left: list[float], right: list[float]) -> float | None:
    """Rank correlation, or ``None`` when it is undefined.

    Computed as Pearson over average ranks, which *is* Spearman's definition —
    not an approximation of it. Done this way because ``Series.corr(method=
    "spearman")`` delegates to scipy, and scipy is not a dependency of this
    project; ``rank()`` and the default Pearson are pure pandas, which is.

    A model that answers with the same number every time has no ranks to
    correlate. That is a real and interesting outcome — it means the model is
    not discriminating — so it is reported as absent rather than as 0.0, which
    would read as "uncorrelated" and understate the problem.
    """
    if len(left) < 2:
        return None
    a, b = pd.Series(left).rank(), pd.Series(right).rank()
    # Checked before correlating rather than after: a constant series has zero
    # variance, and dividing by it produces the right answer (NaN) via a numpy
    # RuntimeWarning that would otherwise be printed on every such column.
    if a.nunique() < 2 or b.nunique() < 2:
        return None
    value = a.corr(b)
    return None if pd.isna(value) else float(value)


def _first_pass(attempts: list[Attempt], model: str) -> dict[tuple[str, str], dict[str, float]]:
    """One score per example per model — repeat 0, the comparable pass."""
    return {
        (a.article_id, a.ticker): a.scores
        for a in attempts
        if a.model == model and a.repeat == 0 and a.ok
    }


def golden_correlation(
    attempts: list[Attempt],
    model: str,
    labels: dict[tuple[str, str], dict[str, float]],
    *,
    keys: set[tuple[str, str]] | None = None,
) -> dict[str, float | None]:
    """Per-dimension Spearman of the model's scores against Ricky's labels."""
    scored = _first_pass(attempts, model)
    shared = sorted(set(scored) & set(labels) & (keys if keys is not None else set(scored)))
    out: dict[str, float | None] = {}
    for name, *_ in DIMENSIONS:
        out[name] = spearman(
            [scored[k][name] for k in shared], [float(labels[k][name]) for k in shared]
        )
    return out


def self_consistency(attempts: list[Attempt], model: str) -> dict[str, float | None]:
    """Mean per-example standard deviation across the repeated runs."""
    by_example: dict[tuple[str, str], list[dict[str, float]]] = {}
    for attempt in attempts:
        if attempt.model == model and attempt.ok:
            by_example.setdefault((attempt.article_id, attempt.ticker), []).append(attempt.scores)

    out: dict[str, float | None] = {}
    for name, *_ in DIMENSIONS:
        sigmas = [
            statistics.stdev([s[name] for s in runs])
            for runs in by_example.values()
            if len(runs) > 1
        ]
        out[name] = float(statistics.fmean(sigmas)) if sigmas else None
    return out


def schema_compliance(attempts: list[Attempt], model: str) -> float | None:
    """Share of calls the model answered that parsed and stayed in range.

    Transport failures are excluded from *both* halves, not just the numerator.
    SPEC §7.4 uses this number to judge structured-output maturity, and a model
    throttled by a free-tier quota has demonstrated nothing about that. When
    every call was transport the answer is ``None`` — unmeasured — rather than
    0.0, so `passes` reports it as unmeasured instead of failing a model that
    was never heard from.
    """
    calls = [a for a in attempts if a.model == model and a.answered]
    return len([a for a in calls if a.ok]) / len(calls) if calls else None


def transport_failures(attempts: list[Attempt], model: str) -> int:
    """Calls that never reached the model. Reported, never silently dropped."""
    return len([a for a in attempts if a.model == model and not a.answered])


def cost_per_valid_signal(attempts: list[Attempt], model: str) -> float | None:
    """Total spend ÷ articles the model itself scored above the relevance bar."""
    calls = [a for a in attempts if a.model == model]
    if any(a.cost_usd is None for a in calls if a.ok):
        return None
    total = sum(a.cost_usd or 0.0 for a in calls)
    valid = len(
        {
            (a.article_id, a.ticker)
            for a in calls
            if a.ok and a.scores.get("relevance", 0.0) > VALID_SIGNAL_RELEVANCE
        }
    )
    return total / valid if valid else None


def inter_model(attempts: list[Attempt], left: str, right: str) -> dict[str, float | None]:
    """Per-dimension Spearman between two candidates. PREREGISTRATION §8.3."""
    a, b = _first_pass(attempts, left), _first_pass(attempts, right)
    shared = sorted(set(a) & set(b))
    return {
        name: spearman([a[k][name] for k in shared], [b[k][name] for k in shared])
        for name, *_ in DIMENSIONS
    }


def passes(
    correlation: dict[str, float | None],
    consistency: dict[str, float | None],
    compliance: float | None,
) -> tuple[bool, list[str]]:
    """SPEC §7.4's gate, applied before cost is allowed to decide anything."""
    reasons = []
    for name, bar in BARS.items():
        value = correlation.get(name)
        if value is None or value <= bar:
            reasons.append(f"{name} correlation {_cell(value)} ≤ {bar}")
    sigma = consistency.get("polarity")
    if sigma is None or sigma >= MAX_POLARITY_SIGMA:
        reasons.append(f"polarity σ {_cell(sigma, 3)} ≥ {MAX_POLARITY_SIGMA}")
    if compliance is None:
        reasons.append("schema compliance unmeasured — no call reached the model")
    elif compliance <= MIN_SCHEMA_COMPLIANCE:
        reasons.append(f"schema compliance {compliance:.1%} ≤ {MIN_SCHEMA_COMPLIANCE:.0%}")
    return not reasons, reasons


def _cell(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def report(attempts: list[Attempt]) -> str:
    """The comparison table SPEC §7.4 asks for, and nothing that decides."""
    labels = {key_of(row): row for row in read_jsonl(LABELS)}
    models = sorted({a.model for a in attempts})
    flagged = flagged_keys()
    unflagged = {k for k in labels if k not in flagged}

    lines = [
        "# Bake-off — SPEC §7.4",
        "",
        f"Generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M}Z from {len(attempts)} calls "
        f"across {len(models)} model(s).",
        "",
        "> **This table does not choose.** MANUAL-TASKS §5 makes the decision Ricky's: "
        "among models passing golden-set correlation and self-consistency, take the lowest "
        "cost per valid signal. Record the choice and its date in `config/models.yaml`.",
        "",
        "> **Disclosure (PREREGISTRATION §R, 2026-08-08).** Two of the five dimension "
        "definitions — `relevance` and `intensity` — were sharpened with Claude's input while "
        "the labelling was under way. Every one of the 500 values is Ricky's, and no model "
        "supplied or corrected a number. But a bake-off that ranks Claude against a schema "
        "Claude helped word has to say so.",
        "",
        "## Golden-set correlation (Spearman vs Ricky's labels)",
        "",
        "| model | " + " | ".join(name for name, *_ in DIMENSIONS) + " |",
        "|---|" + "---|" * len(DIMENSIONS),
    ]

    correlations = {m: golden_correlation(attempts, m, labels) for m in models}
    for model in models:
        row = correlations[model]
        lines.append(f"| `{model}` | " + " | ".join(_cell(row[n]) for n, *_ in DIMENSIONS) + " |")

    lines += [
        "| **passing bar** | "
        + " | ".join(f"> {BARS[n]}" if n in BARS else "—" for n, *_ in DIMENSIONS)
        + " |",
        "| **noise floor** | "
        + " | ".join(
            "—" if NOISE_FLOOR[n] is None else f"±{NOISE_FLOOR[n]:.2f}" for n, *_ in DIMENSIONS
        )
        + " |",
        "",
        "The noise floor is the golden set's disagreement with itself "
        "(PREREGISTRATION §8.3, re-measured 2026-08-13). **A difference between two models "
        "smaller than the floor for that dimension is not evidence.**",
        "",
        "**All five floors were replaced on 2026-08-13** by a `golden recheck --fresh` run "
        "against the corrected labels, superseding the 2026-08-10 set that `golden label "
        "--redo-all` had invalidated. Three got worse: `polarity` 0.07 → 0.095, `intensity` "
        "0.07 → 0.140, `forwardness` 0.13 → 0.205. Differences that cleared the old floors "
        "may not clear these, so any ranking argued from the earlier numbers has to be "
        "re-read against this row rather than carried forward.",
        "",
        "**`forwardness` cannot be ranked at all.** Its ±0.205 floor is wide enough to "
        "swallow essentially any difference this bake-off can produce, and it is also why "
        "the golden set *failed* `verify` at this measurement (mean per-article worst-"
        "dimension gap 0.30 against a 0.25 threshold; 0.195 — a pass — with `forwardness` "
        "excluded). The schema defect behind it is known and deferred to a v2 set, because "
        "rewording a definition while its finished labels are visible is the contamination "
        "§R has already had to declare once. `relevance`'s 0.040 is a lower bound rather "
        "than a tight floor: it was re-labelled one day before the recheck while the other "
        "four dimensions' first-pass answers are five days older, and a short gap measures "
        "memory rather than standard.",
        "",
        "## Self-consistency (mean σ across repeated runs)",
        "",
        "| model | " + " | ".join(name for name, *_ in DIMENSIONS) + " |",
        "|---|" + "---|" * len(DIMENSIONS),
    ]

    consistencies = {m: self_consistency(attempts, m) for m in models}
    for model in models:
        row = consistencies[model]
        cells = " | ".join(_cell(row[n], 3) for n, *_ in DIMENSIONS)
        lines.append(f"| `{model}` | {cells} |")

    lines += [
        "",
        f"Bar: `polarity` σ < {MAX_POLARITY_SIGMA}.",
        "",
        "## Compliance, cost and latency",
        "",
        "| model | temp | schema compliance | never reached | cost per valid signal | "
        "mean latency | verdict |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]

    for model in models:
        calls = [a for a in attempts if a.model == model]
        answered = [a for a in calls if a.answered]
        compliance = schema_compliance(attempts, model)
        unreached = transport_failures(attempts, model)
        cost = cost_per_valid_signal(attempts, model)
        latency = statistics.fmean([a.latency_s for a in answered]) if answered else None
        temps = {a.temperature for a in calls}
        temp = "none" if temps == {None} else "/".join(_cell(t, 1) for t in sorted(temps, key=str))
        ok, reasons = passes(correlations[model], consistencies[model], compliance)
        verdict = "passes §7.4" if ok else "; ".join(reasons)
        lines.append(
            f"| `{model}` | {temp} | {'—' if compliance is None else f'{compliance:.1%}'} | "
            f"{unreached}/{len(calls)} | "
            f"{'—' if cost is None else f'${cost:.4f}'} | {_cell(latency, 2)}s | {verdict} |"
        )

    lines += [
        "",
        "**`temp`** is the temperature each model was actually sent, and `none` means the "
        "parameter was omitted. SPEC §6.3 asks for the most deterministic setting each vendor "
        "still offers, which as of 2026-08-11 is not the same value for all three: Anthropic "
        "answers HTTP 400 with `temperature is deprecated for this model` for "
        "`claude-sonnet-5`, `gpt-5` accepts only 1, and litellm warns Gemini 3+ has the "
        "parameter slated for removal. **When this column is not identical across rows, the "
        "models were not run under the same conditions**, and the self-consistency table above "
        "is the only remaining evidence about determinism — which is why PREREGISTRATION §8.3 "
        "makes that σ the instrument for `LLM output reproducibility`, and why the 5 repeats "
        "are not the place to save money.",
        "",
        "**`never reached`** counts calls that failed before the model answered — quota, "
        "credit, outage, a rejected parameter. They are excluded from compliance and from "
        "mean latency, because SPEC §7.4 measures the model there and a 429 says nothing "
        "about it. A non-zero count is still a warning about the *run*: a model measured on "
        "a fraction of the corpus is being compared on a different sample from its rivals. "
        "Confirmed on the AI Studio dashboard 2026-08-12 — Gemini's free tier is 5 RPM / "
        "20 RPD, so an unthrottled 500-call run against it lands almost entirely in this "
        "column, and even a paced one needs several days to clear the corpus once.",
    ]

    if len(models) > 1:
        lines += [
            "",
            "## Inter-model agreement (PREREGISTRATION §8.3)",
            "",
            "| pair | " + " | ".join(name for name, *_ in DIMENSIONS) + " |",
            "|---|" + "---|" * len(DIMENSIONS),
        ]
        for i, left in enumerate(models):
            for right in models[i + 1 :]:
                row = inter_model(attempts, left, right)
                lines.append(
                    f"| `{left}` × `{right}` | "
                    + " | ".join(_cell(row[n]) for n, *_ in DIMENSIONS)
                    + " |"
                )
        lines += [
            "",
            "§8.5's gate reads `polarity` here and wants > 0.5. Note this is the golden-set "
            "corpus; the gate also requires the same measurement on the 2026-08-12..26 "
            "window's live articles.",
        ]

    lines += [
        "",
        "## Ranking on the unflagged subset",
        "",
        "`review_influence` in `scripts/golden.py` asks for this: 8% of the set had its "
        "bucket changed after a rule flagged it, and a ranking that only holds on those is a "
        "ranking of the flagging rules.",
        "",
        "| model | " + " | ".join(name for name, *_ in DIMENSIONS) + " |",
        "|---|" + "---|" * len(DIMENSIONS),
    ]
    for model in models:
        row = golden_correlation(attempts, model, labels, keys=unflagged)
        lines.append(f"| `{model}` | " + " | ".join(_cell(row[n]) for n, *_ in DIMENSIONS) + " |")

    lines += [
        "",
        "## How to read a narrow spread",
        "",
        "Candidates clustering is the expected outcome, not a failed run — live multi-model "
        "benchmarks find agent architecture dominates model choice (RESEARCH.md §3.4). "
        "§7.4's rule already covers it: **take the cheaper one.** Re-running until something "
        "separates is exactly the defect PREREGISTRATION exists to prevent.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SPEC §7.4 model bake-off")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="call every candidate over the golden set")
    p_run.add_argument("--repeats", type=int, default=REPEATS)
    p_run.add_argument("--limit", type=int, default=None, help="fewer examples, for a dry run")
    p_run.add_argument(
        "--model",
        action="append",
        default=None,
        metavar="NAME",
        help="restrict the run to these candidates (repeatable). Re-running one model "
        "after a fix is the normal case, and `report` keeps each model's newest run "
        "separately so the others are not disturbed.",
    )
    p_run.add_argument(
        "--resume",
        action="store_true",
        help="continue the newest stored run instead of starting one, skipping calls it "
        "already paid for",
    )

    p_report = sub.add_parser("report", help="render the comparison table from stored attempts")
    p_report.add_argument(
        "--all",
        action="store_true",
        help="include superseded runs; by default each model's newest run is used",
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        from src.util.config import load_models

        candidates = load_models().get("candidates")
        if not candidates:
            raise SystemExit("config/models.yaml has no `candidates:` list to bake off")
        if args.model:
            wanted = set(args.model)
            candidates = [c for c in candidates if c["model"] in wanted]
            unknown = wanted - {c["model"] for c in candidates}
            if unknown:
                raise SystemExit(f"no such candidate(s) in config/models.yaml: {sorted(unknown)}")

        stored = load() if ATTEMPTS.exists() else []
        targets = {c["model"] for c in candidates}
        fresh = dt.datetime.now(dt.UTC).isoformat()
        # One `run_at` per model, not one for the invocation. `latest_run`
        # groups by model, and each candidate may be resuming a different run —
        # `--model` exists precisely so they can be run separately. A single
        # timestamp taken across all of them would stamp one model's resumed
        # calls with another model's run and split the first in two.
        run_ats = dict.fromkeys(targets, fresh)
        done: set[tuple[str, str, str, int, str]] = set()
        if args.resume:
            mine = [a for a in latest_run(stored) if a.model in targets] if stored else []
            if not mine:
                raise SystemExit(f"nothing to resume for {sorted(targets)} in {ATTEMPTS}")
            for model in targets:
                previous = [a.run_at for a in mine if a.model == model]
                if previous:
                    run_ats[model] = max(previous)
            done = answered_keys(mine)
            print(f"resuming {len(run_ats)} model(s) — {len(done)} calls already paid for")

        # Written per call rather than once at the end. Pacing Google at 8
        # calls/min makes a full run over an hour long, and a machine that
        # sleeps at call 1,400 must not discard 1,400 paid-for calls.
        def flush(attempt: Attempt) -> None:
            store([attempt], run_at=run_ats[attempt.model])

        try:
            attempts = run(
                candidates,
                repeats=args.repeats,
                limit=args.limit,
                sink=flush,
                done=done,
            )
        except KeyboardInterrupt:
            print(
                f"\ninterrupted — every completed call is in {ATTEMPTS}. "
                f"Continue with:  bakeoff run --resume"
            )
            return 130
        failed = len([a for a in attempts if not a.ok])
        print(f"{len(attempts)} calls, {failed} failed → {ATTEMPTS}")
        return 0

    attempts = load()
    if not attempts:
        raise SystemExit(f"no attempts stored at {ATTEMPTS}; run the bake-off first")
    if not args.all:
        kept = latest_run(attempts)
        if len(kept) != len(attempts):
            print(
                f"# {len(attempts) - len(kept)} call(s) from superseded runs excluded; "
                f"pass --all to include them.\n"
            )
        attempts = kept
    print(report(attempts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
