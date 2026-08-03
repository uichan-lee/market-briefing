# Daily Market Briefing Pipeline — Design Spec v0.5

> [!abstract] Purpose of this document
> The input spec fed directly to Claude Code. The goal is to **fix the output shape and evaluation criteria before writing code**, so we don't waste effort collecting data that never gets used, and don't move the goalposts after the fact (= self-deception).

> [!info] Change log
> **v0.2** — Removed LLM from Stage 1 (replaced with embeddings) / model adapter layer / golden set + bake-off (§7) / entity resolution (§4) / re-reporting detection switched to embedding-based deduplication
> **v0.3** — Redesigned the delivery layer (§2.0) as a swappable adapter. Dropped the Telegram dependency; Obsidian vault + email are now the defaults
> **v0.4** — §8 split out into `PREREGISTRATION.md` and frozen before collection began; §8 here is now a pointer. §10 updated to match the built layout (`src/util/`, `src/collectors/validate.py`, toolchain files)
> **v0.5** — Added §2.2⑧ AI 총평 (the synthesis half of the Stage 3 §6.1 always specified) with a deterministic guard, and §2.2⑨ medium-term regime. §5 gained three long-horizon per-ticker features. §2.3 separates stable section IDs from display order. Made while `data/raw/` was still empty, so the composite's changed input is logged in PREREGISTRATION §R rather than contaminating an evaluation in flight

> [!note] Language policy
> Repository documents, code, and commit messages default to English. Exceptions are domain data (ticker/company names, the alias dictionary, Korean-language few-shot prompt examples) and this document. This SPEC was translated to English by Claude Code via `/plan translate SPEC.md to English, preserve all structure and numbering`.

---

## 0. Design principles (do not change)

1. **The LLM does not make judgment calls.** Its only role is turning news into structured numbers. The briefing does state a directional rating (§2.2⑥), but that rating is computed from the numbers by a deterministic rule — never written as free-form LLM text. This is what keeps it reproducible, and therefore evaluable under PREREGISTRATION §8.4.
2. **Don't use an LLM where a deterministic solution exists.** Anything solvable with string matching, embedding similarity, or statistics is solved that way. The LLM is reserved for the small number of cases that genuinely require judgment.
3. **Raw data is stored immutably.** Collected raw data is loaded into date-partitioned storage before any processing. This becomes the backtest dataset three months from now.
4. **Models must be swappable.** Vendor-specific code must never leak outside the adapter.
5. **No trade-execution automation.** A human pulls the trigger for at least the first six months.
6. **Evaluation criteria are pre-registered.** §8 is committed before looking at the data; any later revision is logged as a revision history.

---

## 1. Execution schedule

| Run | Time (KST) | Coverage | Purpose |
|---|---|---|---|
| `RUN_MORNING` | 07:00 | US market close (previous day) + KR pre-market | 2 hours before KOSPI open |
| `RUN_EVENING` | 21:30 | KR market close (same day) + US pre-market | 1 hour before US open (DST) |

> [!warning] Daylight saving time
> US market close is 05:00 KST during DST and 06:00 KST outside it. Fix the schedule in UTC and determine the session with a market calendar. Use `pandas_market_calendars`.

> [!note] GitHub Actions cron delay
> On-time execution is not guaranteed and can slip tens of minutes during peak hours. If the target is 07:00, trigger at 21:20 UTC to leave margin. On run failure, retry once; if it still fails, send a failure notice through the configured delivery channels and **publish a partial report anyway** — a silent failure is the worst outcome.

---

## 2. Report template

### 2.0 Delivery layer

Output is a single markdown file. Like the model adapter, delivery is built as a **swappable adapter** — it must not be tied to a specific messenger.

```yaml
# config/delivery.yaml
channels:
  - type: vault          # default — commit to the repo, Obsidian pulls via git
    path: reports/
    commit: true
  - type: email          # mobile reading path
    to: <address>
    smtp_secret: SMTP_PASSWORD
    body: summary        # summary | full
# add later if needed: webhook (Slack/Discord/Kakao, etc.)
```

| Channel | Purpose | Implementation |
|---|---|---|
| `vault` | Full read on desktop | Actions commits to `reports/` → pulled via the Obsidian Git plugin |
| `email` | Quick scan on mobile | Sent via SMTP from Actions, using a dedicated address + app password |
| `webhook` | For later, if needed | Generic POST. Implemented but left inactive |

> [!important] How the Obsidian connection works
> Since GitHub Actions runs in the cloud, it can't write directly to the vault on the Mac. The simplest setup is to keep the repo as a folder inside the vault and pull it via the Obsidian Git plugin. That's inconvenient on mobile, so email fills that role instead.

### 2.1 Header (summary — top of email subject/body)

```
📅 2026-07-29 (Wed) 07:00 KST
S&P +0.4% | NASDAQ +0.8% | SOX +1.9% | USDKRW 1,382 (+0.3%)
▶ Today's focus: Semiconductor gap-up pressure / secondary battery outflows, day 3
▶ Holdings flagged: 2 (below)
⚠ Data missing: us_filings (retry failed)
⚠ 총평 생략: 등급과 모순 — 000660 (§2.2⑧)
```

The last line appears only when the commentary was dropped. Every degradation of the briefing is stated in the header, never only in logs.

### 2.2 Sections

Content and identity. Display order is §2.3.

**① US → KR market transmission (comes first)**

- Major indices + sector ETF daily returns (XLK, XLE, XLF, SMH, XBI...)
- SOX, DXY, USDKRW, WTI, US 10Y yield — levels and daily change
- Correspondence mapping: expected direction of US sector → KR sector
  - SMH → Semiconductors (Samsung Electronics, SK Hynix, Hanmi Semiconductor)
  - XLE → Refining/Shipbuilding
  - Russell 2000 → KOSDAQ small/mid-cap
- Each mapping is shown alongside its **trailing 60-trading-day rolling correlation**. If correlation has broken down over the window, that's a flag to disregard the signal.

> [!tip] Why this section comes first
> This is the most defensible quantitative content in the briefing. The causal direction is clearer than sentiment analysis, and the lag is real. It's also important that this section involves no LLM at all.

**② Holdings / watchlist scan**

One line per ticker, plus flags. Bold if any of the conditions below are triggered.

| Flag | Condition |
|---|---|
| `outflow` | Foreign 5-day cumulative net buying z < -1.5 |
| `inflow` | Foreign 5-day cumulative net buying z > +1.5 |
| `filing` | New DART/EDGAR filing the previous day |
| `earnings_revision` | Absolute 4-week change in consensus EPS > 3% |
| `valuation_band` | 3-year PBR band bottom 10% or top 10% |
| `news_spike` | Post-dedup mention volume z > 2.0 |
| `volatility` | 20-day realized volatility z > 1.5 |

**③ News score aggregation**

Aggregates the individual scores from the §6 schema per ticker. Displayed fields: relevance-weighted average polarity, average uncertainty, article count (post-dedup), and the headline + link of the single highest-intensity article.

> Quoted excerpts are capped at 15 words; everything else is summarized.

**④ Calendar**

Same-day/next-day earnings releases, FOMC/CPI/employment data, options expiration, KR ex-dividend dates and IPO schedules.

**⑤ Red team section**

Has the LLM generate **counterarguments only** against the conclusions from ①–④. The prompt explicitly instructs it "not to agree." This section is likely to end up the most valuable part of the briefing.

**⑥ Directional rating**

Every watchlist ticker gets a rating on a seven-point scale, with the evidence that produced it.

| Rating | Korean | Composite score |
|---|---|---|
| Strong buy | 강한 매수 | ≥ +2.0 |
| Buy | 매수 | +1.0 … +2.0 |
| Weak buy | 약한 매수 | +0.4 … +1.0 |
| Hold | 관망 | −0.4 … +0.4 |
| Weak sell | 약한 매도 | −1.0 … −0.4 |
| Sell | 매도 | −2.0 … −1.0 |
| Strong sell | 강한 매도 | ≤ −2.0 |

The composite score is a weighted sum of the §5 feature z-scores plus the aggregated news polarity from §6.2. Weights and cut points live in `config/rating.yaml`; the cut points above are starting values, and calibrating them is Ricky's call (MANUAL-TASKS §6), not something to tune until the ratings look agreeable.

> [!important] The rating is computed, not written
> An LLM never produces this rating or its rationale. The scale is derived arithmetically from numbers the LLM only ever assigned per-article, which is what makes it reproducible — and reproducibility is the precondition for evaluating it at all (PREREGISTRATION §8.4). A rating written as prose could not be backtested, correlated with forward returns, or compared across model versions.

The rationale is generated by ranking each term's contribution (`weight × z-score`) and reporting the largest ones, so it states literally what moved the number:

```
005930 삼성전자 — 매수 (+1.4)
  · 외국인 5일 순매수 z=+2.1        기여 +0.63
  · 뉴스 polarity +0.6 (기사 3건)   기여 +0.42
  · 20일 상대강도 z=+0.9            기여 +0.18
  · 밸류 밴드 3년 PBR 상위 12%      기여 −0.11
  ▶ HBM 공급 계약 확대 (2026-07-28, 한국경제) — [link]
```

Two guards, both because a confident-looking rating on thin evidence is worse than no rating:

- A ticker whose inputs are largely missing is rated `관망` with the missing inputs named, never rated confidently on whatever happened to arrive.
- A missing feature is never silently treated as zero — it is excluded from the sum and the weights are renormalized over what is present.

> [!note] The rationale is truncated, so it must show its residual
> `RatingResult.rationale()` returns only the top `max_rationale_terms` contributors (4 by default), while the headline score is the sum of **all** of them. With the committed config a reader adding up the displayed lines gets `+1.065` against a stated `+1.13`. The renderer emits a residual line whenever terms were dropped:
>
> ```
>   · 그 외 3개 항목                    기여 +0.065
> ```
>
> A number on the page that does not reconcile teaches the reader to stop checking, which defeats the reason the rationale is a decomposition in the first place.

**⑦ Shadow portfolio P&L**

Cumulative performance of a hypothetical account that traded exactly according to the ⑥ ratings. Tracked automatically, separate from the real account. This is the section that makes the rating falsifiable rather than merely opinionated.

> [!warning] Rating ≠ execution
> This system produces an opinion and stops. It places no orders, and SPEC §0 principle 5 and CLAUDE.md's absolute rule 2 continue to hold without exception. The trigger is pulled by a human, and not before the 3-month gate in PREREGISTRATION §8.5.

**⑧ AI 총평 (commentary)**

Five to eight lines synthesizing everything above into what a reader with thirty seconds needs: the day's single most decision-relevant fact, two or three tickers worth attention with the number behind each, and the one thing most likely to make today's numbers wrong.

This is the **synthesis** half of the Stage 3 already specified in §6.1 — the red-team section ⑤ has always been the other half. The prompt lives at `src/llm/prompts/v1_synthesis.md`, versioned per §6.3.

Three properties keep this inside CLAUDE.md's absolute rule 3, and all three are load-bearing:

| Property | Why it matters |
|---|---|
| Input is the **rendered deterministic sections**, never raw articles | It cannot re-score news. Scoring happened at §6.2 and only its output arrives here |
| Output passes `src/report/consistency.py` before publication | Every rating label in the prose is compared against ⑥'s computed rating for that ticker |
| Nothing downstream reads it | Not a feature, not a score, not the shadow portfolio, not a PREREGISTRATION metric |

On contradiction the section is **dropped** and the reason is written into the header (`⚠ 총평 생략: 등급과 모순 — 000660`). Publishing an opinion known to disagree with the computed rating is worse than publishing neither, and CLAUDE.md requires a partial report over no report.

> [!important] The guard and the prompt are a matched pair
> The seven rating labels are reserved vocabulary in the prompt — ordinary market movement must be written as 순매수 / 순매도 / 수급 유입 / 수급 이탈. The checker relies on this: without it, `매수` appears in normal Korean market prose constantly and the guard would drop the section daily. A checker that fires every day is one nobody reads. Changing either file requires re-reading the other.

The section carries a permanent footer marking it as LLM-authored and outside evaluation, so it is never mistaken for the computed part of the briefing.

**⑨ 중장기 국면 (medium-term regime)**

The rest of the briefing is same-day. This section supplies the horizon that ①–⑧ lack — and it contains no LLM, like ①.

| Indicator | Source | Definition |
|---|---|---|
| `yield_curve_10y2y` | FRED | US 10Y − 2Y, level and 60-day change |
| `dxy_trend_120d` | FRED | 120-day trend of the dollar index |
| `usdkrw_trend_120d` | FRED | 120-day trend of USD/KRW |
| `wti_trend_120d` | FRED | 120-day trend of WTI |
| `us_sector_rotation_120d` | US price collector | each sector ETF's 120-day return − SPY, mapped to KR sectors via `config/sector_mapping.yaml` |

> [!important] Why the regime is a section and not a rating input
> These indicators are deliberately **excluded from the ⑥ composite.** PREREGISTRATION §8.4 evaluates on sector-excess return, and a macro term is common to every ticker, so it cancels in the excess by construction — it could add variance to the composite but never IC. The medium-term signal that *is* cross-sectional lives in §5 as per-ticker features (`rel_strength_120d`, `flow_persistence_60d`, `rev_trend_12w`) and enters ⑥ there.

> [!warning] There is no long-horizon news feature, and there never will be
> Naver's search API caps result count and paging depth, so historical news cannot be retrieved in bulk (API-KEYS.md §2). The news corpus only accumulates forward from the day collection starts. Every medium-term signal in this project is therefore price, flow, or macro — which has the side benefit of keeping this section LLM-free.

### 2.3 Render order

Section IDs ①–⑨ are **stable identifiers**, referenced from `src/report/rating.py`, `config/rating.yaml`, `MANUAL-TASKS.md`, `PREREGISTRATION.md` §8.4/§R, and both READMEs. They are not renumbered when sections are added, so ID order and display order are separate things.

```
헤더 → ⑧ AI 총평 → ① 미국→한국 전이 → ⑨ 중장기 국면 → ② 종목 스캔
     → ③ 뉴스 집계 → ④ 캘린더 → ⑥ 방향성 등급 → ⑤ 반증 → ⑦ 섀도 P&L
```

⑧ is displayed first and **generated last** — it consumes every other section as input. ⑤ sits after ⑥ for the same reason it exists: a counterargument is only useful once the reader has seen the claim it attacks.

---

## 3. Data source spec

> [!warning] Rate limits and free-tier terms change frequently
> The table below reflects the state at project start. Re-check current documentation when signing up for each service, and update this table with what you find.

### 3.1 Korea

| Source | Purpose | Auth | Library | Notes |
|---|---|---|---|---|
| pykrx | OHLCV, net buying by investor type, short-interest balance, market cap/PER/PBR | None | `pykrx` | Based on KRX scraping. Sleep required between calls |
| DART OpenAPI | Filing lists, financial statements | API key (free) | `dart-fss` or direct | Daily call limit applies |
| KIS Open API | Real-time quotes, balances | App key/secret | `python-kis` | Mock trading environment provided. **Read-only in stage 1** |
| Naver News API | Ticker news, mention volume | Client ID/secret | Direct | Limited number of search results |

> [!important] A structural edge in the Korean market
> Daily net buying by investor type (foreign/institutional/retail) doesn't exist as a data source in the US. This pipeline's most differentiated feature comes from here. It's especially notable as a **signal obtained without an LLM**.

### 3.2 United States

| Source | Purpose | Auth | Notes |
|---|---|---|---|
| SEC EDGAR | Full text of 8-K/10-Q/10-K, Form 4 insider trading | None (User-Agent required) | Full-text search API available |
| FRED | Interest rates, FX, macro | API key (free) | `fredapi` |
| yfinance | OHLCV backfill | None | Unofficial. **Do not depend on it in production**; backfill only |
| Alpaca / Tiingo | OHLCV, news | API key (free tier) | Pick one to use as the production quote source |
| GDELT | Global news volume | None | Useful for computing mention-volume z-scores |

### 3.3 Storage layout

```
data/
  raw/
    kr/price/2026-07-29.parquet
    kr/investor_flow/2026-07-29.parquet
    kr/news/2026-07-29.jsonl
    us/price/2026-07-29.parquet
    us/filings/2026-07-29.jsonl
  embeddings/
    2026-07-29.parquet        # article embeddings (for re-report detection / relevance filtering)
  features/
    2026-07-29.parquet
  scores/
    2026-07-29__{model_id}__{prompt_version}.jsonl
reports/
  2026-07-29-morning.md
```

`raw/` is **never overwritten**. On a re-run, save separately with a `-v2` suffix and keep the original.
`scores/` files embed the model ID and prompt version in the filename — this is the comparison unit for the §7 bake-off.

---

## 4. Entity Resolution

> [!danger] If this step fails, everything downstream is meaningless
> Ticker identification in Korean news is structurally hard. Don't hand this step to an LLM — solve it deterministically.

### 4.1 Problem types

| Type | Example |
|---|---|
| Group name ↔ affiliate | "한화" (Hanwha) → 한화 / 한화솔루션 / 한화에어로스페이스 / 한화오션 |
| Holding company ↔ operating company | "LG" → LG (holding co.) vs LG전자 (LG Electronics) vs LG화학 (LG Chem) |
| Preferred shares | "삼성전자우" (Samsung Electronics Preferred) is a separate ticker |
| Abbreviations/aliases | "하이닉스", "SK하이닉스", "005930" |
| Homonyms | "한섬" (Hanssem, the company) vs the common noun, "미래에셋" (Mirae Asset) has multiple tickers |
| Industry mentions | "semiconductor market conditions" — not a specific ticker |

### 4.2 Resolution approach

1. Explicitly maintain a per-ticker alias dictionary in `config/aliases.yaml`. No auto-generation — this is manually managed.
2. Primary matching: exact alias match + ticker code match
3. Ambiguous cases (only a group name appears, etc.) are **not matched — they go into the `ambiguous` bucket.** Better to drop a match than get it wrong.
4. Log the `ambiguous` ratio in the report every day. If it exceeds 30%, augment the alias dictionary.

---

## 5. Feature definitions

All features are normalized as a **252-trading-day rolling z-score** per ticker's time series. Absolute values cannot be compared across tickers.

$$z_{i,t} = \frac{x_{i,t} - \mu_{i,t-252:t-1}}{\sigma_{i,t-252:t-1}}$$

| Feature | Definition |
|---|---|
| `foreign_flow_5d` | 5-day cumulative foreign net buying ÷ 5-day cumulative trading value |
| `inst_flow_5d` | Same, for institutional investors |
| `short_ratio` | Short-interest balance ÷ shares outstanding |
| `rev_4w` | 4-week change in consensus EPS |
| `rel_strength_20d` | Ticker 20-day return − sector 20-day return |
| `rv_20d` | 20-day realized volatility (stdev of log returns × √252) |
| `news_volume_z` | z-score of daily article count **after deduplication** |
| `us_kr_beta_60d` | 60-day rolling beta against the corresponding US sector ETF |

**Medium-term features (added v0.5).** Everything above tops out at 4 weeks for fundamentals and 20 days for price; 252 days appeared only as the normalization window, never as a signal. These three supply the missing horizon and are cross-sectional, so unlike the §2.2⑨ regime indicators they can enter the ⑥ composite and be measured by IC.

| Feature | Definition |
|---|---|
| `rel_strength_120d` | 120-day return − sector 120-day return |
| `flow_persistence_60d` | 60-day cumulative foreign net buying ÷ 60-day cumulative trading value |
| `rev_trend_12w` | 12-week change in consensus EPS — the slower companion to `rev_4w` |

All three are computable from the day the 3-year backfill lands (§12 step 4), since pykrx and DART both serve history. Their weights sit **commented out** in `config/rating.yaml` until `src/features/compute.py` produces them: `rate()` renormalizes over present features, so activating a weight for a feature that does not yet exist would lower every ticker's `weight_coverage` and could trip the 0.5 floor into spurious `관망`.

---

## 6. News processing pipeline

### 6.1 Stage structure (redesigned in v0.2)

```
1,000–2,000 raw articles
    │
    ├─ Stage 0: Entity matching (§4)         — deterministic, zero cost
    │      ↓
    ├─ Stage 1: Embedding (bge-m3, local)     — zero cost, 100% reproducible
    │      ├─ Re-report detection: cosine similarity > 0.92 → keep only 1 cluster representative
    │      └─ Relevance filter: cut the bottom tail of similarity against ticker profile sentences
    │      ↓
    │   top 60–100 articles
    │      ↓
    ├─ Stage 2: LLM 5-dimensional scoring     — the only stage that incurs LLM cost
    │      ↓
    └─ Stage 3: LLM report synthesis + red-team section
```

> [!tip] What changed from v0.1
> The relevance filter and re-report detection moved from the LLM to embeddings. Three reasons:
> 1. **Cost**: LLM filtering of 1,500 articles/day → $0
> 2. **Reproducibility**: embeddings are deterministic. Even at `temperature=0`, the LLM isn't perfectly identical run to run
> 3. **Re-report detection accuracy**: for Korean media's re-reporting pattern, sentence-level similarity catches it more accurately than LLM judgment does
>
> The embedding model is `BAAI/bge-m3` (handles Korean/English simultaneously, MIT license). Runs locally on the Mac via `sentence-transformers`. The 0.92 similarity threshold is an initial value, to be tuned against the §7 golden set.

### 6.2 Stage 2 output schema (fixed)

```json
{
  "article_id": "string",
  "ticker": "005930",
  "model_id": "string",
  "prompt_version": "v1",
  "relevance": 0.0,      // 0-1, is this directly related to the ticker's earnings/price
  "polarity": 0.0,       // -1 to 1, direction
  "intensity": 0.0,      // 0-1, magnitude of financial impact
  "uncertainty": 0.0,    // 0-1, degree of outcome uncertainty
  "forwardness": 0.0,    // 0-1, already-priced-in past fact (0) vs future expectation (1)
  "rationale": "string"  // 40 characters or fewer
}
```

> [!note] Why polarity alone isn't used
> In multi-dimensional sentiment research, intensity and uncertainty contributed more predictive power than simple polarity alone.

### 6.3 Prompt discipline

- `temperature=0`, the schema is enforced via tool/structured output
- The system prompt and scoring criteria are kept separate as **prompt caching** targets
- Changing the prompt wording makes prior scores non-comparable. Tag the prompt with a version and record it alongside the score.
- Prompts are managed as files under `src/llm/prompts/`, not hardcoded in the code.

---

## 7. Model selection — adapters and bake-off

### 7.1 Vendor-neutral principle

All model calls pass through the single `src/llm/adapter.py`. Code outside the adapter must not know which vendor is in use.

```yaml
# config/models.yaml
embedding:
  provider: local
  model: BAAI/bge-m3

scoring:
  provider: anthropic        # anthropic | openai | google | naver | local
  model: claude-sonnet-5
  temperature: 0
  batch: true

synthesis:
  provider: anthropic
  model: claude-sonnet-5
  temperature: 0.3
```

Implement either via a unified `litellm` layer, or a thin custom wrapper combined with per-vendor SDKs. Either way, the requirement is that **swapping happens via a single provider string.**

### 7.2 Model fit per stage

| Stage | Required capability | Candidates | Verdict |
|---|---|---|---|
| Embedding | Handles Korean/English simultaneously, runs locally | bge-m3, multilingual-e5, KR-SBERT | bge-m3 handles mixed KR/EN documents well. Zero cost since it's local |
| Scoring | Nuance in Korean financial articles, structured output, low-cost batching | Claude Sonnet 5 / GPT-5 family / Gemini Pro family / HyperCLOVA X / EXAONE / Solar | **Decided by bake-off** |
| Synthesis / red-team | Long-form reasoning, instruction following | Any frontier model | Call volume is low so cost impact is negligible. Prioritize quality |
| Development tooling | Agentic coding | Claude Code / Codex CLI / Gemini CLI / Cursor / Aider | Personal preference. This project is written against Claude Code |

> [!important] Don't exclude domestic Korean models from consideration
> HyperCLOVA X uses a Korean-specialized tokenizer, so the same Korean text produces fewer tokens than with overseas models. For a workload dominated by Korean articles, that directly translates into a cost difference. EXAONE and Solar have open-weight variants, which could drive the marginal cost to zero via local serving. That said, **maturity of structured-output support (tool/JSON schema)** varies by vendor, so this must be measured empirically.

### 7.3 Golden set

Before the bake-off, build **100 hand-labeled examples**. Should take about 2 hours. Without this, model selection is entirely guesswork.

- Composition: 25 clearly positive / 25 clearly negative / 25 ambiguous / 25 irrelevant
- Labels: assign all 5 dimensions from the §6.2 schema by hand
- Storage: `data/golden/v1.jsonl` (committed)

### 7.4 Bake-off protocol

Run 3 candidate models against the same golden set + same prompt, and measure the following.

| Metric | Definition | Passing bar |
|---|---|---|
| Golden-set correlation | Spearman correlation of model score vs. my label (per dimension) | relevance > 0.7, polarity > 0.6 |
| Schema compliance rate | Fraction of calls returning valid JSON without a parse failure | > 99% |
| Self-consistency | Stdev of scores across 5 repeated runs on the same article | polarity σ < 0.1 |
| Inter-model agreement | Spearman correlation between candidate models | See PREREGISTRATION §8.3 |
| Cost per valid signal | Total cost ÷ count of articles with relevance>0.5 | Relative comparison |
| Latency | Time to complete a batch | Within the briefing's time budget |

**Decision rule**: among models that pass golden-set correlation and self-consistency first, adopt the one with the lowest cost per valid signal. If performance is comparable, pick the cheaper one.

---

## 8. Evaluation pre-registration

> [!danger] This section now lives in `PREREGISTRATION.md`
> It was split out and committed as a standalone artifact on 2026-08-02, before any data collection. A pre-registration that also exists as a copy inside a document under active revision is not a pre-registration — the two drift, and it stops being possible to say what was committed to and when. `PREREGISTRATION.md` is the single source of truth; this section is a pointer only.

**Full text: @PREREGISTRATION.md** — section numbering §8.1–§8.5 is retained there, so references to "§8.3" and "§8.5" resolve unchanged.

In summary:

- **§8.1** — what 2 weeks can and cannot validate. Pipeline integrity, data consistency, `ambiguous` ratio, reproducibility, golden-set performance, inter-model agreement, and cost are all measurable. Signal hit rate and strategy profitability are not.
- **§8.2** — why: separating a 55% hit rate from 50% needs ~800 independent observations; two weeks yields an effective 60–100 after market beta eats the cross-sectional independence.
- **§8.3** — measured instead: inter-model agreement, as a test of measurement stability rather than predictive power.
- **§8.4** — the real signal evaluation, deferred to 3 months and run on sector-excess returns via IC / ICIR / quantile spread, not direction.
- **§8.5** — the 2-week, 3-month, and 6-month decision gates, and the rule that no real money moves before the 3-month gate passes.

Revisions to any of the above are logged in `PREREGISTRATION.md` §R, per §0 principle 6.

---

## 9. Cost estimate

### 9.1 Runtime (monthly)

| Item | Minimal configuration | Standard configuration |
|---|---|---|
| GitHub Actions | $0 (within the 2,000 free private minutes) | $0 |
| Data APIs | $0 (all free tier) | $0 |
| Embedding (local bge-m3) | $0 | $0 |
| Stage 2 scoring (batch) | ~$2 | ~$8 |
| Stage 3 synthesis (real-time) | ~$3 | ~$10 |
| Delivery (vault/email) | $0 | $0 |
| **Total** | **~$5/month** | **~$18–20/month** |

Minimal configuration = 15 watchlist tickers, run once daily.
Standard configuration = 60 watchlist tickers (30 KR + 30 US), run twice daily.

The reduction from v0.1 comes from moving Stage 1 to embeddings. Batch APIs get a 50% discount on both input and output, and the briefing has no real-time requirement, so batch is the default across the board.

**During the bake-off period, cost multiplies by the number of candidate models.** 100 golden-set examples × 3 models × 5 repeats = 1,500 calls, but the total is still under $1.

### 9.2 Development cost

Claude Code has no separate pricing table — it's billed at the standard token rate of whichever model it runs — but Claude Pro/Max subscriptions include usage within the subscription's limits. For personal development use, the subscription is cheaper than pay-as-you-go API billing.

---

## 10. Project structure

```
market-briefing/
  README.md                   # orientation: status, pipeline flow, repo map
  README.ko.md                # Korean version of the above
  CLAUDE.md
  SPEC.md                     # this document
  PREREGISTRATION.md          # §8 split out and committed first
  MANUAL-TASKS.md             # work only Ricky can do
  API-KEYS.md                 # per-provider issuance walkthrough for §1 of the above
  pyproject.toml              # uv + pytest + ruff config
  uv.lock
  .env.example                # every required key, no values
  .gitignore                  # .env, data/, Python and OS artifacts
  config/
    watchlist.yaml
    aliases.yaml              # ticker alias dictionary (manually managed)
    sector_mapping.yaml       # US ETF ↔ KR sector mapping
    models.yaml               # model selection
    rating.yaml               # §2.2⑥ — weights and cut points for the directional rating
    delivery.yaml             # §2.0 — the only place a channel may be declared
  src/
    util/
      session.py              # UTC/KST, trading days, look-ahead boundary
      config.py               # config loading + hand-editing safeguards
    collectors/
      validate.py             # the four checks every collector must pass
      kr_price.py
      kr_flow.py
      kr_news.py
      us_price.py
      us_filings.py
      macro.py
    entity/
      resolve.py              # §4
    embed/
      encode.py
      dedup.py                # re-report clustering
      relevance.py
    features/
      compute.py
      normalize.py
    llm/
      adapter.py              # vendor-neutral layer
      score.py
      synthesize.py
      prompts/
        v1_scoring.md
        v1_redteam.md
        v1_synthesis.md       # §2.2⑧ — paired with report/consistency.py
    report/
      rating.py               # §2.2⑥ — the deterministic directional rating
      consistency.py          # §2.2⑧ — commentary checked against the rating
      render.py
    notify/
      base.py                 # Channel interface
      vault.py
      email.py
      webhook.py
    eval/
      golden.py               # golden-set scoring
      bakeoff.py               # model comparison
      ic.py
      shadow_portfolio.py
  data/
    golden/v1.jsonl
  tests/
    test_session.py
    test_validate.py
    test_config.py
    test_rating.py
    test_consistency.py
    test_collectors.py
    test_entity.py
    test_features.py
  .github/workflows/
    briefing.yml
```

---

## 11. CLAUDE.md draft

````markdown
# Project Conventions

## Purpose
Automated daily briefing generation for Korean and US equities. Does not execute trades.
Full spec at @SPEC.md, evaluation criteria at @PREREGISTRATION.md.

## Absolute rules
- Never overwrite or delete files under `data/raw/`.
- Never write code that calls order/execution-related KIS API endpoints. Read-only only.
- Never have the LLM generate buy/sell recommendation language. Output is limited to the numeric schema in SPEC §6.2.
- Never import a vendor SDK directly outside `src/llm/adapter.py`.
- Never guess at external API specs. Read the official docs, and ask if uncertain.

## Determinism first
Don't use an LLM for problems solvable with string matching, embedding similarity, or statistics.
When proposing a new feature that involves an LLM call, first explain why it can't be done deterministically.

## Collector rules
When building a new collector, **write the validation functions first**:
1. Assert the return schema (column names, dtypes)
2. Check the missing-value ratio against a threshold
3. Check trading-day continuity (excluding market holidays)
4. Cross-check at least one known value (e.g., Samsung Electronics' closing price on a specific date)

A collector without validation does not get merged.

## Normalization
All features use a 252-trading-day rolling z-score. No comparing absolute values.

## Time handling
- Store everything in UTC
- Display only in KST
- Determine market sessions with `pandas_market_calendars`; no hardcoding

## Look-ahead prohibition
When computing a feature, never use data at time $t$ to explain the return at time $t$.
News is split by publication timestamp to determine the earliest tradeable time.
News published during market hours is assumed to be tradeable at the next day's open.

## Failure handling
Don't silently pass over a collection failure. Note the missing data in the report header and
publish a partial report anyway. Don't use a try/except that swallows the exception.

## Secrets
`.env` locally; GitHub Actions uses repository secrets.
Never print keys to logs or reports.

## Testing
Run via `pytest`. Tests that hit the network are separated under `@pytest.mark.network`.
````

---

## 12. Work order

1. Create the repo, commit `SPEC.md`, `PREREGISTRATION.md`, `CLAUDE.md`
2. Write `watchlist.yaml` + `aliases.yaml` — start with 15 KR tickers
3. 4 collectors + validation tests
4. 3-year backfill, load into `data/raw/`
5. Entity resolution + measure the `ambiguous` ratio
6. Embedding pipeline (dedup + relevance)
7. **Hand-label the 100-example golden set**
8. Model adapter + bake-off → finalize the scoring model
9. Feature computation + cross-check against known values
10. Report renderer + delivery channels (vault, email)
11. GitHub Actions workflow, verify manual trigger
12. Activate the schedule, log daily runs and cost
13. §8.5 gate decision after 2 weeks

> [!note] You'll want to skip step 7
> Golden-set labeling is tedious and the only step that doesn't involve writing code. But skipping it makes step 8 impossible, and model selection ends up decided by "Claude seemed good." It's worth the 2 hours.
