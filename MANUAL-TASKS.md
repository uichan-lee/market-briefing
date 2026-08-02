# MANUAL-TASKS.md

Work that Claude cannot do. Ricky does these. Everything else in this project is delegable.

Ordered by when it blocks progress. Items marked **BLOCKING** stop the pipeline until they are done.

---

## 1. Accounts and credentials — **BLOCKING**

Estimated time: 60–90 minutes total, mostly waiting on approvals.

| Item | Where | Notes |
|---|---|---|
| DART OpenAPI key | opendart.fss.or.kr | Instant issue. Free. |
| Naver Developers app | developers.naver.com | Register an application, enable the 검색 (search) API, record client ID and secret. |
| KIS Open API keys | 한국투자증권 → 트레이딩 → Open API → KIS Developers | Requires a securities account. Request the **모의투자** (paper) environment as well. Approval is not always instant. |
| FRED API key | fred.stlouisfed.org | Instant issue. Free. |
| US market data key | Alpaca or Tiingo | Pick one. Free tier is sufficient at this stage. |
| Email sending credential | Dedicated address + app password | Do not use a primary personal address. |

Store all of these in `.env` locally and as GitHub repository secrets. Never paste a key into a chat session with any AI tool, including Claude Code.

---

## 2. Watchlist — **BLOCKING**

File: `config/watchlist.yaml`

Start with **15 Korean tickers**. Not 60. A smaller list makes every downstream problem visible faster, and the alias dictionary in the next section scales with this number.

Include:
- Whatever Ricky actually holds
- A few names Ricky watches but does not hold, as a control group
- At least two large caps with heavy news coverage (삼성전자, SK하이닉스) — these stress-test deduplication
- At least one name with an ambiguous group name (한화 계열, LG 계열) — these stress-test entity resolution

US tickers come later, after the Korean side is stable.

---

## 3. Alias dictionary — **BLOCKING**

File: `config/aliases.yaml`

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

---

## 4. Golden set — **BLOCKING for model selection**

File: `data/golden/v1.jsonl`

Without this, the choice of scoring model is guesswork. With it, the choice is measured.

**Estimated time:** about 2 hours. This is the only step in the project that involves no code, and it will be tempting to skip.

### Procedure

1. Run the collectors for one week. Do not run any LLM yet.
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

## 6. Obsidian wiring

One-time setup:

1. Clone the repository into a folder inside the Obsidian vault.
2. Install the Obsidian Git community plugin.
3. Configure it to pull automatically. Do not configure it to push — the repository is written by CI, and a bidirectional sync will produce conflicts.
4. Confirm `reports/` renders correctly, including the callout blocks and LaTeX.

Mobile reading goes through email, not Obsidian. Obsidian Git on mobile is unreliable enough that it is not worth depending on for a daily habit.

---

## 7. Daily, during the two-week trial

Five minutes a day. This is the actual experiment.

- [ ] Read the briefing. Note whether it was read at all — if Ricky stops reading it by day 8, that is the most important finding of the trial and it means the format is wrong.
- [ ] Log anything that looked wrong: a misattributed article, a stale number, a section that was noise.
- [ ] Record the daily `ambiguous` ratio and the run cost.

Keep this log in `notes/trial-log.md`. It is the input to the two-week gate decision in PREREGISTRATION §8.5.

**Do not change any trade based on the briefing during these two weeks.** The sample is far too small to carry information, and acting on it converts a measurement into a bias.

---

## 8. Not on this list

Everything else — collectors, entity resolution, embeddings, features, the report renderer, delivery adapters, the Actions workflow, tests — is Claude's work. If Ricky finds himself writing that code by hand, something has gone wrong with the delegation, not with the plan.
