# Model bake-off decision: record

Written 2026-08-14, moved out of `MANUAL-TASKS.md §5` the same day — this is
an engineering decision record, not a Ricky action item, so it belongs here
rather than in the file scoped to "work Claude cannot do on Ricky's behalf."
`MANUAL-TASKS.md §5` keeps the decision rule, the re-run commands, and the
standing monitoring responsibility (watching whether the OpenAI free-token
pool stays enabled); this file keeps the history of how the current choice
was reached.

**Status: closed. `gpt-5.4` is the decision, recorded in `config/models.yaml`.**
Chosen 2026-08-12 as `gpt-5.1`, replaced by `gpt-5.4` one day later. Below is
why.

> **Production status, 2026-08-29:** the model decision is unchanged, but the
> selected scorer has not run in Actions because `OPENAI_API_KEY` is not a
> repository secret. The pending 2026-08-28 change adds `data/scores` staging,
> bounded checkpointing, and active-model filtering; it must be reviewed,
> rebased, and deployed before the key is registered. `news_polarity` may then
> accumulate as a diagnostic, but its rating weight is frozen until after the
> 2026-11-13 gate.

## Round 1 (2026-08-12)

1,117 calls run. `claude-sonnet-5` and `gpt-5.1` both passed the bar, and per
the standing rule ("among candidates passing, take the lowest cost per valid
signal") `gpt-5.1` was chosen — 8.3× cheaper (~$5/mo vs ~$28/mo at ~140
calls/day). `sonnet-5` led on relevance (0.83 vs 0.76), but the rule selects
by cost among passing candidates, not by rank. `gemini-3.5-flash` was
disqualified **on unmeasured self-consistency, not on price** — its free
tier's 20-calls/day cap forced `--repeats 1`, so σ was absent rather than
failing. In the course of this round the golden set's `relevance` dimension
was fully relabeled for all 100 examples (three models failing in parallel
turned out to be a rubric problem, not a model problem).

## Round 2 (2026-08-13, Ricky asked for a re-check after reading about GPT-5.6 Luna's 80% price cut)

Two things surfaced:

1. **The original bake-off never surveyed the OpenAI lineup properly.**
   `gpt-5.1` was picked only because `gpt-5` rejects `temperature=0` and
   `5.1` was the next version that accepted it — nobody had checked whether
   a newer generation existed. By 2026-08-11, `gpt-5.2`, `gpt-5.4`, `gpt-5.5`,
   and `gpt-5.6` had already shipped.
2. **OpenAI's data-sharing free-token program** (confirmed on Ricky's account
   dashboard, 2026-08-13): `gpt-5.4`, `gpt-5.2`, `gpt-5.1` and others share a
   250K-token/day free pool. **None of the `gpt-5.6` family (sol/terra/luna)
   is in that pool.** Luna's "80% cheaper" sticker price is therefore worse
   in practice than a free-tier model at this volume — the free pool erases
   the headline discount's relevance.

Re-running the bake-off with `gpt-5.4` added (plus `gpt-5.6`, `gpt-5.6-luna`,
`gpt-5.6-terra`, `gpt-5-nano`, `gpt-5.4-nano` for completeness): `gpt-5.4`
passed and beat `gpt-5.1` on relevance (0.78 vs 0.76), polarity (0.86 vs
0.82), and especially uncertainty (0.55 vs 0.43 — a real difference, outside
the noise floor) — at the same effective cost, since both draw from the same
free pool. `gpt-5.1` → `gpt-5.4`. `gpt-5.6` (sol) and `gpt-5.6-luna` also
passed the bar but sit outside the free program, so both cost more in
practice than `gpt-5.4` despite `gpt-5.6-luna`'s lower sticker price.
`gpt-5-nano` (relevance 0.47) and `gpt-5.4-nano` (relevance 0.48, σ(pol)
0.113) failed outright; `gpt-5-nano` additionally ran 17.4s mean latency
against 1–5s for everything else. `gpt-5.6-terra` was stopped by Ricky after
10 dry-run calls, before a full run. Full basis:
`config/models.yaml`'s `scoring:` comment and the
[2026-08-13 PREREGISTRATION §R entry](../PREREGISTRATION.md).

## The manual confirmation that made the decision valid

The `gpt-5.4` choice only holds if the free-token pool is actually on — at
sticker price `gpt-5.4` is 4.4× more expensive than `gpt-5.1` ($0.0448 vs
$0.0101 per valid signal), and the standing rule takes the *cheapest*
passing candidate. **The only basis for choosing `gpt-5.4` over `gpt-5.1`
is "both are effectively $0 under the free pool."** If sharing were off, the
rule would have selected `gpt-5.1`.

This is an OpenAI account-level setting — nothing in this repository can
observe or automate it. Ricky confirmed by eye on 2026-08-13: OpenAI
dashboard → Data Controls → "Share inputs and outputs with OpenAI" reads
`Enabled for all projects`, with `You're enrolled for complimentary daily
tokens.` displayed on the same screen. That confirmation is what closed the
decision (`MANUAL-TASKS.md`'s former item 16).

**The tradeoff for the free tokens:** the scoring prompt (article title,
summary, ticker) is shared with OpenAI for training. All of it is already-
public RSS text, so nothing new leaves the project, but it's a real term of
the deal and worth stating once. Also worth noting: sharing applies only to
traffic *after* it was enabled, so none of the 1,117 round-1 bake-off calls
were shared — this doesn't affect the cost comparison above.

**This can silently revert.** It's an account setting, not code, so the
repository cannot detect if it turns off. If the free pool disappears,
`gpt-5.4`'s justification disappears with it — an OpenAI invoice showing
scoring costs is the signal to re-open this decision (`MANUAL-TASKS.md §5`'s
rule would then select `gpt-5.1` again).

## Noise floor, as read on 2026-08-13

Before ranking models on any bake-off table, check
[PREREGISTRATION §8.3](../PREREGISTRATION.md)'s per-dimension noise floor —
a gap smaller than the floor is not evidence. As of 2026-08-13, all five
floors had just been remeasured and three got worse: `polarity` 0.07 →
0.095, `intensity` 0.07 → 0.140, `forwardness` 0.13 → 0.205. Margins that
cleared the old floors don't necessarily clear the new ones.
**`forwardness` (±0.205) cannot be used to rank models at all** — it's wider
than any difference this bake-off can produce. Models coming out similar is
the expected outcome (live benchmarks generally find architecture dominates
and model choice is marginal); when that's true, the standing rule (cheapest
passing candidate) is exactly the right way to decide, and re-running the
bake-off hoping for a clearer signal is the behavior PREREGISTRATION exists
to prevent.

These specific numbers are a snapshot of the golden set as it stood on
2026-08-13 and will go stale the next time the set is relabeled — read
PREREGISTRATION §8.3 directly for the current floor rather than trusting
this record for anything but history.
