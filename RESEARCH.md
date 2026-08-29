# RESEARCH.md — LLM trading agents in the wild, and what applies here

Survey of publicly documented LLM/agent-driven trading systems, written 2026-08-11 to inform SPEC §12 step 8 (model adapter + bake-off) and to check this project's design against what the field has actually learned.

**Scope note.** This project does not trade (CLAUDE.md rule 2, SPEC §0). Most systems below do. They are still the right comparison set, because the part this project shares with them — turning text into a number that is supposed to predict returns — is exactly the part where the field's failures cluster.

**Evidence grading used throughout.** Claims are marked ⓟ primary source read directly, ⓢ secondary reporting only. Where a number is a *claim by the system's author* rather than an independent measurement, it says so.

---

## 1. The headline answer

Ricky's starting observation was that some of these report returns above the S&P 500. That is true, and it is mostly not what it looks like.

**Three tiers of evidence exist, and they disagree with each other in a way that is itself the finding.**

| Tier | Typical reported result | What it actually establishes |
|---|---|---|
| Backtests inside the model's training window | Sharpe 6–8, annualised 30–90% | Almost nothing. The model was trained on the outcome. |
| Live, post-cutoff, single system, short window | +7% to +25% excess | Weak. Underpowered; usually one run, one regime. |
| Live, post-cutoff, many agents, controlled | Median agent loses money | The only tier with a real signal, and the signal is negative for autonomous trading |

The single most informative datapoint is that **when the same experiment is run across many models with real money and real costs, most lose.** Two independent live arenas found this. The systems reporting spectacular numbers are, without exception found in this survey, either backtested inside the training window or run once over a few months.

**But the tiers do not collapse to "none of it works."** One post-cutoff, statistically-controlled study (MarketSenseAI, §3.3) does show a credible edge — and it is a *recommendation* system whose output is a monthly ordinal rating, not an autonomous trader. That is the closest published analogue to what this project is, and it is the one result worth taking seriously.

---

## 2. The contamination problem, in detail

This is the field's dominant methodological failure and deserves its own section because it determines how to read everything in §3.

**Mechanism.** ⓟ An LLM trained on data through date *T* has absorbed what happened after any date *t < T*. Asked to "analyse" a 2024-01 headline, a model with a 2024-04 cutoff is not forecasting; it is partially recalling. Frontier models reproduce S&P 500 closing prices **with under 1% error inside the training window**, and error jumps sharply after the cutoff.

**Why it is worse than classic look-ahead bias.** Classic look-ahead is a code defect — findable by audit. This lives in the weights. There is no way to inspect which facts a model retained, and a correct bullish call is indistinguishable from a memorised one.

**The measurement.** ⓟ *Detecting Lookahead Bias in LLM Forecasts* (arXiv 2512.23847) builds a **Lookahead Propensity (LAP)** score: query the model with a date-and-firm pair and measure whether it can recall the outcome. The result is clean — LAP is materially positive throughout the in-sample period and **collapses to essentially zero immediately after the training cutoff**. Forecast accuracy is amplified precisely on high-LAP firm-date pairs, and that interaction loses significance post-cutoff. So the bias is real, measurable, and its size tracks a quantity anyone can compute.

**The published mitigations, ranked by how much they actually buy:**

1. **Evaluate only after the cutoff, plus a buffer.** The only mitigation that fully works. Everything else is partial.
2. **Anonymisation** — strip ticker, company name, executives, products from the text before the model sees it. ⓟ *BlindTrade* (arXiv 2603.17692) replaces `AAPL` → `STOCK_0026` via a knowledge-graph pass. Honest about its limits: *"We do not claim this blocks all leakage, but it at least blocks the path where LLM sees a ticker and decides 'it's Apple, so buy.'"* **And they never ran the ablation** — Appendix A admits no direct anonymised-vs-raw comparison. That gap is an opportunity for this project (§6.2).
3. **Separate knowledge from reasoning.** Use the LLM to interpret text handed to it; never to recall history. RAG over live documents is structurally safer than "what happened to X in 2024".
4. **Decision auditing.** Log every input given to the model. A correct call made without the relevant input having been supplied is evidence of recall.

**Practical red flags for reading any paper in this space** ⓟ: results on 5–10 tickers over 3–6 months inside the training window; no transaction costs; no survivorship handling; no comparison against equal-weight or random baselines.

---

## 3. The cases

### 3.1 TradingAgents (TauricResearch) — the most-copied architecture, the least trustworthy numbers

ⓟ arXiv 2412.20138. Multi-agent firm simulation: four analysts (fundamental / sentiment / news / technical) → bull-vs-bear researcher **debate** with a facilitator → trader → three-way risk team (aggressive / neutral / conservative) → fund manager sign-off. Hybrid protocol: structured documents between stages, natural-language dialogue inside the debate, a global agent state to stop message corruption.

Reported: AAPL cumulative **26.62%**, Sharpe **8.21**, max drawdown 0.91%; GOOGL Sharpe 6.39; AMZN 5.60.

**Why the numbers cannot be used.** Backtest is **2024-01-01 to 2024-03-29 — three months, three tickers**, entirely inside GPT-4o's training window. The authors state the window was short "due to intensive LLM and tool use" and acknowledge the Sharpes exceed normal empirical ranges. A Sharpe of 8 on three months of three megacaps in a rising market is a description of the market, not the method.

**What is worth taking.** The *architecture* is the field's reference design and the cost accounting is honest: **11 LLM calls and 20+ tool calls per prediction**, with an explicit fast-model / deep-model split (`gpt-4o-mini` for shallow tasks, reasoning model for the debate).

### 3.2 FinMem / FinAgent — the memory line

ⓢ FinMem (arXiv 2311.13743, ICLR/AAAI-SS) introduced layered memory keyed to a trader-like cognitive structure — profiling, hierarchical memory, decision — with an adjustable "cognitive span". FinAgent added a multimodal market-intelligence encoder with dual-level reflection and reported **annualised return >90% across six benchmarks**.

Treat the 90% as uninterpretable without a post-cutoff replication; it is the same tier as §3.1. The durable contribution is the **memory taxonomy** — working / episodic / semantic — which the survey in §4 adopts.

### 3.3 MarketSenseAI — the one credible positive result ⓟ

arXiv 2604.17327, *Signal or Noise in Multi-Agent LLM-based Stock Recommendations?* This is the closest published system to this project's shape, and the only one in this survey with an evaluation design that would survive PREREGISTRATION.

**Design.** Four specialist agents — **News** (ticker events), **Fundamentals** (financials, filings, transcripts), **Dynamics** (price action), **Macro** (sector and macro context) — each emitting free text. A **synthesis agent** reads all four and emits a thesis plus an **ordinal recommendation** (strong sell → strong buy). Monthly cadence, first-Friday rebalance, one-month forward holding.

**Method.** 467 S&P 500 stocks × 19 monthly observations (2024-09 → 2026-03, 8,873 rows), all signals **generated live at each observation date** and the whole cohort **post-dating the LLM training cutoff**. Null distribution from **10,000 Monte Carlo** equal-weight random portfolios of matched size.

**Results.** Strong-buy equal-weight **+2.18%/month vs +1.15%** equal-weight benchmark → **+25.2pp compound excess** (+46.8% vs +21.6%), **99.7th percentile** of the null (p = 0.003), 57.9% of months beating the benchmark. Date-level **ICIR +0.489** (p = 0.024).

**Attribution — the interesting part.** A non-negative least-squares decomposition of thesis embeddings onto agent embeddings shows **no single agent dominates**. Fundamentals leads on the S&P 500 pooled (IC +0.052) but **Macro leads on 6 of 19 dates**, and those dates coincide with macro events (Fed easing, election, tariffs, recession scares). Dynamics has a *negative* aggregate IC (−0.069) yet leads 5 dates — behaving as a regime-conditional signal rather than a consistent one.

**Their own caveats, which are unusually honest.** 19 observations. A generally positive-return regime throughout; bear-market behaviour unknown. Portfolio beta 0.865, which argues against a pure high-beta explanation but does not settle it. Sell-side calls were *wrong* — sell/strong-sell names earned positive returns, mechanism unexplained. And the **S&P 100 robustness cohort was not significant** (83rd percentile, p = 0.17, ~10 stocks/month). One cohort significant, one not, is the correct description of this result.

### 3.4 Live arenas — where the story turns

**Agent Market Arena / "When Agents Trade"** ⓟ (arXiv 2510.11695, ACM Web Conf 2026). Five backbones (GPT-4o, GPT-4.1, Claude-3.5-haiku, Claude-sonnet-4, Gemini-2.0-flash) × four agent architectures, live on BTC, ETH, TSLA, BMRN from 2025-08. Best cell: InvestorAgent + GPT-4.1 on TSLA, **+40.83%, Sharpe 6.47**. HedgeFundAgent **+39.66% on ETH but heavy losses elsewhere**.

Its central finding matters more than any return: **agent architecture dominates model choice.** Swapping architecture produced substantial differences; swapping the LLM inside a fixed architecture produced only modest ones. All agents struggled with abrupt macro reversals.

**DeepFund** ⓟ (arXiv 2505.11065, NeurIPS'25 D&B) — titled *"Time Travel is Cheating"*, built specifically to force live, post-cutoff evaluation. Notable for engineering reliability numbers rather than returns: **4,144/4,320 valid signals (96%)**, **1,059/1,080 valid decisions (98%)**. Returns were modest — 8.61% TSLA, 9.45% BMRN.

**AI-Trader / HKU Business School** ⓢ (arXiv 2512.10971; 21.2k★ repo; live board at ai4trade.ai). Ten current frontier models — Qwen, Kimi, Seed, GLM, GPT-5.4, MiniMax, Claude Opus 4.6, DeepSeek V3.2, Gemini 3.1 Pro, Grok-4.1 Fast — each $100k, identical tools, data and leverage, six weeks from 2026-04, on FX majors, the S&P index and metals. Spread: **Qwen ≈ +10% to DeepSeek −15.1%**, GPT-5.4 near flat.

Their stated conclusions are the useful output: **"general intelligence does not automatically translate to effective trading capability"**; risk control determines cross-market robustness; excess returns come more easily in liquid markets; and **"the quality of decisions may matter more than the quantity"** — models placing 1,000+ trades did not beat those placing 200–800, and cautious sizing beat aggressive leverage.

### 3.5 Real money — Alpha Arena ⓢ

nof1.ai. Six LLMs, **$10,000 each of real capital**, identical prompts, autonomous crypto perpetuals on Hyperliquid.

| Model | Return |
|---|---|
| Qwen3 Max | **+22.3%** |
| DeepSeek Chat V3.1 | +4.89% |
| Claude Sonnet 4.5 | −30.81% |
| Grok 4 | −45.3% |
| Gemini 2.5 Pro | −56.71% |
| GPT-5 | −62.66% |

Four of six lost, several catastrophically. The organiser attributed early damage to **excessive trading costs**, and to models struggling to parse market data inside limited context.

**The best critique of it is also worth reading** ⓟ (Boris Tseitlin, *"Why Alpha Arena is literally the worst"*): one instance per model over a very short horizon establishes nothing — *"if you took six people and had them trade for two weeks, what would it tell you about their trading ability?"* All models received identical pre-computed indicators, which hardcodes the strategy and removes the thing supposedly being measured; and they had no news or outside tools, so they were asked to predict from "a wall of numbers".

**How to hold both.** The critique is correct that Alpha Arena cannot rank models. It does not rescue the models: four of six losing 30–60% of real capital in weeks is still evidence that autonomous LLM trading with live execution and real costs is not a solved problem.

### 3.6 Real money, single operator — ChatGPT micro-cap ⓟ

LuckyOne7777/LLM-Trading-Lab. $100 real capital, US micro-caps ≤$300M, no shorting/leverage/derivatives, six months (2025-06-27 → 2025-12-26), ChatGPT holding full decision authority with the human limited to prompting and execution. Ships a full evaluation report — the most methodologically self-aware artifact in this survey.

**Outcome: bad, and instructively so.** Equity fell to ~$67.10 at max drawdown (**−50.33%**), including a single ~40% day from an overnight gap. 46 FIFO lot exits over 22 tickers, **50% win rate**, average win +$3.01 against average loss −$3.83, **profit factor 0.82**, expectancy −$0.41/lot.

**The three behavioural findings are the value here:**

- **Concentration.** ~3.1 tickers held per day, average cost basis 25% of starting capital per position.
- **Thesis persistence.** 32% of tickers were bought more than once, and **the three worst-PnL tickers all had repeated buy-side entries.** The model re-entered names after realised losses.
- **Downside asymmetry.** One position (ATYR) dominated everything: removing it flips expectancy from −$0.41 to **+$0.49**.

Plus a governance finding: the model **wrote false statements about hedging instruments and portfolio rules into its weekly reports** despite derivatives being prohibited — narrative drift without execution drift. Exactly the failure mode a consistency guard exists to catch.

The author's own limitations section (single run, one regime, unfixed temperature, evolving prompt templates, reconstructed accounting) is more rigorous than most of the papers above.

### 3.7 Claude-specific builds ⓢ/ⓟ

- **"I gave Claude Code $100k…"** (Nesler) ⓟ — multi-agent CEO/strategy/governance setup over Alpaca, Go compute tool + JS MCP server, SQLite vector store of past trades. **Paper trading, not real money** — the author says so explicitly and urges against live use. 33 days, **+7.6% vs S&P +4.52%**, but with a **−22.4% drawdown** after a single earnings crash, and the author's own verdict: *"not a real number you can trust."* Since deprecated.
- **HKUDS/AI-Trader** ⓟ — agent-native platform, $100k paper accounts, supports Claude Code / Codex / Cursor / others as drop-in agents, multi-broker, live mark-to-market leaderboard.
- **Ecosystem** ⓟ (LLMQuant/awesome-trading-agents) — the tooling layer has consolidated fast: official **Alpaca MCP** (paper + live), Kraken/OKX/IB MCPs, market-data MCPs, and Claude **Skill** packs (`tradermonty/claude-trading-skills`, vectorbt backtesting skills). Notably there is now a **memory MCP for decision rationale** (`mnemox-ai/tradememory-protocol`) — the pattern of journalling decisions and feeding them back.
- **Claude Code Routines** ⓢ — cron-scheduled headless runs are the common scheduling pattern; the recurring advice is that a 15–30 minute cycle is ample and that a written decision journal is the highest-value artifact.

---

## 4. How they are built — the converged architecture

ⓟ *Agentic Trading: When LLM Agents Meet Financial Markets* (arXiv 2605.19337) proposes an **Architecture–Capability–Adaptation** taxonomy that describes nearly every system above:

- **Perception** — text (news, filings), time-series (OHLCV), multimodal (charts).
- **Memory** — working (context), episodic (past trades), semantic (domain knowledge).
- **Reasoning** — reactive (rules), reflective (chain-of-thought, self-critique), strategic (portfolio planning).
- **Execution** — order mapping, cost model, latency.

Recurring patterns: RAG-grounded memory; chain-of-thought with a reflection pass before acting; tools for market data and portfolio state rather than free-form reasoning; role decomposition (analyst / risk / executor) coordinated by hierarchical message passing.

**What the survey says works:** explicit reasoning traces improve auditability even when they do not improve returns; memory consolidation stabilises performance across regimes **if temporal boundaries are enforced**; modular architectures are testable.

**What does not work, or has no evidence:** no memory architecture has been shown superior; LLM reactive/HFT is infeasible on latency; **hallucinated actions propagate through agent loops, so risk controls must operate during reasoning, not after**; and naive RAG suffers the **"Oracle Fallacy"** — retrieving narratives that explain a past event using information that did not exist at decision time.

---

## 5. The limits — the field's own audit of itself

The most useful number in this entire survey. ⓟ Of **19 primary studies** with tradable actions and closed-loop evaluation, the survey found:

| Disclosed | Count |
|---|---|
| Time-consistent data splits | **2 / 19** |
| A transaction-cost model | **1 / 19** |
| Universe / survivorship handling | **1 / 19** |
| Execution timing or semantics | 11 / 19 |
| R0 reproducibility (no artifacts) | 15 / 19 |
| R3 reproducibility (full replication) | **0 / 19** |

The authors call comparable evaluation protocols "the field's immediate bottleneck." Their open problems list — temporal discipline, execution realism, reproducibility, regime adaptation, hallucination, coordination — reads as a specification for what a careful system should do.

**Costs, which are usually omitted.** TradingAgents needs 11 LLM + 20 tool calls *per ticker per decision*. At this project's 71 tickers × 2 runs/day that is ~1,600 LLM calls and ~2,900 tool calls daily for a system whose evaluation window is three months. That is the real reason those backtests are three months long, and it is a design constraint, not an accident.

---

## 6. Know-how worth stealing

Ordered by value to this project.

1. **Score the article, not the ticker-date.** Every credible mitigation reduces to handing the model text and letting it interpret, never letting it recall an outcome for a named entity on a known date.
2. **Anonymise before scoring, and *measure* what it changes.** BlindTrade's unrun ablation is a free experiment for anyone with a labelled set.
3. **Temperature 0, and measure self-consistency explicitly.** ⓟ Reported intra-model agreement across repeated calls runs Fleiss' κ **0.886–0.969**; Claude models specifically retain near-perfect structural reliability even at T=0.9, while smaller models degrade. Determinism is cheap and should not be left to chance.
4. **Split fast and deep models by task.** Universal across the cost-aware systems.
5. **Journal the decision and its inputs.** Both the auditability argument and the leakage-detection argument point to the same artifact.
6. **Equal-weight and cap positions.** The two real-money failures were caused by concentration and re-entry, not by bad reading of news.
7. **Guard the prose against the numbers.** The micro-cap model wrote rules into its reports that it was not following. A mechanical consistency check is the only thing that catches this.
8. **Report validity rates, not just returns.** DeepFund's 96%/98% signal and decision validity is a more useful engineering metric than its returns.

---

## 7. What this project should actually do with this

Mapped to files and SPEC sections. Ordered by whether it changes a decision.

### 7.1 The contamination position is already strong — write it down before step 8

`data/raw/kr/news/` begins **2026-08-03**. Every article this project will ever score post-dates the training cutoff of every candidate model in the bake-off. That is the §2 mitigation that actually works, obtained structurally rather than by design cleverness, and **most published systems do not have it.**

The 3-year price/flow backfill *is* inside training windows — but no LLM touches it. CLAUDE.md rule 3 confines LLM output to three places, none of which sees a price series. **This is the rule doing exactly the work it was written for**, and §2 is the external evidence for why it should not be relaxed.

**Action:** record this as a stated property in SPEC §7.4 before the bake-off runs, so that a later reader can tell the property was designed rather than discovered afterwards.

### 7.2 Run the anonymisation ablation BlindTrade skipped — cheap, and it is a real result

Score the 100-example golden set twice: once as-is, once with company names and tickers masked. If per-dimension agreement barely moves, memorisation is not driving the scores; if it collapses, it is.

Cost is 200 calls. The golden set (`data/golden/`) already exists, `scripts/golden.py` already computes per-dimension agreement, and PREREGISTRATION §8.3 already records the **±0.13 `forwardness` noise floor** that any such comparison must clear to mean anything. This is the single highest-value addition to step 8, and it is a genuine contribution — the paper that proposed anonymisation admits it never measured it.

**Caveat to state in advance:** with a floor of ±0.13 on `forwardness` and ±0.03–0.07 elsewhere, this test can only detect a large memorisation effect. That is still worth knowing.

### 7.3 The bake-off should expect a small spread, and that is not a failure

ⓟ Agent Market Arena: architecture dominates, model choice contributes modestly. ⓟ HKU: general capability does not transfer to trading skill.

SPEC §7.4 ranks models on golden-set agreement, schema compliance, self-consistency and cost. Expect the frontier models to cluster on the agreement dimension. **Write down now** that a small spread means "pick on cost and schema compliance", not "the bake-off failed" — otherwise a tie invites re-running until something separates, which is the same defect PREREGISTRATION exists to prevent.

### 7.4 SPEC §7.4's self-consistency bar has external support

The `polarity σ < 0.1` across 5 repeats bar is consistent with published intra-model agreement (κ 0.886–0.969). Run at temperature 0. If a model cannot hit it at T=0, that is a schema or prompt defect, not sampling noise.

### 7.5 The 3-month ICIR gate is ambitious but not absurd

PREREGISTRATION §8.4/§8.5 gate at **ICIR > 0.3**. MarketSenseAI — post-cutoff, Monte-Carlo-controlled, 467 stocks — reports date-level **ICIR +0.489**. So the gate sits below a published result on a comparable problem. Useful anchor; not a prediction.

### 7.6 A tension worth recording, not resolving

PREREGISTRATION §8.4 **excludes the macro layer from the composite** because macro terms are common across tickers and cancel in sector-excess return. MarketSenseAI found its **Macro agent leading on 6 of 19 dates**.

These do not contradict: their target is raw forward return, where a common term does not cancel; this project's is sector-excess, where it does. **The exclusion argument survives.** It is recorded here so that a future reader who encounters the MarketSenseAI result does not "fix" §8.4 without noticing the targets differ.

### 7.7 If a shadow portfolio is built (SPEC §2.2⑦), the failure modes are known in advance

Both real-money failures were caused by **concentration, re-entry after loss, and one dominant position** — not by signal quality. Equal-weight, a position cap, and no re-entry rule address all three, and the quantile-spread metric in §8.4 is already an equal-weight construction. Transaction costs are already in the 6-month gate; **1 of 19 surveyed studies modelled them at all.**

### 7.8 Cost sanity

The multi-agent debate architectures cost 11+ LLM calls per ticker per decision. This project scores **each article once** and aggregates deterministically, so cost scales with news volume rather than tickers × agents × rounds. At ~1,000 articles/day and one call each, it is roughly an order of magnitude cheaper than a TradingAgents-style pipeline over the same universe — which is what makes an evaluation window longer than three months affordable. **The determinism-first rule is also the cost strategy.**

---

## 8. Where this project already sits ahead

Against the §5 audit table, stated plainly because it is the reason to keep the discipline rather than a reason to be pleased:

| Field's gap | Here |
|---|---|
| 2/19 time-consistent splits | `as_of` required on every feature function; look-ahead prohibition in CLAUDE.md |
| 1/19 transaction-cost models | Fees, transaction tax and slippage are the 6-month gate |
| 1/19 survivorship handling | Watchlist is hand-maintained and versioned; raw data is immutable |
| 0/19 full reproducibility | Deterministic rating; raw data committed; criteria frozen 2026-08-02 with an append-only revision log |
| Contaminated backtests | News record starts after every candidate model's cutoff |
| Hallucinated narrative | `src/report/consistency.py` compares prose against the computed rating |

Two honest qualifications, updated 2026-08-29. First, the consistency guard is
wired into `render.py`, and the pending 2026-08-28 change closes the reproduced
`checked_lines == 0`, unrated-subject inheritance, and compound-recommendation
bypasses with fail-closed regression tests. It has still never checked published
production prose because the Anthropic key is absent and the change is not yet
deployed. The row above is therefore a tested control, not yet a
production-proven one; the key must not be enabled before the pending change
lands. Second, none of this is evidence the signal works; it is evidence that if
the signal does not work, this project is designed to be able to tell.

---

## 9. What could not be verified

- The `When Agents Trade` PDF did not yield per-model return tables directly; §3.4 numbers come from the paper's alphaXiv overview.
- AI-Trader / HKU numbers are from the university press release and search summaries, not the arXiv PDF.
- Alpha Arena figures are from secondary crypto-press reporting; the nof1 primary results page was not read.
- FinMem/FinAgent performance claims (§3.2) were not traced to a primary table and should be treated as unverified.
- No Korean-market LLM signal study was found. The KOSPI literature located is classical sentiment/analyst-revision work, not LLM-based. **This project's KR half has no published comparison set** — which is a reason for the golden set to carry its weight, since there is nothing external to check against.

---

## Sources

Contamination and methodology:
- [Detecting Lookahead Bias in LLM Forecasts (arXiv 2512.23847)](https://arxiv.org/abs/2512.23847)
- [Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis (arXiv 2309.17322)](https://arxiv.org/pdf/2309.17322)
- [Look-Ahead Bias in LLM Trading: Why Your Backtest Is Lying](https://paperswithbacktest.com/course/look-ahead-bias-llm-trading)
- [Can Blindfolded LLMs Still Trade? An Anonymization-First Framework (arXiv 2603.17692)](https://arxiv.org/html/2603.17692v1)

Systems and results:
- [TradingAgents: Multi-Agents LLM Financial Trading Framework (arXiv 2412.20138)](https://arxiv.org/html/2412.20138v5)
- [Signal or Noise in Multi-Agent LLM-based Stock Recommendations? (arXiv 2604.17327)](https://arxiv.org/html/2604.17327)
- [FinMem: A Performance-Enhanced LLM Trading Agent (arXiv 2311.13743)](https://arxiv.org/abs/2311.13743)
- [When Agents Trade: Live Multi-Market Trading Benchmark (arXiv 2510.11695)](https://www.alphaxiv.org/overview/2510.11695v2)
- [Time Travel is Cheating: Going Live with DeepFund (arXiv 2505.11065)](https://arxiv.org/abs/2505.11065)
- [AI-Trader (HKUDS)](https://github.com/HKUDS/AI-Trader) · [HKU Business School press release](https://www.hkubs.hku.hk/media/press-release/testing-ai-in-the-real-world-hku-business-school-released-ai-agents-trading-performance/)
- [Agentic Trading: When LLM Agents Meet Financial Markets (arXiv 2605.19337)](https://arxiv.org/html/2605.19337v1)

Real-money and practitioner accounts:
- [Alpha Arena (nof1.ai)](https://nof1.ai/) · [LLM crypto trading contest finds LLMs can't trade crypto (Protos)](https://protos.com/llm-crypto-trading-contest-finds-llms-cant-trade-crypto/) · [Four Out of Six AI Models Suffer Losses (ForkLog)](https://forklog.com/en/four-out-of-six-ai-models-suffer-losses-in-trading-tournament/)
- [Why Alpha Arena is literally the worst (Boris Tseitlin)](https://borisagain.substack.com/p/why-alpha-arena-is-literally-the)
- [LLM-Trading-Lab evaluation report (LuckyOne7777)](https://github.com/LuckyOne7777/LLM-Trading-Lab/blob/main/Experiments/chatgpt_micro-cap/evaluation/evaluation_report.md)
- [I gave Claude Code 100k to trade with (Jake Nesler)](https://medium.com/@jakenesler/i-gave-claude-code-100k-to-trade-with-in-the-last-month-and-beat-the-market-ece3fd6dcebc)

Ecosystem and reliability:
- [awesome-trading-agents (LLMQuant)](https://github.com/LLMQuant/awesome-trading-agents)
- [STED and Consistency Scoring: Evaluating LLM Structured Output Reliability (arXiv 2512.23712)](https://arxiv.org/abs/2512.23712)
- [LLM Output Drift: Cross-Provider Validation for Financial Workflows (arXiv 2511.07585)](https://arxiv.org/pdf/2511.07585)
