# market-briefing

Automated daily market briefing for Korean and US equities. It states a directional opinion on every ticker it tracks, with the evidence behind it — and then stops. **This system does not execute trades.**

한국어: [README.ko.md](README.ko.md)

---

## What this is

Twice a day, a GitHub Actions run collects market data and news, turns the news into structured numbers, computes features, and renders a Korean-language markdown briefing that lands in an Obsidian vault and an inbox. A human reads it and decides what to do.

| Run | Time (KST) | Covers |
|---|---|---|
| `RUN_MORNING` | 07:00 | US close (previous day) + KR pre-market — 2h before KOSPI opens |
| `RUN_EVENING` | 21:30 | KR close (same day) + US pre-market — 1h before US opens |

### The five rules that shape everything else

1. **Opinions are computed, not written.** The briefing rates every ticker 강한 매수 through 강한 매도, but that rating is a weighted sum of z-scores — not LLM prose. The LLM only converts individual articles into a fixed numeric schema; it never sees the rating. This is what makes the opinion reproducible, and reproducibility is the precondition for ever finding out whether it was any good.
2. **Determinism first.** Anything solvable with string matching, embedding similarity, or statistics is solved that way. LLM calls are reserved for judgments that genuinely need language understanding — this is what makes results reproducible enough to evaluate.
3. **Raw data is immutable.** Collected data is written to date-partitioned storage and never overwritten. In three months it becomes the backtest dataset.
4. **Models are swappable.** All model calls go through one adapter. No vendor SDK is imported anywhere else.
5. **Evaluation criteria were frozen before any data was collected.** See [PREREGISTRATION.md](PREREGISTRATION.md).

### What it deliberately does not do

- **No order, execution, or cancellation API calls.** Read-only endpoints only. The briefing gives an opinion; a human decides and acts.
- No LLM-authored ratings or rationale — those are arithmetic, and auditable as such.
- No auto-generated ticker aliases (a wrong alias corrupts every downstream number silently).
- No real-money trading before the 3-month evaluation gate passes.

> **Opinion vs. execution.** These are different things and the project takes both seriously. Stating "강한 매수" with cited evidence is the product. Placing the order is not, and never becomes so.

---

## Current status

**Running stage.** The deterministic pipeline collects, resolves, computes, rates, renders and delivers twice a day without supervision, and has done so since 2026-08-03. `data/raw/` holds a 3-year backfill plus a live news record that began 2026-08-03. The golden set and the model bake-off (SPEC §12 steps 7–8) are both finished — `gpt-5.1` was selected for scoring on 2026-08-12 and replaced by `gpt-5.4` on 2026-08-13 once OpenAI's data-sharing free-token program (which `gpt-5.6`'s tiers are not part of) made 5.4 effectively free at current volume and a measurable quality step up. What's still missing is the embedding pipeline (step 6), without which `news_polarity` has nothing to score.

| Component | Status | Notes |
|---|---|---|
| Design docs (SPEC, PREREGISTRATION, MANUAL-TASKS) | ✅ Done | Evaluation criteria frozen 2026-08-02 |
| Python project, testing, linting | ✅ Done | `uv` + `pytest` + `ruff`, 602 offline tests passing, 9 network |
| Time & market sessions (`src/util/session.py`) | ✅ Done | Trading days, DST, look-ahead boundary |
| Collector validation framework (`src/collectors/validate.py`) | ✅ Done | The four checks every collector must pass |
| Config loading & safeguards (`src/util/config.py`) | ✅ Done | Rejects alias collisions, unquoted tickers |
| Directional rating (`src/report/rating.py`) | ✅ Done | 7-point scale + rationale; weights need calibration |
| AI commentary guard (`src/report/consistency.py`) | ✅ Done | Drops the 총평 if it contradicts the computed rating |
| Config files | ✅ Done | `watchlist.yaml` 31 KR + 40 US and `aliases.yaml` both filled and verified against live data |
| API credentials | ✅ Done | KRX and Alpaca verified live; all 9 values mirrored into Actions secrets. Only KIS outstanding, and it blocks nothing yet |
| `kr_price` collector (pykrx OHLCV) | ✅ Done | Four checks + committed fixture; known value cross-checked against Naver |
| `macro` collector (FRED) | ✅ Done | 6 series verified live; known value cross-checked against Treasury |
| `kr_news` collector (outlet RSS) | ✅ Done | 14 feeds via Actions, twice hourly in session; ~980 articles/poll across 8 outlets |
| `us_price` collector (Tiingo) | ✅ Done | Four checks + committed fixture; known value cross-checked against Yahoo Finance |
| `us_price` over Alpaca | ✅ Done | The US source in use. SIP confirmed on the free plan; **48 symbols in 2 requests** where Tiingo needed 48 |
| `kr_flow` collector (pykrx) | ✅ Done | Investor flows, short interest, cap, fundamentals — **the 55% of rating weight KRX was gating**. Six checks incl. an accounting identity and a cross-collector price check |
| Entity resolution (`src/entity/resolve.py`) | ✅ Done | Alias-driven, ambiguous bucket reported in the header — running at 7.9% of 1,013 articles |
| Feature computation (`src/features/compute.py`) | ✅ Done | 5 of the 7 weighted features; 252-day rolling z-score per ticker. `short_ratio` carries KRX's ~3-session disclosure lag since 2026-08-13 — before that it reached no published rating at all |
| Report renderer + delivery (`src/report/`, `src/notify/`) | ✅ Done | SPEC §2 sections, `vault` + `email`, HTML mail with a `text/plain` alternative |
| GitHub Actions workflow | ✅ Done | `collect-news.yml` hourly, `report.yml` morning ×3 + evening; live since 2026-08-03 |
| Golden set (100 hand-labeled articles) | ✅ Done | 100 examples × 5 dimensions, `relevance` fully re-labelled 2026-08-12 after a mid-run definition change; `verify` reports zero conflicts. The `relevance` and `forwardness` noise floors are **withdrawn** — both predate the redo ([PREREGISTRATION §R](PREREGISTRATION.md), 2026-08-12 and 08-13) and a fresh `golden recheck` is what restores them |
| LLM adapter + scoring bake-off | ✅ Done | `src/llm/adapter.py` + `src/eval/bakeoff.py`, 3,195 calls logged in `data/bakeoff/attempts.jsonl`. **`gpt-5.1` selected 2026-08-12, replaced by `gpt-5.4` 2026-08-13** — see below |
| Embedding pipeline (dedup + relevance) | ⬜ Not started | SPEC §12 step 6, dependency resolved 2026-08-12. Not blocking `news_polarity` — see caveat below |
| `news_polarity`, `rev_4w` features | ⬜ Not started | Both carry live weight in `config/rating.yaml` — see the caveat below |
| `src/eval/ic.py`, `shadow_portfolio.py` | ✅ Done | The 3-month gate (§8.5). **Criteria pinned 2026-08-13, before the code** — window 2026-08-13 → 2026-11-13, 62 KR sessions. Reports "not enough sessions" until November, which is the correct answer |
| `us_filings`, `kr_filings` | ⬜ Not started | SEC EDGAR and DART |

**The pipeline runs end to end today.** Collectors → features → computed rating → rendered briefing → email, twice a day, unattended. What is missing is the *news* half of the score: the LLM stage (SPEC §12 steps 6–8) is not built, so `news_polarity` produces nothing.

**The two weights for features that do not exist are gone (2026-08-08).** `news_polarity` (0.20) and `rev_4w` (0.15) sat in `config/rating.yaml`'s `weights` with nothing computing them, so `rate()` renormalized against 1.10 of intended weight while only 0.75 could ever arrive — leaving 18 points of margin above the `min_weight_coverage: 0.5` floor instead of 50. They now live in a `deferred_weights` key that `rate()` never reads and the report header still names. It was not a bookkeeping change: because the composite is `Σ|weight| × z`, every score fell by 0.75/1.10 = 0.68×, **8 of 31 published ratings changed label**, and the five tickers the floor had been forcing to `관망` were released. Rank order is preserved, so PREREGISTRATION §8.4's IC, ICIR and quantile spread are unaffected — only the displayed bucket moved. Logged in [PREREGISTRATION §R](PREREGISTRATION.md).

> [!note]
> **The outer buckets are now harder to reach, and that is unresolved.** The composite's scale tracks total weight, so `강한 매수`/`강한 매도` begin at a uniform z of **2.67** where they began at 1.82. Rescaling the cut points is permitted by §8.4 for distributional reasons, but doing it inside the same change that moved the scale would have made the two indistinguishable afterwards. Left as measured, and pinned by `test_the_outer_bucket_needs_a_z_of_two_point_seven`. It belongs with the calibration in [MANUAL-TASKS §6](MANUAL-TASKS.md).

**`short_ratio` had never once entered a published rating, and now does (2026-08-13).** KRX discloses a session's short-sale balance about three sessions later, and `src/features/compute.py` divided `short_balance` by `shares_outstanding` **on the same row** — so on the newest session, which is the only one a rating is ever computed for, the numerator was always absent. The feature is missing from all 31 tickers of **5 of the 6 published sessions**; only the 2026-08-03 run, computed off the backfill, carried it. That is 0.10 of the 0.75 live weight, presented as working by this file's own status table and by the report header. The same defect ran backwards through history: each session's balance was read on the session it describes rather than the session it was published on, which is look-ahead — `known_at_utc` is stamped once per row at the session close for every column alike, so `_visible()` cannot express a per-column lag and never could. **The ratio is now formed on its own session and shifted forward by the disclosure lag, per ticker.** Nothing under `data/raw/` was touched and no KRX request was re-issued: the archive already self-repairs, because `collect_daily.py` re-fetches an 8-day catch-up window every run and writes a `-vN` when a stored date gains rows. Measured over the current archive, recomputing both ways: weight coverage on the two sessions whose balance is still undisclosed rises **0.768 → 0.901**, and labels move on 2, 3, 0, 2, 9 and 4 of 31 tickers for 2026-08-03, 06, 07, 10, 11 and 12. Logged in [PREREGISTRATION §R](PREREGISTRATION.md).

> [!note]
> **The look-ahead boundary is now enforced in two places, not one.** `_visible()` filters whole rows on `known_at_utc`; the short-balance lag lives in the feature definition because a single per-row timestamp cannot carry a per-column disclosure delay. `compute.py`'s docstring says this outright, so that reading `_visible()` alone does not leave the impression the boundary is handled in one place. A separate defect found in the same pass and **not** fixed here: `src/report/render.py` calls `compute(..., as_of=None)`, which makes `_visible()` inert in production. Live ratings do not read the future today — `collect_daily.py`'s `kr_end` clamps the fetch window first — but the guard is doing nothing. It would matter to anything that reconstructs a past `as_of`, and the naive repair makes it worse rather than better: `_visible()` compares `known_at_utc < as_of` strictly, while the evening run's `as_of` is that session's close and the session's own rows are stamped at exactly that instant — so passing it would drop the very session being rated and `write_ratings()` would refuse the empty frame. The boundary has to become the run's clock, not the session's, which is its own careful change. `src/eval/ic.py` deliberately does not depend on it: it reads the **published** scores in `data/ratings/` rather than recomputing features, which is both what was actually knowable on the day and what §8.4 calls the pipeline's actual output.

**The news collector's alarm was pointing the wrong way, and is fixed (2026-08-10).** Five consecutive `collect-news` runs mailed a failure on the night of 2026-08-08 and none of them had lost anything. `last_run_at` reads the run clock off the last written filename, but a run with no new articles wrote no file — so through the quiet Korean night `check_collection_gap` measured time since the last *article* and reported 2.9h, 3.8h, 5.0h, 5.8h for a collector firing on schedule, while `check_feed_continuity` passed on every one. The same conflation ran the other way in the exit code, which consulted the report only when the frame was empty: a run that stored articles with a feed timed out — unmeasured, unrecoverable loss — exited 0 and stayed silent. **Runs now always write what they collected, empty or not, and the exit code reports validation and nothing else.** The commit step became `if: always()`, without which the new alarm would have skipped the commit and destroyed the articles it was warning about.

**The same alarm was wrong a second way, and that is fixed too (2026-08-11).** It fired the moment a feed did not answer, on the reasoning that unverified loss is unmeasured and unrecoverable. The first half holds for about an hour; the second does not. The next run where the feed answers compares its buffer against the same stored history and settles what the outage cost — that comparison already ran, and **over the sixty runs to 2026-08-11 every failure of the check was the silence branch and not one measured a loss**, while every recovering run passed. It mailed four alarms between 03:05 and 07:58 KST that morning for 전자신문, which had published nothing at all between 13:30Z and 21:00Z and whose articles the 22:00Z run recovered intact. Three of them were consecutive, which the §8.5 gate's third condition would have failed — for weather rather than for a defect. **An outage is now reported every run and fails only on a measured loss, or past `MAX_FEED_SILENCE` (24h) where no later run can settle it either.** `fetch` stops double-reporting the same event: malformed XML still fails on its own, because no later run resolves it, while a connection failure defers to the check holding the evidence. Since the check changed an hour into the two-week window, the window moved a day to 2026-08-12 → 2026-08-26 so it is measured under one definition; it held zero runs at the time, so nothing measured was discarded.

**Macro's window now outlasts its slowest publisher (2026-08-10).** The FRED FX series `DEXKOUS` and `DTWEXBGS` run about a week behind the daily ones, and the morning run's `end` is the *previous* UTC day relative to the KST date it is read on — so the 8-day window opened after their last observation and the 2026-08-10 briefing shipped with `usdkrw has no rows at all`. Every Monday would have landed identically. Widening to 30 days for macro only (`kr_flow`'s 124 KRX requests keep the global window at 8) also surfaced the expensive half: **`wti` 2026-07-28 was missing from three years of stored history while FRED serves 80.91 for it** — an EIA value published after the short window had moved past the date, with nothing left to re-fetch it. It is the only genuine gap in the stored macro; every other one is Columbus Day or Veterans Day, when the bond market is shut and NYSE is not. The briefing header now dates macro separately, because the USDKRW level on the market line is routinely older than the prices printed beside it.

**The rating archive stopped double-counting sessions (2026-08-08).** `load_rating_history()` was concatenating every parquet under `data/ratings/` — **217 rows for 3 real sessions**, with 2026-08-06 stored four times and `2026-08-07.parquet` holding 31 tickers at 0% coverage from a run that fired before KRX opened. It now selects the newest version per session (93 rows), and `write_ratings()` refuses a frame the report already refuses to publish. Nothing was deleted: the superseded files stay on disk as the record that the bug reached publication.

**Korean news collection is live and unblocked** — `kr_news` reads 14 outlet RSS feeds via GitHub Actions — twice an hour through the KRX session, hourly otherwise — needing no credential at all. Because RSS cannot be backfilled, that clock only starts once `collect-news.yml` is on the default branch.

**The KRX blocker is cleared.** `data.krx.co.kr` went members-only and returned HTTP 400 `LOGOUT` without a session, which withheld investor flows, short interest, market cap and fundamentals — **55% of the rating weight**, enough to force every ticker to `관망`. A login now works and all six gated endpoints were verified live on 2026-08-04 ([API-KEYS.md §0](API-KEYS.md)). `kr_flow` is now built on top of it and passing.

**The golden set is labelled.** All 100 examples carry all five dimensions, the four buckets are 25 each, and `verify` reports no rule conflicts. The labelling was Ricky's alone — no model supplied or corrected a value, which is the property the whole bake-off rests on. What automation did contribute is measured rather than assumed: **8% of the finished set had its bucket changed after a rule flagged it, and 14% holds at least one score a rule sent back to be re-scored** (`review_influence` and `redo_influence` in `scripts/golden.py`). Both numbers undercount, because two dimension *definitions* were sharpened mid-run with Claude's input — recorded in [PREREGISTRATION §R](PREREGISTRATION.md).

**The golden set is finished and the bake-off is unblocked (2026-08-10).** `golden recheck` re-labelled 10 examples a day later without showing the first answers, and `verify` passes at a mean gap of 0.16 against its 0.25 threshold. The set also separates well: `|polarity| ≥ 0.5` on 50 of 100 examples, double the floor `verify` enforces, with a mean polarity of +0.04 and no positivity skew.

What the recheck also showed is that the disagreement is not spread evenly. **`forwardness` carries almost all of it** — mean gap 0.13 against 0.03–0.07 for the other four dimensions, every ±0.25 deviation, and a direction rather than scatter (5 of 6 moves went down). The three largest were 확정됐지만 처음 알려진 사실, which is exactly the case the dimension's own written hint sends the other way. The schema was deliberately not rewritten: editing a definition while its finished labels are visible is the contamination [PREREGISTRATION §R](PREREGISTRATION.md) already had to declare once. Instead the floor is recorded there in advance — a `forwardness` difference between two models below 0.13 is not evidence, and a v2 set fixes the anchors before any label is written.

> [!warning]
> **Both numbers above are superseded and neither floor stands (2026-08-13).** The paragraph records what the 2026-08-10 recheck measured, which is still what happened — but it measured against labels `golden label --redo-all` rewrote on 2026-08-12. `relevance`'s floor was withdrawn that day and `forwardness`'s on 2026-08-13, both in [PREREGISTRATION §R](PREREGISTRATION.md); `src/eval/bakeoff.py` now prints `—` for the pair rather than a number. **Until a fresh `golden recheck` runs against the corrected labels, no model-to-model difference along `relevance` or `forwardness` is evidence in either direction, however large it looks.** `polarity` (±0.07), `intensity` (±0.07) and `uncertainty` (±0.03) are unaffected, and `polarity` is the one the §8.5 gate keys on.

**The bake-off ran and `gpt-5.1` won on cost (2026-08-12).** `claude-sonnet-5` and `gpt-5.1` both passed every §7.4 bar; `gemini-3.5-flash` did not — not on price, but because its free tier's 20-calls/day cap forced `--repeats 1`, leaving its self-consistency unmeasured rather than failed. Between the two that passed, `gpt-5.1` costs 8.3× less per valid signal ($0.0101 vs $0.0837), and MANUAL-TASKS §5's pre-registered rule takes the cheaper of any two that pass. sonnet-5's 0.07 relevance lead (0.83 vs 0.76) is real and is not the basis for excluding it — the rule selects on cost among passing candidates, not on which one scores highest — but the comparison's noise floor (±0.07, from a `golden recheck` run 2026-08-10) predates a mid-run redefinition of `relevance` and is not currently valid for judging that gap. It needs re-measuring, not reused or dropped; [PREREGISTRATION §R](PREREGISTRATION.md) records the gap as open rather than closing it either way.

**`gpt-5.1` was replaced by `gpt-5.4` a day later (2026-08-13), after two gaps surfaced under questioning rather than new data.** First, the OpenAI side of the original bake-off was never a survey of OpenAI's own lineup — `gpt-5.1` was picked only because `gpt-5` refused `temperature=0` and 5.1 was the nearest version that accepted it; `gpt-5.2`, `gpt-5.4`, `gpt-5.5` and `gpt-5.6` had all already shipped by 2026-08-11 and none were checked. Second, OpenAI's data-sharing program (Data Controls → Sharing) gives daily free tokens — up to 250K/day shared across `gpt-5.1`, `gpt-5.2`, `gpt-5.4` and others, up to 2.5M/day for their mini/nano tiers — and **`gpt-5.6`'s tiers (sol/terra/luna) are not in either pool.** That inverts the sticker-price comparison that motivated re-testing in the first place: `gpt-5.6-luna`'s much-publicized 80% price cut (2026-07-30) turns out cheaper on paper but costlier in practice than a free-tier-eligible model at current volume. `gpt-5.4` passed §7.4's bars with a real edge over `gpt-5.1` (relevance 0.78 vs 0.76, polarity 0.86 vs 0.82, uncertainty 0.55 vs 0.43 — the last one clear of its ±0.03 noise floor) at effectively the same cost, so it replaced 5.1. `gpt-5-nano` and `gpt-5.4-nano` were tested as free-tier backups and both failed relevance outright; `gpt-5.6-terra` was dropped mid-run once luna and sol covered its price range. Full numbers: `config/models.yaml`'s `scoring:` comment and [PREREGISTRATION §R](PREREGISTRATION.md)'s 2026-08-13 entry.

Getting there required a full re-labelling of the golden set's `relevance` dimension: all three models failed the correlation bar simultaneously (0.70/0.67/0.58 against a >0.7 threshold), which pointed at the ruler rather than the models — `relevance`'s definition had moved mid-labelling on 2026-08-07 and old labels hadn't all followed. All 100 examples were re-scored blind (not just the ones a rule flagged, to avoid letting model disagreement pick which labels get revisited — the circularity [PREREGISTRATION §R](PREREGISTRATION.md) already named once). 66 of 100 changed, mostly downward. Two triage errors also surfaced this way — 두산 (000150) and 현대차 (005380) were bucketed `irrelevant` when their own scores said otherwise — and were corrected, which is why `redo_influence()`'s 68% no longer means what its docstring says: most of that is the deliberate full redo, not a rule sending a score back. `review_influence()`'s 11% (bucket changes recorded in `review.jsonl`) still measures what it always did.

**The schedule is best-effort, and that is measured, not assumed.** GitHub fires roughly a third of the declared runs: over 2026-08-03..07 the news workflow declared 31 runs a day and delivered 6–10, for **42.6% hourly coverage** (40 of 94 hours). Both scheduled report runs on record were hours late. Everything downstream is built to survive it — `last_closed_session()` resolves a run from the clock rather than the date, the morning report is declared three times behind a published-check, and `check_feed_continuity` measures what a gap actually cost instead of guessing. With `etnews_economy` removed, **1 of 45 observed gaps (2.2%)** exceeded the fastest remaining feed buffer, so the coverage number is alarming but the realised loss is not.

---

## How the pipeline works

### Daily run, end to end

```
  Collectors                    ─→  data/raw/YYYY-MM-DD.*     (immutable, never overwritten)
      │                                  │
      │  pykrx, DART, outlet RSS,        │
      │  SEC EDGAR, FRED, Alpaca         │
      ↓                                  ↓
  Entity resolution             ─→  which article is about which ticker
      │  deterministic, driven by config/aliases.yaml
      │  ambiguous cases are DROPPED, never guessed
      ↓
  Embeddings (local bge-m3)     ─→  data/embeddings/
      │  ├─ dedup: cosine > 0.92 collapses Korean media re-reporting
      │  └─ relevance filter: drop the bottom tail
      │  1,000–2,000 articles  →  60–100 survivors
      ↓
  LLM scoring (Stage 2)         ─→  data/scores/    ← the only step that costs money
      │  5 dimensions per article: relevance, polarity,
      │  intensity, uncertainty, forwardness
      ↓
  Feature computation           ─→  data/features/
      │  every feature is a 252-trading-day rolling z-score per ticker
      ↓
  Rating (deterministic)        ─→  강한 매수 … 관망 … 강한 매도, per ticker
      │  weighted sum of z-scores → 7-point scale, no LLM involved
      │  rationale = the largest terms in that sum
      ↓
  Report rendering              ─→  the deterministic sections, laid out
      ↓
  LLM synthesis (Stage 3)       ─→  red team + AI 총평
      │  reads the rendered sections only — never raw articles,
      │  so it cannot re-score anything
      ↓
  Consistency guard             ─→  every rating label in the 총평 is compared
      │  against the computed rating; on contradiction the section is
      │  DROPPED and the reason goes in the header
      ↓
  Delivery                      ─→  reports/YYYY-MM-DD-morning.md
                                    vault (git commit) + email
```

### Why the news pipeline is shaped this way

The expensive step (LLM scoring) runs last and on the fewest items. Deduplication and relevance filtering were moved out of the LLM and into local embeddings for three reasons: cost drops to zero, embeddings are perfectly reproducible where an LLM is not even at `temperature=0`, and sentence-level similarity catches Korean media re-reporting more accurately than an LLM judgment does.

### What the briefing contains

1. **US → KR transmission** — the most defensible quantitative content in the briefing, and one of the sections with no LLM involvement at all. US sector ETFs mapped to Korean sectors, each shown with its trailing 60-day rolling correlation so a broken-down relationship is visible rather than silently trusted.
2. **Holdings & watchlist scan** — one line per ticker, flagged on foreign-investor flow, new filings, earnings revisions, valuation band, news spikes, volatility.
3. **News score aggregation** — per-ticker rollup of the Stage 2 scores.
4. **Calendar** — earnings, FOMC/CPI, options expiry, ex-dividend dates.
5. **Red team** — the LLM is instructed to argue *only* against the day's conclusions.
6. **Directional rating** — every ticker on a seven-point scale, with its evidence.
7. **Shadow portfolio P&L** — what a hypothetical account following those ratings would have done, tracked separately from any real account. This is what makes the ratings falsifiable rather than merely opinionated.
8. **AI 총평** — five to eight lines synthesizing all of the above into what a reader with thirty seconds needs. LLM-authored, and the only such section besides the red team. See below.
9. **Medium-term regime** — yield curve, dollar, USD/KRW, WTI, and US sector rotation over 120 days. Deterministic, like ①.

Display order is not ID order: ⑧ renders first (so a rushed reader hits it immediately) but is generated last (it consumes everything else). The IDs are stable identifiers referenced across the docs and code, so they are never renumbered when a section is added.

### How the AI commentary stays honest

The commentary is the only place an LLM writes prose about direction, which is exactly the thing this project otherwise refuses to let a model do. Three properties keep it from becoming the rating, and all three are load-bearing:

- **It reads the rendered deterministic sections, never raw articles.** Scoring already happened upstream; the commentary cannot redo it.
- **It is checked before publication.** `src/report/consistency.py` finds every rating label in the prose, attributes it to a ticker via `config/aliases.yaml`, and compares it against the computed rating. Contradiction → the section is dropped, and the header says so.
- **Nothing downstream reads it.** No feature, no score, no shadow portfolio, no evaluation metric. It is a leaf.

The guard is string matching, not a second LLM — a checker that needed judgment to verify a judgment would be checking nothing. It and the prompt are a matched pair: the prompt reserves the seven rating labels as rating vocabulary and requires ordinary market movement be written as `순매수` / `수급 이탈`, which is what makes the matching sound. Without that rule `매수` appears in normal Korean market prose constantly and the section would be dropped daily.

### How the rating works

A weighted sum of the feature z-scores plus aggregated news polarity, bucketed onto seven levels. Weights and cut points live in `config/rating.yaml`.

| 강한 매수 | 매수 | 약한 매수 | 관망 | 약한 매도 | 매도 | 강한 매도 |
|---|---|---|---|---|---|---|
| ≥ +2.0 | +1.0 | +0.4 | ±0.4 | −0.4 | −1.0 | ≤ −2.0 |

The rationale is the decomposition of that sum — each term's `weight × z-score`, largest first — so it reports literally what moved the number and cannot drift from it:

```
005930 삼성전자 — 매수 (+1.13)
  · foreign_flow_5d    z=+2.10   기여 +0.63
  · rev_4w             z=+1.20   기여 +0.18
  · rel_strength_20d   z=+0.90   기여 +0.14
  · news_polarity      z=+0.60   기여 +0.12
  · 그 외 3개 항목                기여 +0.065
```

Only the top four contributors are listed, so the residual line is required — otherwise a reader adding up the visible terms gets `+1.065` against a stated `+1.13`, and a number that does not reconcile teaches them to stop checking.

Two guards, because a confident-looking rating on thin evidence is worse than no rating:

- A missing feature is **excluded and the weights renormalized**, never silently treated as zero. Zero-filling would drag the score toward `관망` while the output still looked fully informed.
- Below 50% weight coverage the ticker is forced to `관망` and the missing inputs are named.

### Two rules that prevent self-deception

**Look-ahead prohibition.** A feature computed at time `t` never uses data timestamped at or after `t`. Any function computing features takes an explicit `as_of` parameter — a function that reads "the latest" data without a boundary is a bug, not a convenience. News is joined on publication timestamp, and news published during a session is assumed tradeable only at the *next* session's open.

**Normalization.** Every feature is a 252-trading-day rolling z-score per ticker. Raw absolute values are never compared across tickers.

---

## Repository map

```
market-briefing/
├── README.md                  ← you are here
├── SPEC.md                    full design spec — the authoritative document
├── PREREGISTRATION.md         evaluation criteria, frozen before data collection
├── MANUAL-TASKS.md            work only Ricky can do (keys, watchlist, golden set)
├── API-KEYS.md                how to issue each credential, provider by provider
├── CLAUDE.md                  rules for the AI agent working in this repo
│
├── config/
│   ├── watchlist.yaml         tickers to track          🟡 needs Ricky
│   ├── aliases.yaml           ticker alias dictionary   🟡 needs Ricky
│   ├── rating.yaml            rating weights & cut points  🟡 needs calibration
│   ├── news_feeds.yaml        RSS sources — coverage equals this file
│   ├── sector_mapping.yaml    US ETF ↔ KR sector
│   ├── models.yaml            which model per stage
│   └── delivery.yaml          output channels — the ONLY place one may be declared
│
├── src/
│   ├── util/
│   │   ├── session.py         ✅ UTC/KST, trading days, look-ahead boundary
│   │   └── config.py          ✅ config loading + hand-editing safeguards
│   ├── collectors/
│   │   ├── validate.py    ✅ the four checks every collector must pass
│   │   ├── kr_price.py    ✅ pykrx daily OHLCV
│   │   ├── kr_flow.py     ✅ investor flows, short interest, cap, fundamentals
│   │   ├── kr_news.py     ✅ outlet RSS, collected on a measured cadence
│   │   ├── us_price.py    ✅ Tiingo daily OHLCV (morning preview)
│   │   ├── us_price_alpaca.py ✅ Alpaca SIP, the evening source of record
│   │   └── macro.py       ✅ FRED regime series
│   ├── entity/
│   │   └── resolve.py         ✅ alias-driven ticker matching + ambiguous bucket
│   ├── embed/                 ⬜ dedup + relevance — SPEC §12 step 6, not started
│   ├── features/
│   │   ├── compute.py         ✅ 5 of 7 weighted features
│   │   └── normalize.py       ✅ 252-trading-day rolling z-score
│   ├── llm/
│   │   ├── prompts/
│   │   │   └── v1_synthesis.md ✅ the AI 총평 prompt
│   │   ├── adapter.py         ✅ vendor-neutral layer (litellm), scoring
│   │   └── score.py           ✅ Stage-2 scorer — used by the bake-off only; not wired into the daily run yet
│   ├── report/
│   │   ├── rating.py          ✅ the 7-point directional rating
│   │   ├── consistency.py     ✅ commentary checked against the rating — not yet called from render.py
│   │   └── render.py          ✅ markdown rendering, sections ④⑤⑧ still absent
│   ├── notify/                ✅ vault, email adapters
│   └── eval/
│       └── bakeoff.py         ✅ SPEC §7.4 bake-off harness
│       ├── ic.py             ✅ IC / ICIR / quantile spread (§8.4)
│       └── shadow_portfolio.py ✅ §8.5 vs KODEX 200
│
├── scripts/
│   ├── config_helper.py       ✅ find / scaffold / audit for the hand-written config
│   ├── backfill.py            ✅ 3-year initial load
│   ├── collect_daily.py       ✅ morning/evening run driver
│   └── golden.py              ✅ golden-set sampling, triage, labelling, verification
├── .github/workflows/         ✅ collect-news.yml, report.yml
├── tests/                     602 offline tests, 9 network
└── data/                      gitignored except data/raw/, data/ratings/, data/bakeoff/, data/golden/ — see .gitignore for why each is committed
```

### What the built modules actually do

**`src/util/session.py`** — Everything is stored in UTC and displayed in KST. It also carries a removal-only correction for two 2026 KRX closures the calendar library still reports as sessions (지방선거일, 제헌절), found by diffing against KRX itself and re-derived by a network test. Market sessions come from `pandas_market_calendars`, never a hardcoded holiday list, which matters because Korean lunar holidays (Seollal, Chuseok) shift every year. The US close is *derived*, so it correctly lands at 05:00 KST during daylight saving and 06:00 KST outside it without either number appearing in the code. `next_tradeable_open()` implements the look-ahead rule.

**`src/collectors/validate.py`** — Every collector must pass four checks, written before its fetching logic: schema (column names and dtypes), missing-value ratio against a declared threshold, trading-day continuity with holidays excluded, and at least one hardcoded known value. Results are *reported*, not just raised — a failing collector records the failure and lets the pipeline continue, so a partial report still gets published with the gap named in its header.

**`src/report/rating.py`** — Turns feature z-scores into a seven-point directional rating plus the decomposition that produced it. Pure arithmetic and fully reproducible; the same inputs always give the same rating, which an LLM-authored one could not guarantee.

**`src/report/consistency.py`** — The guard on the AI commentary. Finds every rating label in the LLM's prose, attributes it to a ticker through the alias dictionary, and compares it against the computed rating. The interesting part is what it *doesn't* match: `순매수`, `매수세`, `매도호가` are ordinary market vocabulary, not rating claims, so a label glued to an adjacent Hangul syllable is rejected — with an allowlist for trailing particles (`매수는`, `매수로`), which are a closed class where compound nouns are not.

**`scripts/config_helper.py`** — Operator tooling for the two config files Ricky writes by hand. `find` resolves company names to tickers and KRX sectors; `scaffold` pre-fills the mechanical half of an alias entry into a gitignored worksheet; `audit` scores `aliases.yaml` against the news already collected and prints matched headlines. It deliberately never writes `config/aliases.yaml` and never proposes an `aliases` value — only `exclude` and `ambiguous_parents`, which can lose coverage but cannot misattribute.

**`src/util/config.py`** — Loading is also validation. It rejects the mistakes that are easy to make by hand and impossible to spot later: an alias claimed by two different tickers, an alias that also appears in its own exclude list, unquoted tickers, and out-of-order rating cut points.

> **The unquoted-ticker trap.** YAML reads a bare leading-zero number as octal, so an unquoted `000660` (SK하이닉스) silently becomes the integer `432`. `005930` survives only because `9` is not a valid octal digit — which makes the failure inconsistent and very easy to miss. Always quote tickers; the loader rejects unquoted ones with an explanatory error.

---

## Getting started

```bash
uv sync                          # install dependencies

uv run pytest -m "not network"   # run the test suite (the default run)
uv run ruff check . && uv run ruff format .

cp .env.example .env             # then fill in your API keys
```

Tests that hit the network are marked `@pytest.mark.network` and excluded by default. Imports resolve from the repository root: `from src.util.session import ...`.

---

## Where the project stands

Progress is tracked against the thirteen steps in [SPEC §12](SPEC.md), so it can
be checked against the repository rather than taken on trust.

| Step | | Status |
|---|---|---|
| 1 | Repo + SPEC / PREREGISTRATION / CLAUDE | ✅ |
| 2 | `watchlist.yaml` + `aliases.yaml` | ✅ 31 KR + 40 US tickers; 31 alias entries |
| 3 | Collectors + validation tests | ✅ 6 collectors, 602 offline tests, 9 network |
| 4 | **3-year backfill into `data/raw/`** | ✅ macro · us_price · kr_price · kr_flow, 2023-08-03 → 2026-08-07 |
| 5 | Entity resolution + ambiguous ratio | ✅ **ambiguous 7.9%** of 1,013 articles, 2026-08-11 (threshold 30%) |
| 6 | Embedding pipeline (dedup + relevance) | ⬜ dependency resolved 2026-08-12, see [notes/step6-plan.md](notes/step6-plan.md). Not blocking anything — deprioritized behind the §8.5 gate measurement |
| 7 | Golden set — 100 hand-labeled articles | ✅ **done**, `relevance` fully re-labelled 2026-08-12 after a mid-run definition change |
| 8 | Model adapter + bake-off | ✅ **done 2026-08-12** — `gpt-5.1` selected, replaced by `gpt-5.4` 2026-08-13, see [Current status](#current-status) above |
| 9 | Feature computation | ✅ 5 of 7 rating features, 0.75 of 1.10 weight |
| 10 | Report renderer + delivery | ✅ vault + email live; 10 briefings rendered, 2026-08-03..2026-08-12 |
| 11 | Daily collection + report workflow | ✅ **full cloud round trip 2026-08-06** — 5 collectors, render, email, commit |
| 12 | Schedule burn-in | 🟡 **in progress** — cron fires unattended; delivery 42.6% of declared runs (2026-08-03..07) |
| 13 | Two-week gate | ⬜ **clock starts 2026-08-12, read 2026-08-26** — pinned in PREREGISTRATION §8.5 |

**The whole deterministic path now exists and runs unattended — collect,
resolve, compute, rate, render, deliver.** What is missing is step 6, the
embedding pipeline — steps 7 and 8 (golden set, bake-off) finished 2026-08-12,
which the rating does not need to produce a number, but which `news_polarity`
does.

### The backfill is in and the z-scores are real

`kr_flow` finished on 2026-08-06: **22,528 rows, 31 tickers, 728 sessions**
spanning 2023-08-03 to 2026-08-03, with no missing values. The flow identity —
foreign + institutional + retail + other-corporate net buying summing to zero,
which is true by construction and is the cheapest detector of a mis-mapped
investor column — holds on **every one of the 22,528 rows**.

With the history in place the features stopped being `NaN` — counts measured the
same day, and they grow by one session per ticker per KRX day since:

| feature | raw | z-scored |
|---|---:|---:|
| `foreign_flow_5d` | 22,383 | 14,067 |
| `inst_flow_5d` | 22,383 | 14,067 |
| `short_ratio` | 22,528 | 14,408 |
| `rel_strength_20d` | 18,368 | 11,816 |
| `valuation_band` | 0 | 0 |

> [!note]
> `short_ratio`'s counts predate the 2026-08-13 disclosure-lag fix below and are
> left as measured. The feature now carries the balance disclosed by each
> session rather than the one recorded against it, which costs the first three
> sessions of each ticker's history — 31 rows across the archive, not enough to
> move the column above.

`valuation_band` is empty because it needs 756 sessions and the window is still
short of that — see the parked decisions below. 454910 is exactly **40 sessions**
behind every other ticker because it listed on 2023-10-05, 40 sessions after the
window opens, which is history that does not exist rather than history that is
missing. The session totals move every KRX day; the 40 does not.

`scripts/backfill.py` stays resumable. KRX throttles by address — roughly 250
requests earns an HTML error page for a few hours — and `kr_flow` costs 124
requests per year of history, so a re-run lands over several windows.

### Two decisions parked for Ricky

**`rev_4w` has no data source.** SPEC §5 defines it as the 4-week change in
*consensus* EPS — forward analyst estimates. pykrx's `EPS` is trailing, and
substituting it would produce a number that looks like the feature and is not.
Doing it properly needs an estimates vendor (FnGuide, QuantiWise). Weight 0.15;
absent, and `rate()` renormalizes.

**`valuation_band` will turn itself on, and the four-year backfill only buys
time.** It is a 756-session PBR percentile; the stored window held 732 sessions
as of 2026-08-07 and grows by one per KRX session, so it crosses 756 on
**2026-09-11** for 30 tickers and **2026-11-12** for 454910 with no backfill at
all. Extending the backfill by a year turns it on about four weeks earlier, for
124 KRX requests. Weight 0.05 — which is what makes waiting the obvious default.

### Why step 9 came before steps 6–8, and step 10 before them too

`news_polarity` (0.20) is the only rating feature that needs an LLM. The five
built at step 9 carry 0.75 of 1.10 total weight, above `min_weight_coverage:
0.5`, so a real deterministic rating is reachable without the embedding
pipeline, the golden set or the bake-off. That is why the order was inverted.

The [2026-08-06 review](notes/review-2026-08-06.md) extended the same reasoning
to step 10. PREREGISTRATION splits the two-week validation into metrics needing
**steps 10–12** — pipeline integrity, data consistency, entity accuracy, whether
the briefing is read — and metrics needing steps 6–8. Building the LLM stages
first leaves that clock unstarted.

It also closes a live defect. `check_feed_continuity` correctly detected that
one feed (`etnews_economy`) had rolled 5.3 hours past the last stored article,
and reported it into GitHub Actions logs that nothing reads. CLAUDE.md requires
missing data to appear **in the report header, not only in logs** — so the
header is what fixes it, and the header arrives with step 10.

---

## What's blocking progress

These are Ricky's, in the order they will be needed. Full detail in
[MANUAL-TASKS.md](MANUAL-TASKS.md).

| # | Task | Est. time | Blocks |
|---|---|---|---|
| 1 | Sign off on the ~$4.44 one-time gate-measurement spend | judgment | PREREGISTRATION §8.5's 4th 2-week-gate criterion — [notes/gate-inter-model-plan.md](notes/gate-inter-model-plan.md) |
| 2 | `rev_4w` data source decision | judgment | The one rating feature besides `news_polarity` with no source |
| 3 | `config/rating.yaml` calibration | 30 min | Trustworthy ratings (do *after* the 2-week gate) |
| 4 | KIS application | 15 min | Real-time quotes only; blocks nothing today |

Credentials, the `.env` fixes, the watchlist, the Alpaca switch, the alias
dictionary, the model bake-off, and the `sentence-transformers` dependency
decision are all done. **Nothing blocks step 6 any more, and step 6 no longer
blocks anything either** — `src/entity/resolve.py` alone already produces
92–148 (article, ticker) pairs/day, inside SPEC §6.1's own 60–100 target band.
The next priority is the gate measurement above, not step 6; see
[notes/step6-plan.md](notes/step6-plan.md)'s "Priority" section for why the
order flipped 2026-08-13.

The `relevance` re-labelling that used to be the open item here is finished
too, and it earned its place: three models failing the same correlation bar
simultaneously is what surfaced that the golden set's own definition had gone
stale, not the models. `forwardness` still disagrees with itself twice as much
as the other four dimensions — read the floor in
[PREREGISTRATION §8.3](PREREGISTRATION.md) before ranking models on it, and
note it predates the `relevance` redo and needs re-measuring via
`golden recheck` (earliest 2026-08-13 09:00 KST).

**On the defects found in the [2026-08-07 review](notes/review-2026-08-07.md).**
H1 (rating archive) and H2 (phantom weights) were fixed on 2026-08-08. M1
(news-failure reporting) is now partly closed: a failed check in the standalone
news run exits non-zero, so GitHub mails the failure instead of leaving it in a
log nobody reads — the detail still requires opening the run. L1 remains and
costs nothing.

---

## Evaluation

Criteria were frozen in [PREREGISTRATION.md](PREREGISTRATION.md) on 2026-08-02, before any data existed. Revisions are logged there with the date, the reason, and whether data had already been seen.

| Gate | Criteria | If not met |
|---|---|---|
| 2 weeks<br>2026-08-12 → 2026-08-26 | Pipeline uninterrupted, zero data-consistency errors, `ambiguous` < 30%, inter-model polarity correlation > 0.5 | Halt signal work, repair the measurement layer |
| 3 months<br>2026-08-13 → 2026-11-13 | ICIR > 0.3, shadow portfolio beats KODEX 200 buy-and-hold | Discard the signal logic, redesign |
| 6 months | Above holds after fees, transaction tax, and slippage | End the project, switch to indexing |

**Two weeks cannot measure whether the signal works.** Separating a 55% hit rate from 50% needs roughly 800 independent observations; two weeks of 30 tickers yields an effective 60–100 once market beta eats the cross-sectional independence. So the early gate measures *measurement stability* — whether three different models scoring the same articles agree with each other — rather than predictive accuracy. If they disagree, the scores are model-specific noise and there is nothing worth validating yet.

**"Uninterrupted" means something specific, and it is written down.** Every run that fires records a run file, no unexplained `check_feed_continuity` failure, no two consecutive runs failing validation for the same cause — [§8.5](PREREGISTRATION.md) defines it in full and calibrates it against known incidents. The middle condition became measurable on 2026-08-11: a feed that does not answer is now judged by the next run that does, so the criterion counts causes the pipeline can fix rather than the weather it runs in. Delivered-run coverage is reported alongside the decision but is not a criterion: GitHub's scheduler drops the runs, not this pipeline. The inter-model criterion needs the step-8 bake-off, so the gate is read in two parts and passes only when both do.

**"Zero data-consistency errors" means contradiction, not absence.** Four checks count — `schema`, `structural_invariants`, `flow_identity`, and the Alpaca-vs-Tiingo close comparison — read off the committed report headers. `missing_ratio` and `trading_day_continuity` are excluded with reasons given in [§8.5](PREREGISTRATION.md), the second because 25 of them once fired from a single Tiingo rate-limit. The list was written before the count was known; over the record to date it stands at zero.

---

## Documents

| File | Purpose |
|---|---|
| [SPEC.md](SPEC.md) | Full design spec. The authoritative document. |
| [PREREGISTRATION.md](PREREGISTRATION.md) | Evaluation criteria, frozen before collection. |
| [MANUAL-TASKS.md](MANUAL-TASKS.md) | Work only Ricky can do, ordered by what it blocks. |
| [RESEARCH.md](RESEARCH.md) | Survey of published LLM trading agents — what they claim, what survives scrutiny, what applies here. |
| [API-KEYS.md](API-KEYS.md) | Signup walkthrough for every credential, with the per-provider traps. |
| [CLAUDE.md](CLAUDE.md) | Operating rules for the AI agent working in this repo. |
