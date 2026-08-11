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
import statistics
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
from src.llm.adapter import AdapterError, SchemaError
from src.llm.score import load_prompt, out_of_range, score_article

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

# PREREGISTRATION §8.3, measured 2026-08-10. A per-dimension difference smaller
# than this is inside the golden set's disagreement with itself.
NOISE_FLOOR = {
    "uncertainty": 0.03,
    "relevance": 0.07,
    "polarity": 0.07,
    "intensity": 0.07,
    "forwardness": 0.13,
}

# SPEC §7.4: cost per valid signal counts articles the model itself called
# relevant, not articles it was handed.
VALID_SIGNAL_RELEVANCE = 0.5


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
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failure


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


def run(
    candidates: list[dict[str, str]],
    *,
    repeats: int = REPEATS,
    limit: int | None = None,
    prompt_version: str = "v1",
    scorer=score_article,
) -> list[Attempt]:
    """Score every example with every candidate, ``repeats`` times each.

    Failures are recorded rather than raised: SPEC §7.4 measures the share of
    calls that comply, so a model that returns garbage on 3% of articles has to
    reach the table as 97% rather than as a crash.
    """
    prompt = load_prompt(prompt_version)
    rows = examples()[:limit]
    attempts: list[Attempt] = []

    for candidate in candidates:
        for article in rows:
            for repeat in range(repeats):
                attempt = Attempt(
                    article_id=article["article_id"],
                    ticker=article["ticker"],
                    model=candidate["model"],
                    provider=candidate["provider"],
                    repeat=repeat,
                    prompt_version=prompt_version,
                )
                try:
                    result = scorer(
                        article,
                        prompt=prompt,
                        model=candidate["model"],
                        provider=candidate["provider"],
                    )
                except SchemaError as exc:
                    attempt.failure = f"schema: {exc}"
                except AdapterError as exc:
                    attempt.failure = f"adapter: {exc}"
                else:
                    bad = out_of_range(result.parsed)
                    if bad:
                        attempt.failure = f"out of range: {', '.join(bad)}"
                    else:
                        attempt.scores = {
                            name: float(result.parsed[name]) for name, *_ in DIMENSIONS
                        }
                        attempt.rationale = str(result.parsed.get("rationale", ""))
                    attempt.input_tokens = result.input_tokens
                    attempt.output_tokens = result.output_tokens
                    attempt.cost_usd = result.cost_usd
                    attempt.latency_s = result.latency_s
                attempts.append(attempt)
    return attempts


def store(attempts: list[Attempt], path: Path = ATTEMPTS) -> Path:
    """Append every call to disk so the analysis never needs the money twice."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = dt.datetime.now(dt.UTC).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        for attempt in attempts:
            row = {**asdict(attempt), "run_at": stamped}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def load(path: Path = ATTEMPTS) -> list[Attempt]:
    rows = read_jsonl(path)
    fields = set(Attempt.__dataclass_fields__)
    return [Attempt(**{k: v for k, v in row.items() if k in fields}) for row in rows]


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
    calls = [a for a in attempts if a.model == model]
    return len([a for a in calls if a.ok]) / len(calls) if calls else None


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
    if compliance is None or compliance <= MIN_SCHEMA_COMPLIANCE:
        shown = "—" if compliance is None else f"{compliance:.1%}"
        reasons.append(f"schema compliance {shown} ≤ {MIN_SCHEMA_COMPLIANCE:.0%}")
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
        + " | ".join(f"±{NOISE_FLOOR[n]:.2f}" for n, *_ in DIMENSIONS)
        + " |",
        "",
        "The noise floor is the golden set's disagreement with itself "
        "(PREREGISTRATION §8.3, measured 2026-08-10). **A difference between two models "
        "smaller than the floor for that dimension is not evidence** — `forwardness` "
        "especially, at ±0.13.",
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
        "| model | schema compliance | cost per valid signal | mean latency | verdict |",
        "|---|---:|---:|---:|---|",
    ]

    for model in models:
        calls = [a for a in attempts if a.model == model]
        compliance = schema_compliance(attempts, model)
        cost = cost_per_valid_signal(attempts, model)
        latency = statistics.fmean([a.latency_s for a in calls]) if calls else None
        ok, reasons = passes(correlations[model], consistencies[model], compliance)
        verdict = "passes §7.4" if ok else "; ".join(reasons)
        lines.append(
            f"| `{model}` | {'—' if compliance is None else f'{compliance:.1%}'} | "
            f"{'—' if cost is None else f'${cost:.4f}'} | {_cell(latency, 2)}s | {verdict} |"
        )

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

    sub.add_parser("report", help="render the comparison table from stored attempts")

    args = parser.parse_args(argv)

    if args.command == "run":
        from src.util.config import load_models

        candidates = load_models().get("candidates")
        if not candidates:
            raise SystemExit("config/models.yaml has no `candidates:` list to bake off")
        attempts = run(candidates, repeats=args.repeats, limit=args.limit)
        path = store(attempts)
        failed = len([a for a in attempts if not a.ok])
        print(f"{len(attempts)} calls, {failed} failed → {path}")
        return 0

    attempts = load()
    if not attempts:
        raise SystemExit(f"no attempts stored at {ATTEMPTS}; run the bake-off first")
    print(report(attempts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
