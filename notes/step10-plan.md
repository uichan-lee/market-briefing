# Step 10 — report renderer: plan

Written before implementation, on 2026-08-06. SPEC §12 step 10, consuming
`src/features/compute.py` and `src/report/rating.py`.

Direction and ordering rationale: [review-2026-08-06.md](review-2026-08-06.md).

> **Status updated 2026-08-29 (pre-build snapshot kept as history below).** The
> renderer ships; more than 30 briefings are in `reports/`. The rendered-section examples
> below showing `0.75/1.10` and `미구현 섹션: ④⑤⑧` are stale — ④ went partial
> 2026-08-14, ⑤/⑧ were wired 2026-08-25, and the design weight total is 0.95
> after `rev_4w` was dropped. The pending 2026-08-28 change closes the known
> consistency-guard bypasses and renders ③ polarity, uncertainty, and the
> highest-intensity article from the active score archive. It labels the count
> as pre-dedup while dedup remains unwired. Production LLM output still awaits
> merge, Actions secrets, and a verified workflow run. Current sequence:
> `notes/next-steps-2026-08-28.md`.

## Assumptions

1. **No LLM in this step.** Sections ⑤ and ⑧ are the only LLM-authored parts of
   the briefing and both depend on stages 6–8, which do not exist. They are
   rendered as stated absences, not skipped.
2. **The report ships incomplete on purpose.** CLAUDE.md requires a partial
   report over no report. Every gap appears in the header.
3. **`as_of` is a required argument, not a convenience.** The renderer is a
   feature consumer and inherits the look-ahead rule intact.
4. **Output is Korean**, per CLAUDE.md's delivery section. Code, docstrings and
   commits stay English.

## The goal

One markdown file per run, at `reports/YYYY-MM-DD.md`, that a human reads. Today
there is no artifact at all — the deterministic pipeline terminates in a
DataFrame nobody sees.

## What each section can actually produce

Checked against the collectors and the backfill, not against SPEC's wish list.

| § | Section | Verdict | Evidence |
|---|---|---|---|
| ① | US→KR transmission | ✅ build | 48 US tickers incl. SPY/QQQ/SMH/XLE/XLK/XLF/XBI/IWM, 752 sessions; macro 6 series |
| ⑨ | Medium-term regime | ✅ build | all five indicators present — `yield_curve_10y2y`, `dollar_index`, `usdkrw`, `wti`, plus ETF-vs-SPY 120d |
| ⑥ | Directional rating | ✅ build | `rate()` exists; z-scores real since the backfill landed |
| ② | Watchlist scan | 🟡 partial | **3 of 7 flags** — see below |
| ③ | News aggregation | 🟡 partial | counts and headlines only; polarity is §6.2 |
| ⑦ | Shadow P&L | 🟡 empty on day 1 | needs a rating history that starts accumulating here |
| ④ | Calendar | ❌ absent | no earnings/FOMC/IPO collector exists |
| ⑤ | Red team | ❌ absent | LLM, stages 6–8 |
| ⑧ | AI 총평 | ❌ absent | LLM, stages 6–8 |

### ② — which flags are real

| Flag | Buildable | Why |
|---|---|---|
| `outflow` / `inflow` | ✅ | `foreign_flow_5d` z ≷ ∓1.5, 14,067 z-scores available |
| `volatility` | ✅ | 20-day realized vol from `kr_price`, z-scored the §5 way |
| `news_spike` | ✅ | post-resolution mention counts from `src/entity/resolve.py` |
| `valuation_band` | ❌ | needs 756 sessions, window holds 728 — Ricky's parked decision |
| `earnings_revision` | ❌ | needs `rev_4w`; no consensus-EPS source |
| `filing` | ❌ | `kr_filings` / `us_filings` not built |

Three of seven. The scan is still the most useful section in the briefing
because it is the only per-ticker view, so it ships with the four absent flags
named in a footnote rather than silently missing.

## Design decisions

**An unbuildable section renders as a stated absence, never as omission.** This
is the whole point of doing step 10 now. From the review:
`check_feed_continuity` correctly detected a 5.3-hour hole in `etnews_economy`
and reported it into Actions logs nothing reads. A renderer that quietly drops
what it cannot build reproduces that failure at report level.

```markdown
## ④ 캘린더

_이 섹션은 아직 없습니다 — 실적·FOMC·IPO 일정 수집기가 없습니다 (SPEC §12 미착수)._
```

**The header is the contract.** Rendered first, and it carries every degradation:

```
📅 2026-08-06 (목) 07:00 KST
S&P +0.4% | NASDAQ +0.8% | SOX +1.9% | USDKRW 1,382 (+0.3%)
⚠ 수집 실패: 없음
⚠ 뉴스 유실: etnews_economy 5.3시간 (버퍼가 마지막 저장분을 지나침)
⚠ 등급 근거 충족도: 0.75/1.10 (68%) — news_polarity·rev_4w 부재
⚠ 미구현 섹션: ④ 캘린더, ⑤ 반증, ⑧ AI 총평
ℹ 엔티티 모호 비율: 10.9%
```

The feed-continuity line is the finding this step exists to close.

**Ratings are persisted, not only rendered.** `data/ratings/YYYY-MM-DD.parquet`
per run. Not because ratings are unrecoverable — they recompute from features —
but because `config/rating.yaml` is calibrated later (MANUAL-TASKS §6). A rating
produced today under today's weights cannot be reproduced once the weights move,
and ⑦'s shadow P&L is only meaningful against the rating actually published.
Starting the series now costs one parquet a day.

**The rationale prints its residual.** SPEC §2.2⑥ is explicit: `rationale()`
returns the top 4 contributors while the headline score sums all of them, so the
displayed lines do not add up to the stated score. The renderer emits
`· 그 외 N개 항목  기여 +0.065` whenever terms were dropped. A number on the page
that does not reconcile teaches the reader to stop checking.

**Render order follows SPEC §2.3, which is not ID order.** IDs are stable
identifiers; display order is
`헤더 → ⑧ → ① → ⑨ → ② → ③ → ④ → ⑥ → ⑤ → ⑦`. With ⑧ absent the report opens on ①,
which SPEC calls the most defensible content in the briefing.

## Two defects found while planning

**`sector_mapping.yaml` names `RUT`, which is not collected.** `INDEX_ETFS` holds
`IWM`, the Russell 2000 *ETF*; `RUT` is the index and no collector fetches it.
Rendering ① against the config as written produces an empty row for the
코스닥 중소형 mapping. Options: substitute `IWM` in the config, or have the
renderer fail loudly on an unmapped symbol. **Choice: substitute `IWM` in the
config and note why** — the mapping's purpose is a risk-appetite proxy and the
ETF tracks the index closely enough for a 60-day correlation. Silently rendering
an empty row is the one option ruled out.

**`load_raw` cannot read the macro frame.** It de-duplicates on
`["date", "ticker"]`, and macro is keyed `["date", "series"]` — it raises
`KeyError: Index(['ticker'])`. Fix by parameterizing the key rather than adding
a second loader.

## What this step does not build

- No LLM call, no prompt, no `src/llm/adapter.py` work.
- **No email adapter and no Actions workflow.** Those are step 11–12. This step
  builds `src/notify/base.py` and `src/notify/vault.py` only, so the report
  lands in `reports/` and is readable; wiring it to a schedule is a separate,
  independently verifiable change.
- No calendar collector. It is named as absent, not stubbed with fake data.

## Verification

1. `uv run pytest -m "not network"` — 370 existing plus new; output shown.
2. `uv run ruff check .` and `uv run ruff format --check .`.
3. **Render a real report from the real backfill** for 2026-08-03 and read it
   end to end. A renderer that passes unit tests and produces an unreadable page
   has failed.
4. Confirm the header's `weight_coverage` matches what `rate()` returns, and
   that the rationale lines plus residual sum to the stated score.
5. Confirm a deliberately broken input — a feature frame with one ticker missing
   — degrades to a stated absence rather than an exception.
6. `git diff` shown before committing.
