# Step 11 — daily collection, delivery, and the Actions schedule: plan

Written before implementation, on 2026-08-06. SPEC §12 step 11, SPEC §1.

Step 10 produced a readable briefing from the backfill. Step 11 makes it happen
by itself, every day, from fresh data.

## Assumptions

1. **Nothing collects prices today.** `.github/workflows/collect-news.yml` is the
   only workflow in the repository. `kr_price`, `kr_flow`, `us_price` and `macro`
   have run only through `scripts/backfill.py`, by hand. The backfill ends
   2026-08-03, so a report rendered tomorrow would silently use four-day-old
   data — this step is what stops that.
2. **Only `kr_news` has a CLI.** The other four collectors have no `main()`, so
   there is nothing for a workflow to call yet.
3. **Still no LLM.** Sections ④⑤⑧ stay absent; that is step 6–8 work.
4. **Two decisions stay parked**: `rev_4w`'s source and the four-year backfill.

## What this step has to produce

| | Deliverable |
|---|---|
| a | A daily collection entry point for the four price/macro collectors |
| b | `src/notify/email.py` — the mobile reading path, deferred from step 10 |
| c | `.github/workflows/report.yml` — the two SPEC §1 runs |
| d | A failure notice through the delivery channels when no report is produced |

## Three constraints, measured rather than assumed

### 1. `kr_flow` costs 124 KRX requests per run, and KRX blocks near 250

Counted from `src/collectors/kr_flow.py`: four endpoints per ticker
(`trading_value`, `market_cap`, `shorting_balance`, `fundamental`), 31 tickers.
The count is independent of the date range — one day costs the same as one year.

Two report runs a day would spend 248 requests against a block observed at
roughly 250, and a block lasts hours and **is sustained rather than cleared by
retrying**. That is too close to run into deliberately.

**Decision: only the evening run touches KRX.** This is also the correct answer
on its own terms, not merely the safe one:

| Run | KST | KR session state | Collects |
|---|---|---|---|
| `RUN_EVENING` | 21:30 | closed 15:30, final | `kr_price`, `kr_flow`, `macro` |
| `RUN_MORNING` | 07:00 | not yet open | `us_price`, `macro` — **no KRX call** |

At 07:00 KST the KR session being reported on has not happened. Calling KRX then
would spend 124 requests to re-fetch what the evening run already stored.

### 2. Alpaca refuses any `end` at or after the current UTC day

Measured 2026-08-06 at 05:54 UTC:

```
end=2026-08-04  → 1 row      OK
end=2026-08-05  → 1 row      OK
end=2026-08-06  → HTTP 403   "subscription does not permit querying recent SIP data"
```

**This collides with SPEC §1's 07:00 KST morning run.** 07:00 KST on day D is
22:00 UTC on day D−1, so the newest `end` Alpaca will serve is D−2 — while the
US session that just closed (2 hours earlier, at 20:00 UTC D−1) is exactly the
one the reader wants. The morning briefing would carry US data one full session
stale, which guts §2.2①, the front page.

Tiingo behaves differently in kind: `end=2026-08-06` returned **empty**, not a
403. It has no policy restriction, only an absence of data that does not exist
yet — so it may well serve day D−1 at 22:00 UTC D−1. That cannot be tested at
any other hour, so:

**Probe first, decide after.** A single `workflow_dispatch` run at 22:00 UTC
asking both vendors for that day's session settles it. Options, in the order
they should be preferred:

| | Option | Cost |
|---|---|---|
| a | Tiingo serves the morning run, Alpaca the evening one | Two vendors on one path; the cross-check becomes load-bearing |
| b | Move `RUN_MORNING` to 09:10 KST (00:10 UTC) | Fresh US data, but after KOSPI opens — loses SPEC's "2 hours before open" |
| c | Keep 07:00, accept one stale US session | Guts §2.2① |

**Regardless of the outcome, the header states the date of the US data it used.**
That is the part which must not wait for the probe: a stale number that says so
is a different thing from a stale number that does not.

### 3. GitHub drops scheduled runs

Already recorded in SPEC §1 and in `collect-news.yml`: of the first four
`0 * * * *` firings, three never ran. Never schedule on `:00`; never treat a
schedule as coverage.

Applied here as `22:07 UTC` and `12:37 UTC` — not round, not contended.
Korea has no DST, so KST is UTC+9 year-round and these are fixed. The DST
warning in SPEC §1 concerns the *US* close, which matters only for whether the
previous US session is complete — it is by 07:00 KST under either regime
(05:00 KST during DST, 06:00 outside).

## Design decisions

**Collectors report failure; the workflow still renders.** CLAUDE.md requires a
partial report over no report, and step 10 already renders a stated absence for
missing input. So a collector failure becomes a header line, not a failed job.
The workflow fails only if the *renderer* fails.

**One entry point, not four.** `scripts/collect_daily.py --market kr|us` runs
the collectors for one side, writes to `data/raw/`, and prints each report.
Adding a `main()` to each collector would spread the same run-and-report logic
four ways; `kr_news` keeps its own because its schedule is unrelated.

**Failure notice reuses the delivery layer.** `src/notify/base.py:deliver`
already exists and reports rather than raises. A failure notice is a short
markdown document through the same channels — not a second mechanism, so a
channel added later gets failure notices without being told to.

**Email sends the header plus ⑥, not the whole document.** `delivery.yaml`
already carries `body: summary`. The full briefing is 267 lines of tables that
read badly on a phone; the vault copy is the desktop path and is complete.

**The workflow commits `reports/` and `data/ratings/`, with the same retry loop
as the news job.** Both workflows push to `main`, so they race; `collect-news`
already rebases and retries three times, and copying that is right rather than
inventing a second approach.

## What this step does not build

- No LLM, no prompts, no `src/llm/adapter.py`.
- **No webhook adapter.** SPEC §2.0 says implement and leave inactive, but
  CLAUDE.md rule 5 forbids a channel absent from `delivery.yaml`, and it is
  absent deliberately. It arrives when a row is added.
- No calendar collector — ④ stays a stated absence.
- No shadow-portfolio arithmetic. ⑦ keeps counting sessions until there are
  enough to compute against.

## Verification

1. `uv run pytest -m "not network"` — 399 existing plus new; output shown.
2. `uv run ruff check .` and `uv run ruff format --check .`.
3. **`scripts/collect_daily.py` run locally for both markets**, against the real
   APIs, and the resulting parquet compared against the backfill's last day for
   schema and dtype agreement.
4. **The 22:00 UTC vendor probe**, before the morning run's source is fixed.
5. A report rendered from freshly collected data, read end to end.
6. Email sent to the configured address and read on a phone — `body: summary`
   is a claim about legibility and cannot be verified from a test.
7. Both workflows triggered by `workflow_dispatch` before either is left to its
   schedule, and the push-race checked by running them at the same time.
8. A deliberately broken collector (bad credential) run through the whole path,
   confirming the report still publishes and the header names the failure.

## Open question for Ricky

**Which option for the morning run's US source** — a, b, or c above. The probe
in verification step 4 supplies the fact; the trade-off is a judgment about
whether the briefing arriving before KOSPI opens matters more than its front
page being current. Everything else in this step proceeds regardless.
