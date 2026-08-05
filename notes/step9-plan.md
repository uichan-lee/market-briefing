# Step 9 — feature computation: plan

Written before implementation, on 2026-08-05. SPEC §12 step 9, feeding
`src/report/rating.py`.

## What this has to produce

`rate()` takes a mapping of feature name → 252-day rolling z-score, and
renormalizes over whatever is present. So the deliverable is: for each
(ticker, session), a z-score per feature, with `None` where it could not be
computed.

## What is actually computable

Checked against the collector schemas rather than against SPEC's wish list.

| feature | weight | source | build? |
|---|---|---|---|
| `foreign_flow_5d` | +0.30 | `kr_flow.foreign_net`, `trading_value` | ✅ |
| `inst_flow_5d` | +0.15 | `kr_flow.inst_net`, `trading_value` | ✅ |
| `short_ratio` | −0.10 | `kr_flow.short_balance`, `shares_outstanding` | ✅ |
| `valuation_band` | +0.05 | `kr_flow.pbr` | ✅ |
| `rel_strength_20d` | +0.15 | `kr_price.close`, sector from `watchlist.yaml` | ✅ |
| `news_polarity` | +0.20 | LLM scoring, SPEC §6.2 | ❌ stage does not exist |
| `rev_4w` | +0.15 | **consensus EPS** | ❌ **no source in this repo** |

**`rev_4w` is a real gap, not an oversight.** SPEC §5 defines it as the 4-week
change in *consensus* EPS — forward analyst estimates. pykrx's
`get_market_fundamental_by_date` returns `EPS`, which is **trailing**. Using it
would produce a number that looks like the feature and is not it. Getting the
real thing needs an estimates vendor (FnGuide, QuantiWise) or DART analysis, and
that is a source decision for Ricky, not something to improvise here.

Computable weight is 0.75 of 1.10 — 68%, above `min_weight_coverage: 0.5`. So
real ratings are reachable now, with `weight_coverage` recording that two
features are absent. That is the whole point of doing step 9 before steps 6–8.

## Design decisions

**Trailing window excludes the current row.** SPEC §5 writes the z-score as
μ over `t-252 : t-1`. So the mean and standard deviation come from the 252
sessions *before* t, never including t itself. This is not a detail: including
the current row leaks the observation into its own normalization and shrinks
every extreme value toward zero, which is exactly the kind of quiet look-ahead
CLAUDE.md forbids.

**Fewer than 252 prior observations → `NaN`.** Not a shorter window. A z-score
computed over 40 days is not comparable to one computed over 252, and mixing
them across tickers would make the weighted sum meaningless. SPCX-style recent
listings simply have no features for their first year; `rate()` already handles
absence by renormalizing.

**Zero variance → `NaN`, never infinity.** A constant feature over the window
means the denominator is zero. Emitting `inf` would dominate every weighted sum
it entered.

**Nulls stay null.** `kr_flow` uses nullable `Int64` because a halted session
and an absent one are different facts. Filling with zero would state that no
foreigner bought that day, which is a claim about the market rather than about
the data.

**Look-ahead is enforced on `known_at_utc`, not `date`.** Both schemas carry it;
for KR it is the 15:30 KST session close. A row is usable only when
`known_at_utc < as_of`.

## Open questions — flagged rather than guessed

1. **`valuation_band` is normalized twice.** It is defined as a 3-year PBR
   percentile, which is already a bounded rank; SPEC §5 then says every feature
   is z-scored over 252 days. Z-scoring a percentile is defensible but odd. The
   implementation follows SPEC literally and this note exists so the choice is
   visible. Worth revisiting at MANUAL-TASKS §6 calibration.
2. **Sector return for `rel_strength_20d`** is the equal-weighted mean of
   watchlist tickers sharing a `sector` value. With 31 tickers some sectors hold
   one name, where relative strength is identically zero and carries no
   information. Options are to fall back to a market return or to emit `None`;
   this is a judgment about what the feature means, so it is Ricky's call.
   Implemented as `None` for now — a silent zero would read as "no divergence"
   when the truth is "nothing to compare against".

## Scope boundary

Builds `src/features/normalize.py` and `src/features/compute.py` with offline
tests. Does **not** build the LLM scoring stage, the embedding pipeline, or the
report renderer.

---

# Review of the plan above, before implementing

Re-reading it against the actual backfill window found one thing that decides an
open question and one that changes the output shape.

## The 3-year percentile and the 252-day z-score cannot both apply

Measured: the backfill window holds **728 KR sessions**. What each feature needs
before it can emit its first value:

| feature | window | + z-score | needs | 728 available? |
|---|---|---|---|---|
| `foreign_flow_5d` | 5 | 252 | 257 | ok |
| `inst_flow_5d` | 5 | 252 | 257 | ok |
| `short_ratio` | 1 | 252 | 253 | ok |
| `rel_strength_20d` | 20 | 252 | 272 | ok |
| `valuation_band` | **756** | 252 | **1008** | **impossible** |

Applying both would make `valuation_band` permanently `NaN`, and it would look
like a bug rather than a design consequence.

**Decision: `valuation_band` is the percentile and is not additionally
z-scored.** SPEC §5's blanket z-score exists for one stated reason — "absolute
values cannot be compared across tickers." A percentile rank is *already*
ticker-relative and bounded to [0, 1], so the z-score buys nothing here while
costing 252 sessions of history. This is a deliberate, stated deviation from the
letter of §5 in service of its purpose, recorded here and in the module.

It is scaled to `(0.5 − pct) × 2`, giving +1 at the cheapest end of the 3-year
band and −1 at the most expensive, so the sign matches its `+0.05` weight.

**Consequence worth acting on:** even without the second normalization,
`valuation_band` needs 756 sessions and the backfill holds 728. It is 28
sessions short. **Extending the backfill to four years turns this feature on.**
That is a cheap follow-up and belongs to Ricky's call, not to this step.

## Two more things the first draft left unspecified

**Division by zero in the flow features.** `foreign_flow_5d` divides a 5-session
net-buying sum by a 5-session trading-value sum. A ticker halted across all five
sessions gives 0/0. Result is `None`, not zero — zero would assert balanced
flow on a ticker that did not trade.

**Output carries both the raw value and the z-score.** `rate()` only needs the
z-score, but MANUAL-TASKS §6 calibration needs to see the raw distributions to
judge whether the cut points are sane, and a z-score alone cannot be sanity
checked against a broker screen. Written as `{feature}` and `{feature}_z` in
`data/features/YYYY-MM-DD.parquet`, per SPEC §3.3.

## Revised coverage

With `valuation_band` absent until the window extends, four features carry
0.70 of 1.10 — 64%, still above the `min_weight_coverage: 0.5` floor.
