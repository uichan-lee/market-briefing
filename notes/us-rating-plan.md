# US ticker ratings: plan

> **Status, 2026-08-27:** the KR 2-week measurement gate passed. The deferral
> still runs through the 3-month signal gate on 2026-11-13; no US rating work
> starts merely because the first gate closed. The current production-wiring and
> 3-month-evaluation defects have priority.

Written 2026-08-14, not a SPEC §12 numbered step — a scope gap found while
starting the §2.2④ calendar collector work, and settled with Ricky the same
day.

SPEC §2.2⑥ read "every watchlist ticker gets a rating," but that was never
true in practice. `src/report/render.py`'s rating path is hardcoded to
`load_watchlist(market="KR")`, so only the 31 KR tickers have ever received a
seven-point rating. The 40 US tickers surface only in §2.2① (US → KR
transmission — index/sector-level correlation) and the header's market line;
no individual US ticker has ever gotten a directional call.

This wasn't a decision anyone made — it fell out of building KR-specific
features (`kr_flow`'s investor-type net buying) first.

**Decision: US individual-ticker ratings (§2.2⑥) are deferred until the KR
pipeline clears its gates. §2.2① is unaffected and stays as-is.**

**Why not just widen the scope now.** The active weights in
`config/rating.yaml` (`foreign_flow_5d`, `inst_flow_5d`, `short_ratio`) are
Korean investor-type flow data with no US equivalent computed. Dropping the
`market="KR"` filter would push every US ticker below `min_weight_coverage`
(0.5) and force a manufactured 관망 across the board — that isn't rating US
tickers, it's staging the appearance of rating them, which is worse than
today's honest omission.

**What "later" requires.** A separate US feature set, not a widened watchlist
filter:

- `rel_strength_20d`-style relative strength carries over directly.
- Institutional flow (13F, quarterly) and short interest (FINRA, biweekly)
  publish on a different cadence than KR's near-daily figures — they need
  their own definitions, not a market swap.
- Consensus EPS revision is materially cheaper to source for US tickers than
  for KR ones (investigated 2026-08-14, see MANUAL-TASKS.md §11) — this is
  not blocked the way `rev_4w` is on the KR side.

**When to revisit.** After the KR pipeline clears the 3-month gate on
2026-11-13; the 2-week gate already passed on 2026-08-27. This remains outside
the current queue — the system must first run its existing LLM/filings path in
production and produce a trustworthy KR evaluation.
