# PREREGISTRATION §8.3 gate measurement: plan

**Status (2026-08-14): implemented, not yet run.** `live_examples()`, the
`source` parameter on `run()`, `gate_report()`, and the `bakeoff gate` CLI
command described below all exist now in `src/eval/bakeoff.py`, ahead of the
2026-08-15 to 17 window this plan named for having enough live articles.
Everything under "What's needed" is done exactly as scoped — nothing beyond
it (`data/scores/`, `collect_daily.py` wiring, `news_polarity`) was touched,
per this plan's own scope boundary.

**To run it** (spends the real ~$4.44 once enough of the window has
accumulated — dry-run first with `--limit`):

```bash
uv run python -m src.eval.bakeoff gate --start 2026-08-12 --end 2026-08-26 \
  --limit 5   # dry run, ~15 calls, confirms keys/schema before the real spend
uv run python -m src.eval.bakeoff gate --start 2026-08-12 --end 2026-08-26
```

Verified 2026-08-14 with `--limit 0` (zero model calls, free): sampling 300
rows from the real `data/raw/kr/news/` window, the CLI, and `gate_report`'s
formatting all run end-to-end without error. `uv run pytest -m "not network"`
covers `live_examples` (window filtering, row shape, empty-corpus cases) and
`gate_report` (pass/fail against `GATE_POLARITY_BAR`) with synthetic data —
no real spend in the test suite.

`--end` above uses the window's actual close (2026-08-26) rather than
whatever the run date happens to be — nothing requires stopping at "today,"
and a wider end date simply includes fewer future rows until they exist.

---

Written before implementation, 2026-08-13. Not a SPEC §12 numbered step —
this is the measurement PREREGISTRATION §8.5's 2-week gate names as its
fourth criterion (inter-model polarity correlation > 0.5), separate from and
higher priority than [`notes/step6-plan.md`](step6-plan.md).

## What the gate actually requires — read directly from PREREGISTRATION, not inferred

§8.3: *"Run the same articles through 3 different models and look at the
score correlations... This only needs a few hundred articles, so 2 weeks is
plenty."* §8.5's fourth criterion: `inter-model polarity correlation > 0.5`,
measured "against the same window's articles" (2026-08-12 → 2026-08-26).

Two things this is **not**, despite an earlier misreading in this project's
own review process:

- **Not a daily production requirement.** A one-time (or few-time) sample of
  a few hundred live-window articles, scored once each by the bake-off's
  three candidates, is what the criterion asks for. It does not need the
  daily pipeline scoring every article of every day.
- **Not blocked on `data/scores/` or production wiring.** PREREGISTRATION
  §8.3 (lines 84-86) says this explicitly: the production scoring archive and
  `scripts/collect_daily.py` wiring are "not yet built... deferring a
  measurement is legitimate, quietly dropping it is not" — and separately,
  "this is not a §8.5 gate criterion, and none is being added." Building
  that archive is real future work (feeds SPEC §6.3's reproducibility
  archive and eventually `news_polarity` production), but it is not this
  task.

**Also not as urgent as it first looked.** `data/raw/kr/news/` is already
committed hourly by `collect-news.yml`, independent of anything in this plan.
Raw article text does not expire — scoring it can happen retroactively at any
point before 2026-08-26 against already-archived text with an identical
result to scoring it same-day. The only real deadline is leaving enough time
after the measurement to react if it fails (§8.5: "halt signal work, repair
the measurement layer").

## What's needed — small, and mostly already built

`src/eval/bakeoff.py`'s `run()` and `inter_model()` were checked against this
requirement directly:

- **`inter_model()` (`bakeoff.py:602`) needs no golden label at all** — it
  computes Spearman correlation between two candidates' own `Attempt` scores
  on their shared article keys. Confirmed by reading it: `_first_pass()` pulls
  from `attempts`, never from `LABELS`.
- **`run()` (`bakeoff.py:295`) is hardcoded to `examples()`**, which sources
  from `data/golden/triage.jsonl` + `data/golden/v1.jsonl` (`bakeoff.py:214`).
  That is the one piece of new code this needs: a live-corpus equivalent of
  `examples()`.

**New function, shape only, matching `examples()`'s row contract**
(`article_id`, `ticker`, `name`, `title`, `description` — the fields
`score_article`/`render_user` actually read, per `src/llm/score.py:89-96`):

```
def live_examples(window_start: date, window_end: date, *, sample_size: int = 250, seed: int = ...) -> list[dict]:
    # 1. Read data/raw/kr/news/ for the window (already collected, no new fetch)
    # 2. Run src/entity/resolve.py to get (article, ticker) pairs — already built
    # 3. Sample `sample_size` pairs, deterministically (seeded), matching
    #    scripts/golden.py's own sampling discipline (spread across tickers,
    #    not weighted toward whichever ticker has the most coverage)
    # 4. Shape each row identically to examples()'s output, minus the `label`
    #    key (not needed — inter_model() doesn't read it)
```

`run()` needs one small signature change to accept this: an optional
`source: list[dict] | None = None` parameter, defaulting to `examples()` when
absent — the same pattern already used for `scripts/golden.py run_label`'s
`source` override. Everything else in `run()` (pacing, retry, per-call
recording) is reused unchanged.

## Sample size and cost — stated up front, not buried

§8.3's own words: "a few hundred articles." Target **200–300**. At
measured per-call rates (`config/models.yaml`'s bake-off decision comment):
`gpt-5.4` ~$0.0042/call, `gemini-3.5-flash` ~$0.006/call, `claude-sonnet-5`
~$0.0046/call. **300 articles × 3 models ≈ $4.44, one-time** — not a
recurring monthly cost, which is the distinction that matters against
Ricky's stated tolerance (a bounded ~$5 one-off is a different kind of ask
than a $30/month subscription-shaped cost).

> **Updated 2026-08-13**, replacing `gpt-5.1` with `gpt-5.4` above: the
> scoring model changed the same day (`config/models.yaml`, PREREGISTRATION
> §R 2026-08-13). This measures the models actually in play now, so it tracks
> whichever one `scoring` names — re-check this figure if that changes again
> before this measurement runs.

Only 1 repeat per model is needed here (this measures *inter*-model
agreement, not self-consistency — that used the bake-off's 5 repeats and is
already measured). No repeats multiplier on the cost above.

## Timing

Not run today (2026-08-13) — the window opened 2026-08-12 and only a day or
two of live articles exist so far, short of the 200–300 target. Wait for
enough window articles to accumulate (roughly 2026-08-15 to 17, at ~100+
resolved pairs/day), then run. That still leaves over a week of buffer before
2026-08-26 to react if the correlation comes in under 0.5.

## Scope boundary

Builds `live_examples()` (or equivalent) in `src/eval/bakeoff.py` and the
`source` parameter on `run()`, plus a small script/CLI entry point to trigger
the live measurement and print the same `inter_model()` table the golden-set
report already prints. Does **not** build `data/scores/`, does not wire
anything into `scripts/collect_daily.py`, and does not touch
`config/rating.yaml`'s `news_polarity` weight — none of those are required by
the gate criterion, per PREREGISTRATION §8.3's own text quoted above.

## Verification

- `uv run pytest -m "not network" -q` stays green with the new `source`
  parameter (default-argument change, existing golden-set bake-off calls
  unaffected).
- A dry run against a small `limit` first (matching the existing `--limit`
  pattern in `bakeoff.py main()`) before spending the full ~$4.44.
- Record the result in PREREGISTRATION §R regardless of outcome — pass or
  fail, per the project's own stated discipline that a measurement, once
  taken, is recorded rather than quietly reused or dropped.
