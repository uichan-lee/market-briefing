# Step 6 — embedding pipeline: plan

**Status (2026-08-15): half shipped.** The dedup half (`src/embed/encode.py`,
`src/embed/dedup.py`) is built, calibrated, and tested — see the module
docstrings for the numbers, not repeated here. Both of this plan's flagged
open questions for that half are now resolved by measurement rather than
guessed: **title-only** embedding (title+description performed far worse —
59 of 527 contrast pairs above the lowest known duplicate, vs. 2 for
title-only, both of which turned out on inspection to be duplicates the
cheap ground-truth heuristic missed) and **threshold 0.85**, not SPEC's
placeholder 0.92 (which would have missed 2 of 5 real cross-outlet
duplicates). `bge-m3`'s revision is pinned
(`5617a9f61b028005a4858fdac845db406aefb181`, read live off the HF Hub).

**Status (2026-08-16): the topicality half's mechanism is built; its
threshold is not.** `src/embed/topicality.py` now exists —
`build_profile_sentence()` derives a per-ticker profile sentence from
`config/watchlist.yaml`'s `name`/`sector` alone (no new hand-maintained
file), and `filter_topicality()` mirrors `dedup.py`'s shape exactly
(injectable `embed`, per-ticker grouping, a `TopicalityReport`). Two things
are deliberately still open, both left as parameters rather than guessed:
what text gets embedded on the article side (`article_text`, default
title+description, swappable once real labels exist — dedup's title-only
finding does not automatically transfer here) and the threshold itself,
which `filter_topicality` requires as an explicit keyword argument with no
default. `scripts/topicality_labels.py` is the tool that produces the label
set this plan said was needed — reuses `scripts/golden.py`'s
`load_articles`/`matched_pairs`/`stratified_sample` rather than duplicating
them, samples 150 candidates into `data/golden/topicality_candidates.jsonl`
(committed, sampled 2026-08-16), and a `label` subcommand records a binary
y/n judgment per article into `data/golden/topicality_v1.jsonl` — a
separate file from `v1.jsonl`, per this plan's own instruction not to reuse
it.

**Status (2026-08-22): calibrated against real labels — negative result,
Stage 1 deployment held.** Ricky labeled 149 of the 150 candidates
(`data/golden/topicality_v1.jsonl`, 112 topical / 37 not, 1 skipped) on
2026-08-21/22. Profile-sentence similarity was checked against both
`article_text` variants:

======================  ==================================  ======================================
strategy                AUC (149 labels)                     accuracy at best single threshold (Youden's J)
======================  ==================================  ======================================
title-only              0.744                                0.638
title+description       0.743                                0.611
======================  ==================================  ======================================

Both clear random (0.5) by a real margin, so the signal is not nothing — but
neither clears the trivial baseline of labeling every match `topical`
(accuracy 0.752, since the sample is 75% topical already). The reason: the
two class distributions overlap almost completely. `topical=True`
similarities range 0.210–0.579 (title-only); `topical=False` ranges
0.214–0.567 — the lowest true positive scores *below* the lowest false
positive, so no threshold exists that cuts real off-topic tail without also
costing true positives at least as often. Per-ticker z-scoring (to correct
for profile "richness" differing by ticker) was tried too and made it worse
(best accuracy 0.597) — expected, given ~5 labels per ticker is too few to
estimate a per-ticker mean/std from.

This is the opposite finding from dedup's calibration: there, every
inspected near-threshold "counterexample" turned out to be mislabeled ground
truth, i.e. the signal was *better* than the check. Here every check made the
signal look worse, and the false-positive/false-negative examples inspected
by hand read as genuinely ambiguous, not mislabeled — a 두산에너빌리티 wind
farm article whose real subject is the subcontractor, a 현대차 dividend
story that shares almost no vocabulary with "현대차 (005380), 자동차". A
short `name + sector` profile sentence apparently isn't a rich enough query
to separate "about this company" from "mentions this company" via cosine
similarity alone.

**Decision (Ricky, 2026-08-22): hold Stage 1 topicality deployment.**
`src/embed/topicality.py` and `scripts/topicality_labels.py` stay as-built —
tested, `threshold` still has no default — but nothing wires
`filter_topicality()` into the daily pipeline. SPEC §6.2's `relevance`
scoring (P&L materiality, already LLM-judged per matched article and
checked by `src/report/consistency.py`) already discounts off-topic
mentions downstream, so shipping a weak Stage 1 cut on top of it was judged
not worth the false-negative risk. Revisiting this needs a richer per-ticker
profile than `name + sector` (a business-description source, not yet
identified) — not attempted this round, since it would mean a new
hand-maintained data source and CLAUDE.md's entity-resolution rule against
inventing config casually applies to the same instinct here.

Not wired into `scripts/collect_daily.py` or anything else that runs daily —
out of scope per this plan's own boundary, and now also blocked on the
finding above rather than merely deferred.

Written before implementation, on 2026-08-12, revised 2026-08-13 after a
`/project-review` pass found three of the original version's load-bearing
claims did not hold up against the repo. This version corrects them rather
than editing quietly — see "What changed from the first version" at the
bottom, so the wrong claims stay visible instead of disappearing.

SPEC §12 step 6, SPEC §6.1 Stage 1. `sentence-transformers` was added
2026-08-12 as an optional `embed` extra (`pyproject.toml`), so the dependency
decision that blocked starting this is resolved. **This step is not on the
critical path for anything** — see "Priority" below, corrected from the first
version's claim that it was.

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
2. **Topicality filter** (renamed from "relevance filter" — see the design
   section below for why the name matters). Embed a per-ticker profile
   sentence and cut the bottom tail of article-to-profile similarity.

## Priority — corrected

The first version of this plan said step 6 was "the last thing standing
between the deterministic pipeline and a working `news_polarity`." That was
wrong. Measured against the live corpus: `src/entity/resolve.py` alone already
produces **92–148 (article, ticker) pairs/day** (mean ~108) — inside SPEC
§6.1's own 60–100 target band without any filtering. `src/llm/score.py`
already exists and is exercised by the bake-off. Nothing blocks running Stage
2 today, unfiltered, at roughly $17/month sticker price on `gpt-5.4` (the
2026-08-13 scoring choice, superseding `gpt-5.1` — `config/models.yaml`) —
effectively ~$0 in practice, since this volume sits inside the OpenAI
data-sharing free-token pool `gpt-5.4` shares with `gpt-5.1`. Either way,
inside Ricky's stated cost tolerance.

So step 6 is a **cost and quality optimization on an already-runnable path**,
not a blocker. What actually has a deadline is PREREGISTRATION §8.3's 2-week
gate criterion (inter-model polarity correlation on the live window,
2026-08-12 → 2026-08-26) — see
[`notes/gate-inter-model-plan.md`](gate-inter-model-plan.md), which is now
the higher-priority item. This step follows it.

## Why the 두산 alias claim was wrong, and what actually happened instead

The first version claimed `config/aliases.yaml:73-79` documented "~27% noise"
from 두산 (000150) baseball coverage that only this step's relevance scoring
could fix, and made removing that noise an exit criterion. **Neither the
number nor the file existed as described.** The 27% traced to a different
measurement entirely — the sports share of a since-retired site-wide feed
(`config/news_feeds.yaml`'s old `chosunbiz` entry), fixed on 2026-08-05 by
switching to section feeds. Re-measured against the live corpus for
2026-08-06..12 (after that fix): **8 matches on 000150, zero baseball.**

The actual residual noise (apartment-brand and affiliate-abbreviation
mismatches, not sports) was fixed the same way every other alias defect in
that file is fixed — four lines added to `exclude:`, 2026-08-13, verified
against the corpus: 8 matches → 4, and all 4 survivors are genuine 000150
coverage. No embedding involved, and none was needed. **This step carries no
두산-shaped exit criterion.** The general lesson stands: a topicality filter
should reduce cross-domain noise (a sports article, a real-estate article) as
a side effect of doing its actual job, but it is not owed credit for fixing a
defect a four-line config edit already closed.

## Design decisions

**Dedup threshold starts at SPEC's 0.92, tuned against the golden set, not
against intuition.** `data/golden/triage.jsonl` already has 100 KR articles
sampled from the real corpus; several are re-reports of the same story by
construction (SPEC §4's ambiguous-bucket sampling did not filter for this).
Measure pairwise cosine similarity within known-duplicate pairs before
picking a final threshold, rather than shipping 0.92 unverified.

**Dedup clusters within a ticker's article set, after entity resolution —
already settled, not open.** The first version listed this as an open
question; it isn't. SPEC's Stage 0 → Stage 1 ordering already implies it, and
`config/news_feeds.yaml:29-33` states the mechanism directly: outlets vary
wildly in body length (한국경제/조선비즈 0 chars, 뉴시스 ~1,155 chars) *"so
SPEC §6.1's clustering will group a 한국경제 headline with a 뉴시스 item
carrying the full body, and the cluster representative can be the latter."*
That is the representative-selection rule: **prefer the cluster member with
the longest `description`.**

**The length asymmetry is the actual open question the first version missed.**
A cosine threshold of 0.92 between a 0-character headline-only embedding and a
1,155-character full-body embedding of the same story will rarely clear 0.92
— short and long representations of identical content are not neighbors in
embedding space the way two paraphrases of similar length are. This needs
resolving before the threshold means anything: either embed title-only for
every outlet (throws away 뉴시스/머니투데이's extra signal) or find a
similarity measure that tolerates the asymmetry (e.g. compare against the
shorter text truncated to the longer one's opening, or weight title similarity
higher than body similarity). Calibrate against real cross-outlet duplicate
pairs in the golden set's triage sample, not against intuition.

**The topicality/materiality split — this is the substantive correction.**
The first version's design decision said: *"articles Ricky scored ≤0.3
relevance should mostly fall below whatever cut point Stage 1 picks."* That
assumes cosine similarity to a ticker profile sentence measures the same thing
`v1.jsonl`'s `relevance` dimension does. **It does not, and on the corpus's
largest noise class the two run in opposite directions.**

`relevance` was redefined 2026-08-07 and fully re-labelled 2026-08-12 to mean
*이 회사의 손익에 얼마나 닿나* — does this touch the company's P&L
(`src/llm/prompts/v1_scoring.md`). Its own anchor: *"수급·매매동향은 주가
얘기지만 손익에는 닿지 않는다 — 0.0~0.3."* But a 수급 article ("삼성전자
주가 급등, 외국인 순매수") is maximally *topical* to a 삼성전자 profile
sentence — cosine similarity would score it high while the golden set scores
it low by design. Forcing a single threshold to satisfy both constructs
produces a bad cut point in one direction or the other.

**Resolution: split the two constructs instead of conflating them.**
- **Stage 1 filters on topicality only** — is this article about this company
  at all (namesake, brand, passing mention, wrong-company analyst coverage),
  the thing `bge-m3` similarity is actually good at. Calibrate against a
  *new*, small, purpose-built topicality label set — not `v1.jsonl`.
- **Stage 2 keeps materiality**, exactly as it already does. `relevance`'s
  output is already used as a weight in SPEC §2.2③'s relevance-weighted
  polarity average, so a topical-but-immaterial article that survives Stage 1
  is already down-weighted toward zero downstream. Stage 1 does not need to
  replicate that job.
- **Why not calibrate Stage 1 against `v1.jsonl` even loosely:** doing so
  reads the golden set rather than writing to it, so it does not violate the
  no-model-touches-labels rule. But SPEC §7.3 requires the set to represent
  25 irrelevant / 25 ambiguous / 25 negative / 25 positive examples of the
  *unfiltered* corpus, and the §7.4 bake-off's correlation numbers — the ones
  behind the scoring model selection (`gpt-5.1` when this was written,
  `gpt-5.4` as of 2026-08-13 — `config/models.yaml`) — were measured against
  that unfiltered
  distribution. A production filter tuned to match the golden set's
  low-relevance tail would start silently removing the kind of article the
  golden set was built to represent, making the set describe a corpus
  production no longer sees. Keeping the two calibration sets separate avoids
  this.

**`bge-m3`'s HuggingFace revision must be pinned, not referenced by name
only.** Currently referenced only as `BAAI/bge-m3` — in `config/models.yaml`,
in this plan, and in `report.yml`'s cache key (`huggingface-bge-m3`). HF repos
are mutable; an unpinned revision plus a hard 0.92 threshold means a silent
upstream model update could shift cluster membership with no record of why.
This stage's entire justification (SPEC §6.1) is being "100% reproducible" —
an unpinned revision undermines the one property that justifies the stage's
existence. Pin a specific commit hash when the code is written.

**Cross-platform float determinism is a live risk, not a hypothetical.** The
same 0.92 threshold runs on two architectures — Mac arm64 (Ricky, local
testing) and ubuntu x86_64 (`report.yml`, production). Floating-point
embedding output can differ by ~1e-6 between them; near a hard threshold,
that is enough to flip cluster membership for borderline pairs. Either accept
this as noise (and widen the threshold's effective margin) or note it as a
known non-determinism and report it, rather than assuming embeddings computed
on two platforms are bit-identical.

**Output schema.** SPEC §3.3 pattern (raw + gitignored derived data) suggests
`data/embeddings/YYYY-MM-DD.parquet` follows the same convention as
`data/features/` — recomputed on each run, not committed. Confirm against
`.gitignore`'s existing `data/*` + allowlist structure before writing the
collector; do not add a new committed path without the same
`# UNVERIFIED`-grade justification `data/bakeoff/` and `data/golden/` needed.

**Look-ahead — already solved, not open.** The first version flagged this as
needing "an explicit read-through" of `src/util/session.py`; it's done and
in production already. `src/collectors/kr_news.py:479` computes
`known_at_utc = next_tradeable_open("KR", published_at)` per article at
collection time and stores it on every raw record. Stage 1 reads that
existing field; it does not need new look-ahead logic of its own.

**Does the relevance/topicality cut feed `news_volume_z` /
§2.2② `news_spike`?** SPEC §5 defines the article count for those features as
"after deduplication" — not after the topicality filter. If the topicality
cut also removes articles from that count, a live rating flag's meaning
changes. Decide and document before wiring either feature to Stage 1's output.

## What text gets embedded — the open question the first version missed

`config/news_feeds.yaml`'s per-outlet `description` length varies from 0
chars (한국경제, 조선비즈) to ~1,155 (뉴시스). Title-only embedding is
consistent across outlets but throws away the extra signal long-description
outlets carry; title+description is richer but directly causes the length-
asymmetry dedup problem above. This decides both dedup and topicality
filter behavior first-order and needs settling before either is implemented,
not left to fall out of whatever's convenient to code.

## Open questions — flagged rather than guessed

1. **What text is embedded** (title-only vs title+description) — see above,
   highest-leverage open question, unresolved.
2. **`sentence-transformers` model download and offline-test story.** `bge-m3`
   is a multi-GB download. No CI workflow runs `pytest` — `.github/workflows/`
   holds only `collect-news.yml` and `report.yml`, neither of which tests —
   so this is purely about Ricky's Mac, where the model downloads once and
   stays cached regardless of the fixture strategy. `tests/fixtures/`'s
   existing convention (committed sample payloads, per CLAUDE.md) still
   applies, but there is no CI-timeout pressure driving the decision the way
   the first version of this plan assumed.
3. **Whether the topicality cut affects `news_volume_z`'s "after deduplication"
   article count** — see the design-decisions section above.

## Scope boundary

Builds `src/embed/encode.py`, `src/embed/dedup.py`, `src/embed/relevance.py`
(or renamed to reflect the topicality/materiality split above —
`src/embed/topicality.py` may be the more honest name) with offline tests,
using committed fixtures per the open question above. Does **not** wire Stage
1 into `scripts/collect_daily.py`'s daily run, build the LLM synthesis stage
(§2.2⑤/⑧), or touch `src/report/consistency.py`'s wiring into `render.py` —
that is a separate, already-flagged item ([MANUAL-TASKS
§10](../MANUAL-TASKS.md)) that depends on this step's output existing, not on
this step's code. Also does not build
[`notes/gate-inter-model-plan.md`](gate-inter-model-plan.md)'s live-sampling
work, which precedes this step in priority and needs none of it.

## What changed from the first version (2026-08-12 → 2026-08-13)

A `/project-review` direction-lens pass found three claims that did not hold
up against the repo, corrected above rather than silently:

1. **Priority claim was wrong.** "Step 6 is the last thing blocking
   `news_polarity`" — false; entity resolution alone already produces pairs in
   SPEC's target band. Corrected in "Priority" above, and step 6 was
   deprioritized behind `notes/gate-inter-model-plan.md`.
2. **The 두산 exit criterion's evidence didn't exist.** The cited "~27% noise"
   in `config/aliases.yaml:73-79` was a different measurement from a different
   file, about a defect already fixed 2026-08-05. Corrected above; the actual
   residual noise was fixed by a four-line alias edit, not embeddings.
3. **The relevance-filter calibration target measured the wrong construct.**
   Cosine-similarity topicality and the golden set's P&L-materiality
   `relevance` are different things and anti-correlated on the corpus's
   largest noise class. Corrected via the topicality/materiality split above.

Two open questions from the first version were also resolved on
re-examination (dedup ordering — already answered by `config/news_feeds.yaml`;
look-ahead — already built into `kr_news.py`), and two new ones were added
(what text to embed; whether the topicality cut affects `news_volume_z`).
