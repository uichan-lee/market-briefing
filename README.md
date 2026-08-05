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

**Foundation stage.** The plumbing that everything else depends on is built and tested. No data has been collected yet — `data/` is empty and no collector has ever run.

| Component | Status | Notes |
|---|---|---|
| Design docs (SPEC, PREREGISTRATION, MANUAL-TASKS) | ✅ Done | Evaluation criteria frozen 2026-08-02 |
| Python project, testing, linting | ✅ Done | `uv` + `pytest` + `ruff`, 233 tests passing |
| Time & market sessions (`src/util/session.py`) | ✅ Done | Trading days, DST, look-ahead boundary |
| Collector validation framework (`src/collectors/validate.py`) | ✅ Done | The four checks every collector must pass |
| Config loading & safeguards (`src/util/config.py`) | ✅ Done | Rejects alias collisions, unquoted tickers |
| Directional rating (`src/report/rating.py`) | ✅ Done | 7-point scale + rationale; weights need calibration |
| AI commentary guard (`src/report/consistency.py`) | ✅ Done | Drops the 총평 if it contradicts the computed rating |
| Config files | 🟡 Partial | `watchlist.yaml` done — 19 KR + 14 US, all verified against live data. `aliases.yaml` is **the only thing blocking Claude** |
| API credentials | ✅ Done | KRX and Alpaca verified live; all 9 values mirrored into Actions secrets. Only KIS outstanding, and it blocks nothing yet |
| `kr_price` collector (pykrx OHLCV) | ✅ Done | Four checks + committed fixture; known value cross-checked against Naver |
| `macro` collector (FRED) | ✅ Done | 6 series verified live; known value cross-checked against Treasury |
| `kr_news` collector (outlet RSS) | ✅ Done | 15 feeds via Actions, twice hourly in session; ~950 articles/poll across 8 outlets |
| `us_price` collector (Tiingo) | ✅ Done | Four checks + committed fixture; known value cross-checked against Yahoo Finance |
| `us_price` over Alpaca | ✅ Done | The US source in use. SIP confirmed on the free plan; **48 symbols in 2 requests** where Tiingo needed 48 |
| `kr_flow` collector (pykrx) | ✅ Done | Investor flows, short interest, cap, fundamentals — **the 55% of rating weight KRX was gating**. Six checks incl. an accounting identity and a cross-collector price check |
| `us_filings`, `kr_filings` | ⬜ Not started | SEC EDGAR and DART |
| Entity resolution | ⬜ Not started | |
| Embedding pipeline (dedup + relevance) | ⬜ Not started | |
| Golden set (100 hand-labeled articles) | ⬜ Not started | Ricky's task, blocks model selection |
| LLM adapter + scoring + bake-off | ⬜ Not started | |
| Feature computation | ⬜ Not started | |
| Report renderer + delivery | ⬜ Not started | |
| GitHub Actions workflow | ⬜ Not started | |

**Korean news collection is live and unblocked** — `kr_news` reads 15 outlet RSS feeds via GitHub Actions — twice an hour through the KRX session, hourly otherwise — needing no credential at all. Because RSS cannot be backfilled, that clock only starts once `collect-news.yml` is on the default branch.

**The KRX blocker is cleared.** `data.krx.co.kr` went members-only and returned HTTP 400 `LOGOUT` without a session, which withheld investor flows, short interest, market cap and fundamentals — **55% of the rating weight**, enough to force every ticker to `관망`. A login now works and all six gated endpoints were verified live on 2026-08-04 ([API-KEYS.md §0](API-KEYS.md)). `kr_flow` is now built on top of it and passing.

**What blocks Claude now is `config/aliases.yaml`.** The watchlist is filled — 19 Korean tickers including the 두산, 삼성, 한화 and LG clusters that make Korean entity resolution hard, plus 14 US names. Each Korean ticker needs an alias entry; `scripts/config_helper.py` generates the mechanical half and audits the result against the news already collected — see [MANUAL-TASKS.md §3](MANUAL-TASKS.md).

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
│   │   │   ├── validate.py    ✅ the four checks every collector must pass
│   │   ├── kr_price.py    ✅ pykrx daily OHLCV
│   │   ├── kr_news.py     ✅ outlet RSS, collected on a measured cadence
│   │   ├── us_price.py    ✅ Tiingo daily OHLCV
│   │   └── macro.py       ✅ FRED regime series
│   ├── entity/                ⬜ ticker matching
│   ├── embed/                 ⬜ dedup + relevance
│   ├── features/              ⬜ computation + normalization
│   ├── llm/
│   │   ├── prompts/
│   │   │   └── v1_synthesis.md ✅ the AI 총평 prompt
│   │   └── adapter.py         ⬜ vendor-neutral layer, scoring, synthesis
│   ├── report/
│   │   ├── rating.py          ✅ the 7-point directional rating
│   │   ├── consistency.py     ✅ commentary checked against the rating
│   │   └── render.py          ⬜ markdown rendering
│   ├── notify/                ⬜ vault, email, webhook adapters
│   └── eval/                  ⬜ golden set, bake-off, IC, shadow portfolio
│
├── scripts/
│   └── config_helper.py       ✅ find / scaffold / audit for the hand-written config
├── tests/                     272 tests, all offline
└── data/                      gitignored except data/raw/kr/news/ — RSS has no backfill
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

## What's blocking progress

These are Ricky's, in the order they unblock work. Full detail in [MANUAL-TASKS.md](MANUAL-TASKS.md).

| # | Task | Est. time | Blocks |
|---|---|---|---|
| 1 | `config/aliases.yaml` — one entry per KR ticker (`scaffold` → `audit`) | 60–75 min | Entity resolution, all news features |
| 2 | Golden set — 100 hand-labeled articles | ~2 hours | Model selection |
| 3 | Bake-off decision | 30 min | Scoring model choice |
| 4 | `config/rating.yaml` calibration | 30 min | Trustworthy ratings (do *after* 1–2 weeks of real data) |

Credentials, the `.env` fixes, the watchlist and the Alpaca switch are done — KRX verified against all six gated endpoints, SPY's known value cross-checked against Yahoo Finance, every secret mirrored to Actions, and 19 KR + 14 US tickers confirmed to return real data through the collectors.

Task 2 is the one that will feel skippable. It is the only step involving no code, and skipping it makes the bake-off impossible — model selection then ends at "Claude seemed good."

---

## Evaluation

Criteria were frozen in [PREREGISTRATION.md](PREREGISTRATION.md) on 2026-08-02, before any data existed. Revisions are logged there with the date, the reason, and whether data had already been seen.

| Gate | Criteria | If not met |
|---|---|---|
| 2 weeks | Pipeline uninterrupted, zero data-consistency errors, `ambiguous` < 30%, inter-model polarity correlation > 0.5 | Halt signal work, repair the measurement layer |
| 3 months | ICIR > 0.3, shadow portfolio beats KODEX 200 buy-and-hold | Discard the signal logic, redesign |
| 6 months | Above holds after fees, transaction tax, and slippage | End the project, switch to indexing |

**Two weeks cannot measure whether the signal works.** Separating a 55% hit rate from 50% needs roughly 800 independent observations; two weeks of 30 tickers yields an effective 60–100 once market beta eats the cross-sectional independence. So the early gate measures *measurement stability* — whether three different models scoring the same articles agree with each other — rather than predictive accuracy. If they disagree, the scores are model-specific noise and there is nothing worth validating yet.

---

## Documents

| File | Purpose |
|---|---|
| [SPEC.md](SPEC.md) | Full design spec. The authoritative document. |
| [PREREGISTRATION.md](PREREGISTRATION.md) | Evaluation criteria, frozen before collection. |
| [MANUAL-TASKS.md](MANUAL-TASKS.md) | Work only Ricky can do, ordered by what it blocks. |
| [API-KEYS.md](API-KEYS.md) | Signup walkthrough for every credential, with the per-provider traps. |
| [CLAUDE.md](CLAUDE.md) | Operating rules for the AI agent working in this repo. |
