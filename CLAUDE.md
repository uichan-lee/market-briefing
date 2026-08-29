# Agent instructions — market-briefing

Project instructions for `market-briefing`. Checked into the repository. `CLAUDE.md` is the canonical file; `AGENTS.md` is a symlink to it, so both Claude Code and Codex read the same text.

Personal preferences (communication style, commit conventions, naming, general engineering hygiene) live in the user-level instructions file — `~/.claude/CLAUDE.md` for Claude Code, `~/.codex/AGENTS.md` for Codex — and are not repeated here. This file contains only what is specific to this project.

---

## Purpose

Generate a daily market briefing for Korean and US equities. Full specification: `SPEC.md` — read it before changing pipeline logic or schemas. Evaluation criteria, frozen before data collection: @PREREGISTRATION.md (eager-loaded here; it is short and defines what the evaluation may not violate). Work that only Ricky can do: `MANUAL-TASKS.md`.

This system **does not execute trades**. It produces a document that a human reads and acts on.

> **`MANUAL-TASKS.md` is written in Korean, deliberately.** User memory defaults repository documents to English but excepts output artifacts written for Ricky to read, and that file is a checklist Ricky follows step by step rather than reference material for an agent. Keep it Korean; do not "correct" it. Everything else — SPEC, README.md, code, comments, commit messages — stays English, with README.ko.md as the maintained Korean translation.

---

## Repository documents

Beyond this file:

- `SPEC.md` — full system specification. Read before changing pipeline logic or schemas.
- `PREREGISTRATION.md` — evaluation criteria, frozen before data collection. Eager-loaded in Purpose.
- `MANUAL-TASKS.md` — Korean, deliberately (see the note above). Steps only Ricky can perform.
- `API-KEYS.md` — how to obtain each credential and which component it unblocks.
- `README.md` / `README.ko.md` — external overview; the Korean file is the maintained translation.
- `notes/` — design-decision history, per-step implementation plans, dated project reviews, and Korean next-step briefs for Ricky. Read the newest `notes/review-YYYY-MM-DD.md` for evidence and the newest `notes/next-steps-YYYY-MM-DD.md` for follow-up status before assuming any stage is live in production.

**The agent maintains this list.** When a repository document or a `notes/` naming convention is added, renamed, moved, or retired, update this section — and any `@import` in Purpose — in the same change. Do not rely on Ricky to remember. If a referenced file is missing, check this list against `git ls-files` for a rename before assuming deletion.

---

## Absolute rules

If an instruction conflicts with one of these, say so and ask before proceeding.

1. Never overwrite or delete anything under `data/raw/`. Re-runs write to a new suffixed path. This directory is the future backtest dataset.
2. Never write code that calls order, execution, or cancellation endpoints of the KIS Open API. Read-only endpoints only.
3. Never have an LLM produce the directional rating or its rationale as free-form text. The briefing **does** state a directional opinion (SPEC §2.2⑥), but it is computed deterministically from the numbers by `src/report/rating.py`. LLM output is limited to three places: the numeric schema in SPEC §6.2, the red-team section in SPEC §2.2⑤, and the commentary in SPEC §2.2⑧. Adding a fourth requires changing this line first.
4. Never import a vendor LLM SDK outside `src/llm/adapter.py`. The pipeline must stay model-agnostic.
5. Never add a delivery channel that is not in `config/delivery.yaml`.

### The commentary and red-team exceptions, in full

SPEC §2.2⑧ lets an LLM write prose about direction. That is permitted only because three properties make it structurally incapable of becoming the rating. All three are load-bearing; removing any one reopens rule 3.

1. **It reads the rendered deterministic sections, never raw articles.** It cannot re-score news, because scoring already happened upstream at §6.2 and only its output reaches the commentary.
2. **It is checked before publication.** `src/report/consistency.py` compares every rating label in the prose against the computed rating for that ticker. On contradiction the section is dropped and the reason goes in the report header — the section is never published in a state that disagrees with §2.2⑥.
3. **Nothing consumes it.** No feature, no score, no shadow portfolio, no PREREGISTRATION metric reads the commentary. It is a leaf of the pipeline. Non-reproducible prose entering the evaluation path would invalidate the evaluation, which is the whole reason rule 3 exists.

**§2.2⑤ (red team, built 2026-08-25) sits under the same rule 3 but earns its place differently: it is never shown §2.2⑥'s ratings at all.** `src/llm/prompts/v1_redteam.md` gives it only §2.2①-④, and forbids the seven rating labels outright rather than reserving them — there is no rating in its input to originate, alter, or contradict, by construction rather than by a downstream check. `src/report/consistency.py`'s check still runs against its output (property 2, reused unmodified) as defense-in-depth, and property 3 holds identically (nothing consumes ⑤ either).

The invariant, stated once: **the rating is computed; prose may not originate, alter, or contradict it.**

> **Current enforcement status, 2026-08-28.** The reproduced bypasses are
> closed by a fail-closed guard and regression tests: an unattributed,
> ambiguous, or ungraded rating claim drops ⑤/⑧; a recognized US ticker clears
> the prior KR subject; and reviewed compounds such as `저가매수` are detected.
> Ordinary flow vocabulary remains excluded. No agent registers or enables
> `ANTHROPIC_API_KEY`; Ricky registers it only after reviewing this change.

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
- **One narrow exception, in `_CALENDAR_CORRECTIONS` in `src/util/session.py`.** The library is behind on two 2026 KRX closures — 지방선거일 and 제헌절, the latter restored after eighteen years. Both were found by diffing the calendar against KRX itself, and a network test re-derives the list so it fails rather than rots. The exception is **removal-only**: it may delete a session the library wrongly reports, never add one it omits, because a spurious session produces a false continuity gap while a missing one would invent data. Extending it requires the same evidence — a diff against the exchange, not a holiday table.
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

**An empty run file under `data/raw/kr/news/` is data, not litter.** A run that polled and found nothing new writes a zero-row `.jsonl.gz` anyway, because "the feeds were polled and held nothing" is a different fact from "the collector did not run" — and the file's existence is the only place that difference is recorded. `last_run_at` reads the run clock off the filename, so deleting these as cleanup silently restores the 2026-08-08 defect where `check_collection_gap` measured time since the last *article* and failed five consecutive runs that had lost nothing.

**The exit code of a collector reports validation, never volume.** `kr_news.main()` returns non-zero when a check fails and zero otherwise, whether or not any article arrived. A quiet hour is not a failure. This is what makes a workflow's `Commit` step `if: always()` load-bearing rather than tidy — without it the alarm skips the commit and destroys the articles it is warning about. Any new collector wired into a workflow inherits both halves of this.

**A check fires when loss is shown, not when evidence is missing.** This line used to read that a feed timing out is itself a failure "because that feed's loss is unmeasured and unrecoverable". The premise was wrong and was corrected on 2026-08-11: the loss is unmeasured *for one run*. The next run where the feed answers compares its buffer against the same stored history and settles what the outage cost, and over the sixty runs to that date every such comparison passed while every failure of `check_feed_continuity` was the silence branch — four alarms in one morning for a feed that had published nothing and lost nothing. So a transient outage is reported every run and fails only past `MAX_FEED_SILENCE`, where no later run can settle it either. A malformed feed still fails immediately, because no later run resolves malformed XML. When a check can wait for evidence that actually arrives, waiting is the honest design; when it cannot, silence in the exit code is not.

**A collector's fetch window must outlast its slowest publisher, not its fastest.** A check that fires is recoverable; a window that closes before the data arrives is not. `MACRO_WINDOW_DAYS` in `scripts/collect_daily.py` is 30 against the driver's 8 for exactly this reason, and the 8-day window had already cost one WTI observation permanently before anyone noticed. When adding a source, ask what its publication lag is and size the window off that — not off how often the pipeline runs.

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

## Git and CI

Two GitHub Actions workflows — `collect-news.yml` (hourly, twice-hourly in the KRX session) and `report.yml` (four scheduled firings per weekday) — run the pipeline and commit their output straight to `main` as `github-actions[bot]`. `origin/main` advances on its own many times a day, with no local action.

- Before starting work, and again before committing, run `git fetch` then `git rebase origin/main`. A local `main` that was current yesterday is now many commits behind.
- Never analyse "the latest data" from the working tree without fetching first; the local snapshot is stale by construction, and conclusions about coverage or gaps drawn from it are wrong.
- Rebase, don't merge. The bot pushes under a rebase-retry loop (`report.yml` Commit step); a local merge commit is noise against that.

---

## Secrets

Local credentials live in `.env` (gitignored); `.env.example` lists the keys. CI reads them from GitHub Actions repository secrets. `API-KEYS.md` is the inventory — what each key is and which component needs it.

CI secrets and local `.env` drift apart silently: a key can be held locally and absent from Actions. Several LLM stages are wired into the pipeline but produce nothing in production for this reason — see `notes/review-2026-08-27.md`. Do not assume a stage runs in CI because its code path exists and its key is in `.env`.

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
3. `ruff check .` reports no errors and `ruff format .` has been applied.
4. Validation functions exist for any new data path.
5. No `UNVERIFIED` marker remains that Ricky has not seen.
6. The diff has been shown.
