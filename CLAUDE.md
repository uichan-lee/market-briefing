# CLAUDE.md

Project instructions for `market-briefing`. Checked into the repository.

Personal preferences (communication style, commit conventions, naming, general engineering hygiene) live in user memory at `~/.claude/CLAUDE.md` and are not repeated here. This file contains only what is specific to this project.

---

## Purpose

Generate a daily market briefing for Korean and US equities. Full specification: @SPEC.md. Evaluation criteria, frozen before data collection: @PREREGISTRATION.md. Work that only Ricky can do: @MANUAL-TASKS.md.

This system **does not execute trades**. It produces a document that a human reads and acts on.

> **`MANUAL-TASKS.md` is written in Korean, deliberately.** User memory defaults repository documents to English but excepts output artifacts written for Ricky to read, and that file is a checklist Ricky follows step by step rather than reference material for an agent. Keep it Korean; do not "correct" it. Everything else — SPEC, README.md, code, comments, commit messages — stays English, with README.ko.md as the maintained Korean translation.

---

## Absolute rules

If an instruction conflicts with one of these, say so and ask before proceeding.

1. Never overwrite or delete anything under `data/raw/`. Re-runs write to a new suffixed path. This directory is the future backtest dataset.
2. Never write code that calls order, execution, or cancellation endpoints of the KIS Open API. Read-only endpoints only.
3. Never have an LLM produce the directional rating or its rationale as free-form text. The briefing **does** state a directional opinion (SPEC §2.2⑥), but it is computed deterministically from the numbers by `src/report/rating.py`. LLM output is limited to three places: the numeric schema in SPEC §6.2, the red-team section in SPEC §2.2⑤, and the commentary in SPEC §2.2⑧. Adding a fourth requires changing this line first.
4. Never import a vendor LLM SDK outside `src/llm/adapter.py`. The pipeline must stay model-agnostic.
5. Never add a delivery channel that is not in `config/delivery.yaml`.

### The commentary exception, in full

SPEC §2.2⑧ lets an LLM write prose about direction. That is permitted only because three properties make it structurally incapable of becoming the rating. All three are load-bearing; removing any one reopens rule 3.

1. **It reads the rendered deterministic sections, never raw articles.** It cannot re-score news, because scoring already happened upstream at §6.2 and only its output reaches the commentary.
2. **It is checked before publication.** `src/report/consistency.py` compares every rating label in the prose against the computed rating for that ticker. On contradiction the section is dropped and the reason goes in the report header — the section is never published in a state that disagrees with §2.2⑥.
3. **Nothing consumes it.** No feature, no score, no shadow portfolio, no PREREGISTRATION metric reads the commentary. It is a leaf of the pipeline. Non-reproducible prose entering the evaluation path would invalidate the evaluation, which is the whole reason rule 3 exists.

The invariant, stated once: **the rating is computed; prose may not originate, alter, or contradict it.**

---

## Determinism first

If a problem can be solved with string matching, embedding similarity, or statistics, solve it that way. LLM calls are reserved for judgments that genuinely require language understanding.

When proposing a change that introduces a new LLM call, first explain why a deterministic approach does not work. If that reason cannot be articulated, the deterministic approach is the correct one.

This is a design constraint, not a cost optimization. Deterministic components are reproducible, which is what makes the evaluation in PREREGISTRATION possible.

---

## Data sources requiring verification

These are sparsely represented in training data, and plausible-looking but wrong endpoint names, parameter names, and response fields are likely:

- KIS Open API (한국투자증권)
- DART OpenAPI
- pykrx
- Naver Search API

Read the official documentation before writing calls against any of them. Apply the `# UNVERIFIED` marker convention from user memory.

---

## Collector rules

Every collector in `src/collectors/` requires validation written **before** the fetching logic:

1. Assert the returned schema — column names and dtypes.
2. Check the missing-value ratio against a declared threshold.
3. Check trading-day continuity, excluding market holidays.
4. Compare at least one known value against a hardcoded expected result — for example, a specific date's closing price for 005930.

A collector without all four does not get merged. The bottleneck in this project is data correctness, not code generation.

---

## Normalization

All features are normalized as a 252-trading-day rolling z-score per ticker. Raw absolute values are never compared across tickers.

---

## Time handling

- Store everything in UTC. Display in KST.
- Determine market sessions with `pandas_market_calendars`. Never hardcode holidays or DST transitions.
- US market close is 05:00 KST during DST and 06:00 KST outside it. Derive this; do not assume either value.

---

## Look-ahead prohibition

- A feature computed at time `t` never uses data timestamped at or after `t`.
- News is joined on publication timestamp, and the earliest tradeable time is derived from it.
- News published during a session is assumed tradeable at the next session's open.

Any function computing features takes an explicit `as_of` parameter. A function that reads "the latest" data without an `as_of` boundary is a bug, not a convenience.

---

## Entity resolution

Ticker matching is driven by `config/aliases.yaml`, which is maintained by hand. Never auto-generate or auto-extend it — a wrong alias corrupts every downstream number silently, while a missing alias only loses coverage.

When a match is ambiguous, drop the article into the `ambiguous` bucket rather than guessing. Report the daily `ambiguous` ratio in the briefing header.

---

## Failure handling

Silent failure is the worst outcome in this project.

- A collector that fails records the failure and lets the pipeline continue.
- Missing data appears in the report header, not only in logs.
- A partial report is published rather than no report.
- A run that produces no report at all sends a failure notice through the configured delivery channels.

**News is the one source where a missed run is unrecoverable.** Prices and macro can be re-fetched years later; RSS holds a rolling buffer with no history, so an hour not collected is permanently absent from the backtest dataset. That is why `kr_news` collects twice an hour through the KRX session and hourly otherwise, why `data/raw/kr/news/` is committed rather than gitignored, and why a long gap between collection runs is a validation *failure* rather than an idle period.

---

## Delivery

Output channels are pluggable adapters under `src/notify/`, configured in `config/delivery.yaml`. Default channels are `vault` (commit to `reports/`) and `email`. Report content is written in Korean.

---

## Toolchain

`uv` manages the environment; `pyproject.toml` holds all configuration. Every command runs through `uv run`:

```
uv sync                        # install
uv run pytest -m "not network" # the default test run
uv run ruff check . && uv run ruff format .
```

Imports resolve from the repository root — `from src.util.session import ...` — via `pythonpath = ["."]` in `pyproject.toml`. This follows the flat `src/` layout in SPEC §10 rather than the usual `src/<package>/` nesting, because this project is an application run by CI and is never pip-installed.

Dependencies are added when the code that needs them is written, each with a stated reason, not up front from SPEC §3's source list.

---

## Testing

- `pytest`. Tests making network calls are marked `@pytest.mark.network` and excluded from the default run.
- The `network` marker is registered in `pyproject.toml` under `--strict-markers`, so a typo'd marker is an error rather than a silently-running network test.
- Fixtures use committed sample payloads under `tests/fixtures/`, not live API calls.
- Run `uv run pytest -m "not network"` before declaring any task done.

---

## Definition of done

1. The code runs.
2. `pytest -m "not network"` passes.
3. Validation functions exist for any new data path.
4. No `UNVERIFIED` marker remains that Ricky has not seen.
5. The diff has been shown.
