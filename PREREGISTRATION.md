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

### The golden set's own noise floor, measured 2026-08-10

Model agreement is read against a human standard, so it cannot be read more finely than that standard agrees with itself. `golden recheck` re-labelled 10 of the 100 examples a day later without showing the first answers; the per-dimension mean absolute gap between the two passes is the floor below which a model's disagreement with the set is indistinguishable from Ricky's disagreement with himself.

| dimension | mean \|gap\| | signed mean | direction of the moves |
|---|---|---|---|
| `uncertainty` | 0.03 | −0.02 | 3 of 10 moved |
| `relevance` | 0.07 | −0.05 | 5 down, 1 up |
| `polarity` | 0.07 | +0.02 | 2 down, 4 up |
| `intensity` | 0.07 | +0.05 | 1 down, 5 up |
| **`forwardness`** | **0.13** | **−0.10** | **5 down, 1 up** |

Aggregate: mean 0.16, max 0.25 across per-article worst-dimension gaps, against the 0.25 threshold `verify` fails on. The set passes.

**`forwardness` is floored at ±0.13 and the other four at ±0.03–0.07.** Every ±0.25 deviation in the recheck was `forwardness`, and the moves have a direction rather than scattering: the second pass scored it lower on 5 of the 6 examples where it moved. The three largest were 확정됐지만 처음 알려진 사실 — a cancelled supply contract, a reported booking metric, a newly signed agreement — which is the exact case the schema's own note sends the other way (`확정된 사실도 처음 알려졌다면 forwardness는 높다`). The written hint and Ricky's working judgement disagree, the same defect `relevance` had on 2026-08-07.

**Consequence, fixed in advance:** a `forwardness` agreement difference between two models smaller than 0.13 is not evidence that one scores it better, and will not be reported as such. `polarity` is unaffected — its floor is 0.07, well inside the §8.5 gate's 0.5 correlation criterion. The schema was deliberately **not** revised to fix this; see the §R entry for why.

---

## 8.4 Signal evaluation design (after 3 months)

**Evaluate on excess return, not absolute direction.**

$$y_{i,t+1} = r_{i,t+1} - r_{\text{sector}(i),t+1}$$

Removing market beta sharply reduces cross-ticker correlation, which rescues the effective sample size. The evaluation metrics are not hit rate but:

- **IC (Information Coefficient)**: Spearman correlation between score and next-day excess return, logged as a daily time series
- **ICIR**: mean IC ÷ stdev of IC
- **Quantile spread**: excess return of top-20%-score bucket − bottom-20%-score bucket

**Which "score" is evaluated.** Both, reported separately and never pooled:

1. The **composite rating score** from SPEC §2.2⑥ — the continuous value, not the seven-point bucket. This is the pipeline's actual output and the thing the shadow portfolio trades.
2. The **news polarity score** from SPEC §6.2, aggregated per ticker — retained as a component-level diagnostic. If the composite carries signal while its news component does not, the LLM stage is decoration and should be cut.

The continuous composite is what enters IC, not the bucket label. Bucketing discards information and its cut points are a display choice; correlating on the label would confound the signal's quality with the arbitrariness of where the thresholds sit.

**Cut points are frozen against outcome data.** The seven-point thresholds in SPEC §2.2⑥ may be adjusted for distributional reasons — for example if 95% of tickers land in `관망`, making the scale useless — but never because a different cut would have produced better-looking returns. Any change is logged in §R with which of those two reasons applied.

**What the composite contains.** As of the v0.5 revision the composite includes medium-term per-ticker features (`rel_strength_120d`, `flow_persistence_60d`, `rev_trend_12w`) alongside the original short-horizon set. The metric is unchanged; the input is not, and that distinction is exactly what a pre-registration exists to record. This was fixed before any collector ran.

**The macro regime layer is excluded from the composite, on measurement grounds.** SPEC §2.2⑨ indicators are common to every ticker on a given day. Since the evaluation target above is *sector-excess* return, a common term cancels by construction — it can add variance to the composite but cannot contribute IC. Folding macro into a per-ticker score would therefore degrade a measurement while appearing to enrich it. The regime is reported as its own section and evaluated, if at all, as a separate question.

**SPEC §2.2⑧ commentary is outside evaluation entirely.** It enters no IC, no ICIR, no quantile spread, and no shadow portfolio position. It is LLM-authored prose and therefore not reproducible; admitting it to the evaluation path would make the results depend on a model version and a sampling temperature. It is checked for consistency against the rating (`src/report/consistency.py`) and otherwise treated as a display artifact. Whether it is *useful* is a format question, measured in the trial log at MANUAL-TASKS §8, not a signal question.

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
| 2026-08-02 | §8.4 | Specified which score enters IC: the continuous composite rating score (SPEC §2.2⑥) as primary, aggregated news polarity as a component diagnostic, reported separately. Added that cut points may be revised for distributional reasons but never against outcome data. | Ricky's design change added a directional rating as the pipeline's actual output. §8.4 previously said "the score" when only one existed; with two, leaving it ambiguous would let the more flattering one be chosen after the fact. | No — still zero collectors run, `data/raw/` empty |
| 2026-08-03 | §8.4 | Recorded that the composite now includes three medium-term per-ticker features (SPEC §5 v0.5), and that the SPEC §2.2⑨ macro regime layer is excluded from it. | Ricky observed the pipeline had no horizon beyond ~60 days. The metric is unchanged but its input is not, and a pre-registration that does not record a changed input is not recording anything. Macro is excluded on measurement grounds, not preference: it is common across tickers and cancels in the sector-excess target, so it cannot show IC. | No — still zero collectors run, `data/raw/` empty |
| 2026-08-03 | §8.4 | Declared SPEC §2.2⑧ AI commentary outside the evaluation path — no IC, no ICIR, no quantile spread, no shadow portfolio position. | The briefing gained an LLM-authored prose section. Fixing its status now prevents a later argument that the commentary "also predicted" something; non-reproducible text cannot be evaluated, and its usefulness is a format question for the trial log instead. | No — still zero collectors run, `data/raw/` empty |
| 2026-08-08 | §8.3 | Recorded how the golden set that §8.3 measures against was actually produced. Every one of the 500 values was written by Ricky; no model supplied, proposed, or corrected a number, and none ever may. But two dimension **definitions** were sharpened with Claude's input while the labelling was under way: `relevance` was moved onto "does this touch the income statement" on 2026-08-07, after its written hint and its numeric anchors were found to disagree, and the `intensity` procedure was pinned to conditional size on 2026-08-08. Rule-based flagging (`score_conflicts`) additionally sent 15 scores back to be re-labelled, across 14 of the 100 examples, on top of 9 bucket changes at review. | §8.3 ranks models by agreement with this set, which only means something if the set is an independent standard. Definitions co-authored with one of the candidate models are a weaker form of the contamination SPEC §7.3 exists to prevent — weaker because a definition applies uniformly to all 100 examples and was written down before the answers it would change were seen, but not zero. A bake-off that ranks Claude against a schema Claude helped word has to say so. `review_influence` and `redo_influence` in `scripts/golden.py` report the two countable shares (8% and 14%); the definition changes touch every example and cannot be counted at all, which is why they are recorded here instead of measured. | **Yes, partly.** The labels were visible when the definitions were revised — the `relevance` defect was found by measuring Ricky's own scores against the anchors. No outcome data was: no price, return, or realised move for any labelled article had been looked at, and SPEC §7.2 forbids it. |
| 2026-08-08 | §8.4 | `load_rating_history()` now selects the newest version per session instead of concatenating every parquet under `data/ratings/`. Measured before the change: 217 rows for 3 real sessions, because 2026-08-06 was re-rendered four times and each render was archived. Newest wins, on the reasoning that a re-render happens when the earlier run was wrong, so keeping the earlier one measures the bug rather than the method; it also keeps the archive agreeing with `reports/`, which a re-render overwrites in place. Nothing was deleted — `write_ratings()` remains append-only and every superseded file stays on disk. | This is the frame §8.4's IC, ICIR and quantile spread are computed over. A quadruple-weighted session is silent corruption of exactly the kind the determinism discipline exists to prevent, and it would be hardest to notice at the moment it mattered most. Recorded here rather than treated as a bug fix because it changes *what the evaluation reads*, which is the one class of change this log exists for. | **No outcome data.** The archive's own contents were seen — that is how the 217 rows were counted — but no return, no IC, and no shadow-portfolio P&L has been computed at any point. |
| 2026-08-08 | §8.4 | Removed 0.35 of weight for features that have no producer (`news_polarity` 0.20, `rev_4w` 0.15) from `weights` in `config/rating.yaml` into a new `deferred_weights` key that `rate()` never reads. Cut points unchanged. Two consequences, both measured against 2026-08-07 and neither reversed: every score is multiplied by 0.75/1.10 = 0.6818, changing 8 of 31 published rating labels and emptying the 매도 bucket; and weight coverage rises by 1.4667×, which cleared all five tickers the `min_weight_coverage: 0.5` floor had been forcing to 관망. The composite is `Σ\|weight\| × z` at full coverage rather than z itself, so the outer buckets now begin at a uniform z of 2.67 where they began at 1.82 — pinned by `test_the_outer_bucket_needs_a_z_of_two_point_seven`. | The weights violated the rule `config/rating.yaml` states about itself and SPEC §5 states about `rate()`: a weight for a feature that never arrives lowers every ticker's coverage and trips the floor by arithmetic rather than by evidence. **The preregistered metrics are unaffected.** The transformation is a single positive constant applied to every ticker, so it preserves rank, and IC (Spearman), ICIR and quantile spread are all rank-based; only the seven-point label moves, and §8.4 already excludes the label from evaluation as a display choice. Cut points were deliberately *not* rescaled to absorb the shift: §8.4 permits revising them for distributional reasons, but doing it inside the same change that moved the scale would make the two indistinguishable afterwards. | **No outcome data.** The published ratings for 2026-08-07 were seen — that is where the 8-of-31 figure comes from — but no realised return for any of those ratings has been looked at, and none exists for the session in question at the time of writing. |
| 2026-08-10 | §8.3 | Recorded the golden set's own per-dimension noise floor from `golden recheck`, and fixed in advance that a `forwardness` agreement difference below 0.13 will not be reported as evidence of model quality. The set passed `verify` (mean gap 0.16 against a 0.25 threshold), so this records a limit on how the passing set may be read, not a defect in it. The schema was **not** revised: `forwardness`'s written hint and Ricky's working judgement demonstrably disagree, but rewording it now would mean editing a definition while its finished labels are visible — the contamination the 2026-08-08 entry already had to declare once. Deferred to a v2 set, where the anchors can be fixed before any label is written. | §8.3 ranks models by agreement with this set. Agreement measured against a standard that disagrees with itself by ±0.13 on a dimension cannot resolve differences finer than that, and without this written down the first bake-off would have reported a `forwardness` ranking that is inside the noise. Recording the floor costs nothing; discovering it after seeing a ranking would be unrecoverable, because by then no one could say whether the threshold was chosen to fit the result. | **Yes — the labels, both passes.** That is what was measured. No outcome data: no price, return, or realised move for any labelled article has been looked at, and SPEC §7.2 forbids it. |
