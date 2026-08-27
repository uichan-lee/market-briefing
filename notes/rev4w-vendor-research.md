# `rev_4w` vendor research

Written 2026-08-14. Was a research record, not a decision, until
**2026-08-25, when the decision was made: `rev_4w` is permanently dropped**
(see the "Resolved" entry at the end of this file). `MANUAL-TASKS.md §11`
records the decision. This file exists so the investigation already done
isn't lost to chat history, following the same "engineering research
belongs in notes/" principle as
[model-bakeoff-decision.md](model-bakeoff-decision.md).

`rev_4w` (weight 0.15 in `config/rating.yaml`, currently in `deferred_weights`)
needs Korean-listed companies' **consensus EPS estimates** — forward analyst
forecasts, not the trailing figures pykrx provides. No free, ToS-clean source
was found. Below is the full sweep.

## Paid vendors

**FnGuide DataGuide / QuantiWise** — the domestic standard. QuantiWise is a
FnGuide-family product (formerly WISEfn), not a separate competitor.
Enterprise, quote-based pricing — `help-dataguide.fnguide.com` and
`corp.fnguide.com` both route pricing to a sales conversation, no published
individual tier. Given the project's actual budget tolerance (~$1–2/mo,
stated directly by Ricky), a sales-negotiated enterprise contract is very
unlikely to even entertain an individual inquiry at this volume.

**FnSpace (FnGuide's API product) — the one concrete lead.** Checked
`www.fnspace.com` directly:
- **Startup tier:** ₩700,000/mo (financial data) or ₩980,000/mo (consensus
  data) — confirms the enterprise pricing order of magnitude above, not
  affordable.
- **Academy tier (students/graduate students/professors): ₩50,000/mo or
  ₩500,000/yr, VAT excluded.** Confirmed via direct fetch of FnSpace's
  pricing page that this tier **includes** "투자의견&목표주가, 추정실적
  (Fiscal, Daily), Forward 지표" — i.e., consensus data is in scope, not a
  separate add-on.
- **Unresolved:** eligibility requirements aren't published (Korean
  university enrollment vs. any student status — Berkeley wasn't confirmed
  either way). Would need FnSpace's own 1:1 inquiry to check. ₩50,000/mo
  (~$36) is well above the ~$1–2/mo tolerance stated for this project, but
  far closer than every other paid option found — worth a direct inquiry if
  `rev_4w` becomes a priority.

## Free-page scraping — checked and closed, not just unconfirmed

**`comp.fnguide.com` ("Company Guide")** shows the same consensus data
(investment opinions, target prices, per-analyst report summaries) in a free
consumer-facing page. Checked whether this is a viable collector source:

- `robots.txt` for both `comp.fnguide.com` and `fnguide.com` is
  `Disallow: /`, applied to real content paths, not just marketing pages —
  a direct, explicit "no bots" signal, not an ambiguous ToS question.
- Given FnGuide sells the identical data as FnSpace's metered API
  (₩980,000/mo for the consensus tier), the block is clearly commercial —
  scraping the free page would be circumventing the product they charge for.

**Swept 9 sites total** for the same underlying data (Korean and global,
not just FnGuide) to check whether this was a FnGuide-specific block or
structural to the category:

| Site | Result |
|---|---|
| `comp.fnguide.com`, `fnguide.com` | `Disallow: /` |
| `consensus.hankyung.com` (한경컨센서스) | `Disallow: /` |
| `finance.naver.com` | `Disallow: /` for generic bots; only Naver's own crawler (`yeti`) gets a narrow allowlist that doesn't include stock item pages |
| `wsj.com` | `Disallow: /` **plus an explicit legal notice**: automated collection prohibited without written permission, with a licensing contact (`copyright@dowjones.com`) |
| `finance.yahoo.com` | `robots.txt` itself doesn't block `/quote/`, but **Yahoo's Terms of Service explicitly prohibit** "robots, spiders, crawlers, scrapers... without express, prior permission" — robots.txt being open doesn't mean ToS is. A live fetch of a Korean ticker's analysis page also returned 503 (active bot defense). |
| `marketscreener.com` | Akamai returns 403 on the robots.txt request itself — aggressive bot defense before any scraping is attempted |
| `investing.com` | 403, same pattern |
| `finance.daum.net` | Inconclusive (no robots.txt found at the checked path), but consistent with everything else in the sweep |

**Conclusion: this is structural, not a single vendor's choice.** Every
aggregator checked — Korean and international — either blocks outright,
states an explicit anti-scraping legal position, or runs active bot defense.
Analyst consensus data is licensed IP the aggregators paid brokerages for
the right to redistribute; protecting it from free scraping is standard
across the category. Not worth searching further sites on the assumption
one might be more permissive — the pattern across 9 independent sites is
convergent evidence, not an isolated block.

## University-affiliated access (Ricky is a Berkeley student)

Checked via UC Berkeley's library guides
(`guides.lib.berkeley.edu/business-database-finder/financial-markets` and
the WRDS-specific guide) and Haas's research-computing page.

**Bloomberg Terminal (Long Business Library, room S350):** in-person only,
no programmatic/API access from a library terminal booking. Could serve as
a one-off manual cross-check (the kind CLAUDE.md's collector rules already
require — a hardcoded known-value comparison), never as the live daily
source.

**WRDS (IBES Academic), FactSet (Haas remote access), S&P Capital IQ Pro**
— three candidates found on the same Berkeley database-finder page, all
carrying the same two open questions:
1. **Use-case fit.** FactSet's academic terms explicitly restrict use to
   "non-commercial academic research," bar use for "internships and
   external employment," and require deleting downloaded content when the
   project is complete. This project runs indefinitely and stores collected
   data permanently in `data/raw/` (CLAUDE.md's absolute rule 1) — closer to
   a standing personal tool than a bounded research project, and not
   trading-related either, so its fit under an academic license is
   genuinely unclear rather than obviously fine.
2. **Data lag (WRDS/IBES specifically).** Search results describe IBES
   Academic as covering "1992 to the last six months" — suggesting an
   embargo on the most recent ~6 months that would make it useless for a
   *live* daily `rev_4w` (needs the last 4 weeks' consensus change), but
   would not block using it to backfill 3 years of *historical* `rev_4w`
   for the backtest dataset, the way the price/flow collectors were already
   backfilled on 2026-08-06. Not independently confirmed — the email below
   asks directly.

**WRDS access mechanics** (for reference, if pursued): Berkeley students
request an account with a `@berkeley.edu` address at
`wrds-www.wharton.upenn.edu/register/`, ~7-day approval. Contact for
licensing questions on any of the three: `haasref-library@berkeley.edu`
(Haas Business/Economics reference librarians).

## The graduation access-window problem

**Ricky graduates in ~5 months (around 2027-01).** WRDS, FactSet, Capital
IQ Pro, and very likely FnSpace's Academy tier are all scoped to current
student/faculty status — access almost certainly ends at graduation. This
reframes what any of these sources can be used for: **not a foundation for
the ongoing live daily collector**, which would just break again in ~5
months and leave `rev_4w` with a data source that vanishes mid-project — but
potentially a one-time opportunity to backfill 3 years of historical
`rev_4w` into `data/raw/` before access ends, the same kind of one-time
window the project already exploited for the initial 3-year price/flow
backfill. The live-feed problem (how does `rev_4w` get computed *after*
graduation) stays open regardless of what the email below turns up.

## Resolved: Haas library reply, 2026-08-21 — path closed

Sent 2026-08-14 to `haasref-library@berkeley.edu`, asking:
1. Whether WRDS/FactSet/Capital IQ Pro's academic license permits standing,
   indefinite personal use with permanently-stored data (vs. bounded
   research use only).
2. Whether IBES (via WRDS) covers Korean-listed consensus EPS, and what the
   actual embargo period is on recent data.
3. Whether Capital IQ Pro's consensus data covers Korean-listed companies.
4. Whether access ends at graduation, and if so, whether a one-time bulk
   historical download is permitted before then.

**Reply, 2026-08-21 (paraphrased):**
1. Data retention is tied to *ongoing campus affiliation*, not to finishing
   a bounded project — but data may not be retained indefinitely, and must
   be deleted once the license or the user's affiliation ends. Whether a
   personal, non-commercial software project is itself a qualifying use
   under the academic license was not directly addressed.
2. IBES carries a 6-month embargo — confirmed.
3. Ambiguous: "No and Cap IQ does not have the same lag time as IBES."
   Unclear whether "No" answers the Korean-coverage half of the question or
   the lag-restriction half.
4. Access ends at graduation. **Bulk downloading is generally not
   permitted, and storing data for post-graduation use is also not
   permitted.**

**Conclusion: this closes the WRDS/FactSet/Capital IQ Pro path for this
project, independent of how the point-3 ambiguity would resolve.** The
plan this research file flagged as the only path with real value — bulk
backfill 3 years of historical `rev_4w` once, before graduation, and keep
it permanently in `data/raw/` — is barred twice over by point 4: bulk
download itself isn't permitted, and even a trickle collected under the
license couldn't be retained past graduation for a project that stores
everything indefinitely (CLAUDE.md's absolute rule 1). A live-only,
never-stored feed for the ~5 months until graduation is theoretically
still open if Capital IQ turns out to cover Korea, but was not pursued —
the effort-to-payoff ratio for a source that expires on its own in five
months, on a licensing basis that was never confirmed to fit a personal
software project in the first place, was judged not worth it. Ricky closed
out the correspondence with a thank-you rather than chasing the point-3
clarification.

**Resolved, 2026-08-25: `rev_4w` is permanently dropped.** FnSpace Academy
(₩50,000/mo ≈ $36) was the one paid path this research found that isn't
entangled with university affiliation, but it sits above Ricky's stated
cost tolerance and was not taken. `config/rating.yaml`'s `deferred_weights`
no longer lists `rev_4w` at all — deleted, not commented out, since
"deferred" implies it might still arrive and this decision is permanent.
`rate()` never read `deferred_weights` (it's display-only, read by
`render.py`'s header), so the removal changes no computed rating. If a
consensus-EPS vendor ever becomes affordable, FnSpace Academy is still the
concrete lead to revisit — the research above stays valid. `MANUAL-TASKS.md
§11` records the decision.
