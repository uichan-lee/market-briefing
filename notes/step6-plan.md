# Step 6 — embedding pipeline: plan

Written before implementation, on 2026-08-12. SPEC §12 step 6, SPEC §6.1 Stage 1.
Steps 7 and 8 (golden set, bake-off) finished the same day — `gpt-5.1` selected
for Stage 2 scoring — which is why this step is next: it is the last thing
standing between the deterministic pipeline that already runs daily and a
working `news_polarity`.

## What this has to produce

Per SPEC §6.1: take the 1,000–2,000 raw articles Stage 0 (entity matching,
already built — `src/entity/resolve.py`) hands it, and cut that down to the
60–100 that reach Stage 2 (LLM scoring, already built — `src/llm/score.py`,
exercised so far only by the bake-off). Two things do the cutting:

1. **Re-report detection.** Korean outlets republish the same wire story across
   multiple outlets with minor rewording. Cluster near-duplicates by cosine
   similarity on `bge-m3` embeddings (threshold 0.92, SPEC §6.1 — an initial
   value, "to be tuned against the golden set"), keep one representative per
   cluster.
2. **Relevance filter.** Embed a per-ticker profile sentence (SPEC §6.1: "against
   ticker profile sentences" — the exact sentence template is not yet
   specified and is this step's first open question, see below) and cut the
   bottom tail of article-to-profile similarity.

## Dependency decision — this step's actual blocker

`sentence-transformers` is SPEC §3's named choice for running `bge-m3` locally,
but it has not been added to `pyproject.toml` yet — CLAUDE.md's rule is
"dependencies are added when the code that needs them is written," and this is
that code. Per CLAUDE.md, adding it needs Ricky's go-ahead with a stated
reason before the first line of `src/embed/` is written. That is
[README's blocking-work item 1](../README.md#whats-blocking-progress) and the
actual reason step 6 hasn't started yet — not effort, a decision.

## Why the 두산 alias fix belongs to this step, not a separate one

`config/aliases.yaml:73-79` already documents a live, measured defect: 두산
(000150)'s alias config carries **~27% noise** from baseball-team headlines,
because for this one ticker the group name *is* the company name and cannot be
excluded by `exclude:`/`ambiguous_parents:` the way every other ambiguous name
in that file is handled. The alias file itself names the fix: it needs
**relevance scoring**, which does not exist anywhere in this repository until
this step builds it.

So this is not two projects that happen to share a ticker. **The relevance
filter's ability to push 두산 sports coverage below the cut is a real exit
criterion for step 6**, not a nice-to-have:

- Before: measure 두산's current noise rate precisely (README/Lens B cited
  ~27%; get the exact current figure from `scripts/config_helper.py audit` on
  today's collected corpus before writing any embedding code, so there's a
  real baseline rather than a remembered one).
- After: run the same audit through the relevance filter and report the new
  rate. If it does not drop materially, that is a finding about the profile
  sentence design (see open question 1 below), not a reason to ship anyway and
  hope entity resolution alone will paper over it.

## Design decisions

**Dedup threshold starts at SPEC's 0.92, tuned against the golden set, not
against intuition.** `data/golden/triage.jsonl` already has 100 KR articles
sampled from the real corpus; several are re-reports of the same story by
construction (SPEC §4's ambiguous-bucket sampling did not filter for this).
Measure pairwise cosine similarity within known-duplicate pairs before
picking a final threshold, rather than shipping 0.92 unverified.

**The relevance filter's cut point is a golden-set question, same as the dedup
threshold.** Cutting "the bottom tail" needs a percentile or absolute
threshold — SPEC does not fix one. `data/golden/v1.jsonl`'s `relevance`
dimension (100 examples, hand-labelled, the same file the bake-off measures
models against) is the ground truth to calibrate against: articles Ricky
scored ≤0.3 relevance should mostly fall below whatever cut point Stage 1
picks, or Stage 1 is discarding signal Stage 2 would have used, or letting
noise through that Stage 2 pays to re-reject.

**Output schema.** SPEC §3.3 pattern (raw + gitignored derived data) suggests
`data/embeddings/YYYY-MM-DD.parquet` follows the same convention as
`data/features/` — recomputed on each run, not committed. Confirm against
`.gitignore`'s existing `data/*` + allowlist structure before writing the
collector; do not add a new committed path without the same
`# UNVERIFIED`-grade justification `data/bakeoff/` and `data/golden/` needed.

**Look-ahead.** Same discipline as every other feature: an article is only
usable once its publication timestamp has passed, and Stage 1 must not embed
or cluster using anything timestamped at or after the `as_of` boundary it is
computing for. This is not new to this step, but it is the first step where
"the article" rather than "the price bar" is the thing being time-boundaried,
so the existing `next_tradeable_open()` logic in `src/util/session.py` needs
an explicit read-through before Stage 1 is wired to the daily run, not an
assumption that it obviously applies.

## Open questions — flagged rather than guessed

1. **The ticker profile sentence's exact content is unspecified.** SPEC §6.1
   says "against ticker profile sentences" and nothing else. A one-line
   template ("{종목명}, {업종} 기업, 코드 {ticker}") is the obvious starting
   point but has not been validated against the golden set's relevance labels.
   This is the single highest-leverage open question — a bad profile sentence
   makes the relevance filter noise, and the 두산 exit criterion above cannot
   be judged until this is settled.
2. **Whether dedup runs before or after entity resolution.** SPEC's diagram
   puts Stage 0 (entity matching) before Stage 1 (embedding), which resolves
   which ticker an article is about before dedup runs — meaning duplicate
   detection should cluster within a ticker's article set, not across the
   whole day's corpus. Confirm this reading before implementing; clustering
   across tickers first would risk collapsing two different companies'
   coverage of the same macro event into one representative.
3. **`sentence-transformers` model download and offline-test story.** `bge-m3`
   is a multi-GB download. `tests/fixtures/` holds committed sample payloads
   per CLAUDE.md's testing convention — decide whether embedding-pipeline
   tests use committed precomputed embedding vectors (fast, no model download
   in CI) or a smaller stand-in model, before writing the test suite, not
   after hitting CI timeouts.

## Scope boundary

Builds `src/embed/encode.py`, `src/embed/dedup.py`, `src/embed/relevance.py`
with offline tests, using committed fixtures per the open question above.
Does **not** wire Stage 1 into `scripts/collect_daily.py`'s daily run, build
the LLM synthesis stage (§2.2⑤/⑧), or touch `src/report/consistency.py`'s
wiring into `render.py` — that is a separate, already-flagged item
([MANUAL-TASKS §10](../MANUAL-TASKS.md)) that depends on this step's output
existing, not on this step's code.
