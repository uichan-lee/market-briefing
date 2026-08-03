# MANUAL-TASKS.md

Work that Claude cannot do. Ricky does these. Everything else in this project is delegable.

Ordered by when it blocks progress. Items marked **BLOCKING** stop the pipeline until they are done.

---

## 1. Accounts and credentials — **BLOCKING**

Estimated time: 60–90 minutes total, mostly waiting on approvals.

| Item | Where | Notes |
|---|---|---|
| **KRX Data Marketplace** | data.krx.co.kr | **Free, minutes, and now mandatory.** Register with a native ID/PW, not social login. Blocks 55% of the rating weight — see API-KEYS.md §0. |
| DART OpenAPI key | opendart.fss.or.kr | Instant issue. Free. |
| ~~Naver Developers app~~ | — | **No longer needed.** Korean news comes from outlet RSS, which requires no credential. API-KEYS.md §2 explains why and keeps Naver as the fallback. |
| KIS Open API keys | 한국투자증권 → 트레이딩 → Open API → KIS Developers | Requires a securities account, and the **모의투자** environment is a separate prior signup. Approval is not always instant — **start this one first**. |
| FRED API key | fred.stlouisfed.org | Instant issue. Free. |
| US market data key | Alpaca or Tiingo | Pick one. Free tier is sufficient at this stage. |
| Email sending credential | Dedicated address + app password | Do not use a primary personal address. |

Store all of these in `.env` locally and as GitHub repository secrets. Never paste a key into a chat session with any AI tool, including Claude Code.

> **Use `.env.example` as the checklist.** It lists every key above with its issuing URL, in the exact variable names the code expects. Copy it to `.env` and fill in the values:
>
> ```
> cp .env.example .env
> ```
>
> `.gitignore` already covers `.env` and `data/`, so neither can be staged by accident. `.env.example` itself holds no values and is committed deliberately.

**Step-by-step issuance instructions: @API-KEYS.md.** That document covers each provider's signup screen, which options to select, and the non-obvious traps — in particular the KIS `API그룹` selection, which is the strongest available enforcement of absolute rule 2, and the fact that Naver news cannot be backfilled.

> [!warning] Do the KRX registration first
> `kr_price` (daily OHLCV) is built and passing without any credential, because pykrx serves OHLCV through a Naver fallback. Everything else pykrx offers — investor flows, short interest, market cap, fundamentals — now requires a KRX Data Marketplace login, verified 2026-08-03.
>
> Those four carry 55% of the §2.2⑥ rating weight, and the remainder falls below the confidence floor, so without KRX credentials every ticker rates `관망`. It takes minutes and is free. Do it before anything else here, then start the KIS application since it is the only item with an approval queue.

---

## 2. Watchlist — **BLOCKING**

File: `config/watchlist.yaml` — exists, with the schema and the two required large caps seeded. Ricky extends it.

> **Always quote the ticker.** YAML reads a bare leading-zero number as octal, so an unquoted `000660` silently becomes the integer `432`. `005930` survives only because `9` is not a valid octal digit, which makes the failure inconsistent and easy to miss. The loader rejects unquoted tickers with an explanatory error rather than relying on anyone remembering this.

Start with **15 Korean tickers**. Not 60. A smaller list makes every downstream problem visible faster, and the alias dictionary in the next section scales with this number.

Include:
- Whatever Ricky actually holds
- A few names Ricky watches but does not hold, as a control group
- At least two large caps with heavy news coverage (삼성전자, SK하이닉스) — these stress-test deduplication
- At least one name with an ambiguous group name (한화 계열, LG 계열) — these stress-test entity resolution

US tickers come later, after the Korean side is stable.

---

## 3. Alias dictionary — **BLOCKING**

File: `config/aliases.yaml` — exists, with the schema and the two entries below already transcribed from this section. Ricky adds one entry per watchlist ticker.

This is the single highest-leverage manual task in the project. Korean financial news identifies companies inconsistently, and if this file is wrong, every news-derived number downstream is wrong in a way that is hard to detect.

**Estimated time:** 60–90 minutes for 15 tickers.

Format:

```yaml
"005930":
  canonical: 삼성전자
  aliases: [삼성전자, 삼성 전자, Samsung Electronics, SEC]
  exclude: [삼성전자우, 삼성물산, 삼성SDI, 삼성전기]
  ambiguous_parents: [삼성]     # 이 단어만 등장하면 매칭하지 않는다

"000660":
  canonical: SK하이닉스
  aliases: [SK하이닉스, 하이닉스, SK Hynix, Hynix]
  exclude: []
  ambiguous_parents: [SK]
```

Rules Ricky should follow while writing it:

- **`exclude` matters more than `aliases`.** The failure mode is not missing an article, it is attributing 삼성물산 news to 삼성전자.
- Preferred shares (우선주) are separate tickers. List them in `exclude`, never in `aliases`.
- Group names (삼성, SK, LG, 한화, 현대) go in `ambiguous_parents`. An article that mentions only the group name gets dropped, not guessed.
- Include English names. Foreign wire coverage gets picked up too.
- Include common misspellings and spacing variants that actually appear in Korean headlines.

**Maintenance:** the pipeline reports a daily `ambiguous` ratio. When it exceeds 30%, come back and extend this file. Do not let Claude auto-generate entries — an incorrect alias is worse than a missing one, and auto-generation produces incorrect aliases silently.

**Checked on load.** `src/util/config.py` rejects the mistakes that are easy to make by hand and impossible to spot afterward: the same alias claimed by two different tickers, an alias that also appears in its own `exclude` list, an entry with no aliases at all, and unquoted tickers. These are hard errors at startup, not warnings — a collision would otherwise misattribute every article containing that alias, silently, and differently depending on file ordering.

---

## 4. Golden set — **BLOCKING for model selection**

File: `data/golden/v1.jsonl`

Without this, the choice of scoring model is guesswork. With it, the choice is measured.

**Estimated time:** about 2 hours. This is the only step in the project that involves no code, and it will be tempting to skip.

### Procedure

1. Run the collectors for one week. Do not run any LLM yet. News collection is already hourly and automatic once `collect-news.yml` is on the default branch, so this week accumulates by itself — but it only starts accumulating from the day it is merged, because RSS cannot be backfilled.
2. Sample 100 articles from the collected pool: 25 clearly positive, 25 clearly negative, 25 ambiguous, 25 irrelevant. Sample the ambiguous and irrelevant buckets honestly — the temptation is to pick easy cases, which makes every model look good.
3. For each article, assign the five dimensions from SPEC §6.2 by hand:

```json
{"article_id": "...", "ticker": "005930",
 "relevance": 0.9, "polarity": -0.4, "intensity": 0.6,
 "uncertainty": 0.3, "forwardness": 0.8}
```

4. Label without looking at what happened to the price afterward. Labeling with hindsight produces a golden set that no model can match and that measures nothing.
5. Commit the file.

### Calibration notes

- `relevance` — does this article bear on the company's earnings or share price specifically? Sector-wide commentary scores low.
- `polarity` — direction only, not magnitude.
- `intensity` — magnitude of financial impact. A large contract win is high. A CEO's conference appearance is low even if the tone is positive.
- `uncertainty` — how likely is the stated outcome to actually occur? An MOU is high uncertainty. A signed contract is low.
- `forwardness` — 0 means the article reports something already priced in, 1 means it changes expectations about the future.

### Re-labeling check

Label 10 of the 100 twice, a day apart, without looking at the first pass. If Ricky's own two passes disagree more than the models disagree with each other, the schema is underspecified and needs tightening before any model comparison is meaningful.

---

## 5. Model bake-off decision

After the golden set exists, Claude runs the bake-off (SPEC §7.4) and produces a comparison table. **Ricky makes the call**, applying the decision rule: among models that pass the golden-set correlation and self-consistency thresholds, pick the cheapest per useful signal.

Record the decision and its date in `config/models.yaml` as a comment. When revisiting model choice in three months, that note explains why the current model was chosen.

---

## 6. Rating calibration — **Ricky's judgment, not Claude's**

File: `config/rating.yaml`

The briefing states a directional opinion on a seven-point scale (강한 매수 … 강한 매도). That opinion is a weighted sum of z-scores bucketed by cut points, and both the weights and the cut points currently hold **starting values that nobody has calibrated** — no data has been collected yet, so they are informed guesses, not measurements.

Claude built the mechanism. Ricky owns the numbers, because the weights encode a view about what actually moves Korean equities, and that view should be Ricky's.

**When to do this:** after the collectors have run for a week or two and real feature distributions exist. Not before — calibrating against imagined distributions is worse than leaving the defaults.

**What to look at:**

- **Distribution.** If nearly every ticker lands in `관망`, the cut points are too wide and the scale carries no information. If nothing ever lands there, they are too narrow. A usable scale puts most tickers in the middle and a few at each extreme.
- **Weights.** `foreign_flow_5d` starts highest because SPEC §3.1 argues investor-flow data is the most differentiated feature available in Korea and one obtained without an LLM. That is a hypothesis, not a finding.
- **Signs.** `short_ratio` carries a negative weight — rising short interest reads bearish. Confirm that matches how the data actually behaves before trusting it.
- **The three medium-term features** (`rel_strength_120d`, `flow_persistence_60d`, `rev_trend_12w`) sit commented out in the file with starting values that are guesses like all the others. Activating them means rebalancing the existing seven, not appending — weights are relative shares, so adding weight on top silently rescales what every existing weight means.

> [!danger] The one way to get this wrong
> Adjusting weights or cut points because the ratings they produce look agreeable. That is fitting the dial to the answer, and it converts the whole evaluation into circular reasoning.
>
> PREREGISTRATION.md §8.4 permits revising cut points for **distributional** reasons — a scale where everything is `관망` is broken regardless of returns — but never against outcome data. Every change gets a row in PREREGISTRATION §R stating which of the two reasons applied.

---

## 7. Obsidian wiring

One-time setup:

1. Clone the repository into a folder inside the Obsidian vault.
2. Install the Obsidian Git community plugin.
3. Configure it to pull automatically. Do not configure it to push — the repository is written by CI, and a bidirectional sync will produce conflicts.
4. Confirm `reports/` renders correctly, including the callout blocks and LaTeX.

Mobile reading goes through email, not Obsidian. Obsidian Git on mobile is unreliable enough that it is not worth depending on for a daily habit.

---

## 8. Daily, during the two-week trial

Five minutes a day. This is the actual experiment.

- [ ] Read the briefing. Note whether it was read at all — if Ricky stops reading it by day 8, that is the most important finding of the trial and it means the format is wrong.
- [ ] **Note whether only §2.2⑧ (AI 총평) got read.** That section exists so a rushed reader gets something; if it becomes the *only* thing read, the other seven sections are costing money and attention for nothing. Either finding is useful — the failure is not recording which one happened.
- [ ] **Note whether ⑧ was dropped for contradicting the ratings** (the header says so when it happens). Rare is expected. Frequent means either the prompt or `src/report/consistency.py` needs work, and the two must be fixed together.
- [ ] Log anything that looked wrong: a misattributed article, a stale number, a section that was noise.
- [ ] Record the daily `ambiguous` ratio and the run cost.

Keep this log in `notes/trial-log.md`. It is the input to the two-week gate decision in PREREGISTRATION §8.5.

**Do not change any trade based on the briefing during these two weeks.** The sample is far too small to carry information, and acting on it converts a measurement into a bias.

---

## 9. Repository growth — nothing to do, but worth knowing

`data/raw/kr/news/` is committed rather than gitignored, because RSS has no backfill and the collector runs on an Actions runner that is destroyed after each job. Committing is the only thing that makes yesterday's news exist today.

Measured at roughly **300–450 KB/day gzipped**, so about 110–165 MB/year, and 5–7 MB across the two-week trial. No action is needed now. Revisit at the three-month gate if the repository has become unpleasant to clone.

---

## 10. Not on this list

Everything else — collectors, entity resolution, embeddings, features, the report renderer, delivery adapters, the Actions workflow, tests — is Claude's work. If Ricky finds himself writing that code by hand, something has gone wrong with the delegation, not with the plan.
