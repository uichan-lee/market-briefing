# PREREGISTRATION.md — Evaluation criteria, frozen before data collection

> [!danger] This document was committed before any data was collected
> **Freeze date: 2026-08-02.** At the time of this commit, `data/raw/` is empty, no collector has been run, and no LLM has scored a single article. Nothing in this document was chosen after seeing a result.
>
> Changing these criteria after the fact turns validation into curve-fitting. If a criterion must change, it is changed in §R below — with the date, the reason, and an honest statement of whether data had already been seen. A revision made after seeing data is not automatically illegitimate, but it must be visible as such, and any result reported under a post-hoc criterion must say so.

This document holds the evaluation criteria for the daily market briefing pipeline. It was split out of `SPEC.md` §8; SPEC now points here. The section numbering (§8.1–§8.5) is retained from SPEC so that existing cross-references — `SPEC.md` §7.4 and `MANUAL-TASKS.md` §7 — continue to resolve.

Design context and the pipeline itself: @SPEC.md. Work only Ricky can do: @MANUAL-TASKS.md.

---

## 8.1 What can and can't be validated in weeks 1–2

| Validation target | Feasible in 2 weeks? |
|---|---|
| Pipeline integrity (missing data, duplicates, delays) | ✅ |
| Data consistency (cross-check against source) | ✅ |
| Entity-matching accuracy (`ambiguous` ratio) | ✅ |
| LLM output reproducibility | ✅ |
| Model performance vs. golden set | ✅ |
| **Inter-model agreement** | ✅ |
| Measured cost | ✅ |
| Whether the briefing actually gets read | ✅ |
| **Signal hit rate** | ❌ |
| **Strategy profitability** | ❌ |

---

## 8.2 Why hit rate isn't feasible

Distinguishing a directional hit rate of 55% from 50% ($\alpha=0.05$, power 0.8) requires roughly **800** independent observations. 30 tickers × 10 trading days = 300 observations, and tickers on the same day are strongly correlated via market beta, so the effective sample size is roughly 1/3 to 1/5 of the nominal count — effectively 60–100 observations. That's not enough to determine anything.

Strategy-level evaluation is even worse. Making an annualized Sharpe of 1.0 significant at $t=2$ requires roughly 4 years of data.

**Consequence, stated in advance:** any hit-rate number produced during the first two weeks is noise and will not be reported as evidence, in either direction. A flattering early number is not a reason to continue, and an unflattering one is not a reason to stop.

---

## 8.3 What gets measured in 2 weeks instead: inter-model agreement

> [!tip] The core idea
> We can't measure predictive accuracy yet, but we **can** measure measurement stability. Run the same articles through 3 different models and look at the score correlations.
>
> - **High agreement** → at minimum, whatever we're measuring is a real underlying signal. Predictive power still needs to be validated separately.
> - **Low agreement** → the scores are model-specific noise. Not yet eligible to move on to predictive-power validation. Fix the schema or the prompt first.
>
> This only needs a few hundred articles, so 2 weeks is plenty. And this diagnostic tells us far more than a meaningless number like "55% hit rate" would.

Measurement: per-dimension Spearman correlation for each pair of candidate models. Warn if the `polarity` correlation falls below 0.5.

---

## 8.4 Signal evaluation design (after 3 months)

**Evaluate on excess return, not absolute direction.**

$$y_{i,t+1} = r_{i,t+1} - r_{\text{sector}(i),t+1}$$

Removing market beta sharply reduces cross-ticker correlation, which rescues the effective sample size. The evaluation metrics are not hit rate but:

- **IC (Information Coefficient)**: Spearman correlation between score and next-day excess return, logged as a daily time series
- **ICIR**: mean IC ÷ stdev of IC
- **Quantile spread**: excess return of top-20%-score bucket − bottom-20%-score bucket

---

## 8.5 Decision gates

| Checkpoint | Criteria | If not met |
|---|---|---|
| 2 weeks | Pipeline runs without interruption, zero data-consistency errors, `ambiguous` < 30%, inter-model polarity correlation > 0.5 | Halt signal work, repair the measurement layer |
| 3 months | ICIR > 0.3, shadow portfolio beats KODEX 200 buy-and-hold | Discard the signal logic and redesign |
| 6 months | Above conditions hold even after accounting for transaction costs (fees + transaction tax + slippage) | End the project, switch to indexing |

**Any real-money trading, even small amounts, only begins after passing the 3-month gate.** Changing trading behavior based on 1–2 week results means chasing noise.

---

## R. Revision log

SPEC §0 principle 6 requires that any revision to these criteria be recorded. Append a row; never edit or delete a previous row.

| Date | Section | What changed | Why | Data already seen? |
|---|---|---|---|---|
| 2026-08-02 | — | Initial freeze. Split out of SPEC §8 unchanged. | Pre-registration must exist as a standalone committed artifact before collection begins. | No — `data/raw/` empty, no collector run |
