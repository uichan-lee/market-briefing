# Filings collectors (`filing` scan flag, SPEC §2.2②): plan and record

Written 2026-08-25, alongside implementation — this is a record of the design
decisions made and the live findings that shaped them, not a plan written
before any code existed. `notes/calendar-collector-plan.md` left the DART
DS001 gap (the actual filing-list endpoint) unread; this closes it, and
builds `src/collectors/us_filings.py` / `src/collectors/kr_filings.py`.

## Status: built and fixture/live-validated; production re-verification and backfill remain

Ran live 2026-08-25 against the full watchlist (31 KR, 40 US tickers, 8-day
window): `kr_filings` — 122 rows, 3/3 checks pass. `us_filings` — 1550 rows,
3/3 checks pass, after two rounds of fixing a plausibility check that failed
on real, correct data (see below). The `filing` flag renders correctly —
verified against a live `render_scan()` call the same day, 8 of 31 KR
tickers flagged.

**Update 2026-08-29.** The pending local change maps the already-registered
`DART_API_KEY` / `SEC_USER_AGENT` secrets into `report.yml`. It also handles a
date-only filing on a closed market day at the next tradeable open, applies the
same fallback when SEC `acceptanceDateTime` is absent, and fixes the shared
NaT comparison defect. Fixture tests cover the KRX and US holiday cases.
Production recovery and a deliberate backfill still require merge + CI
verification; no live success is inferred from the local tests.

## The four decisions this plan fixes, and why

**1. Storage: parquet, not the `.jsonl` SPEC §3.3 names.** Every collector
integration point (`write_daily`/`backfill.py`'s versioning, `load_raw`) is
parquet-only. A second JSONL writer for one collector would duplicate that
machinery for no benefit. SPEC §3.3 also never named a `kr/filings/` path at
all — this collector supplies it. SPEC §3.3 should be corrected to read
`.parquet` and add the missing `kr/filings/` line the next time it is
touched, the same way `notes/calendar-collector-plan.md` corrected §2.2④'s
wording rather than leaving code and spec silently disagreeing.

**2. Identifier mapping: `config/filing_ids.yaml`, machine-generated.**
Neither `watchlist.yaml` (Ricky's hand-maintained judgment file) nor
`aliases.yaml`'s auto-generation prohibition apply — SEC's
`company_tickers.json` and DART's `corpCode.xml` are official, exact-match,
complete enumerations, not fuzzy calls a human has to make. `scripts/
resolve_filing_ids.py` regenerates it; `src.util.config.load_filing_ids`
rejects a watchlist ticker with no entry at load time (loud, not silent —
the CLAUDE.md-forbidden failure mode here is a `filing` flag that dies
quietly for one ticker forever). Run 2026-08-25: all 71 watchlist tickers
resolved on the first try, no manual intervention needed. DART's
`corpCode.xml` download is slow — observed ~10-15 KB/s for a ~30MB response,
several minutes end to end on every run, including two retries before one
completed inside a 500s `curl --max-time`. `resolve_filing_ids.py`'s HTTP
timeout is sized generously for this rather than tuned to a fast-path
assumption.

**3. Filing-type scope: unfiltered in v1, and the live data shows the real
cost of that.** SPEC states `filing` as a bare presence flag with no type
filter, and it carries no `rating.yaml` weight — a display flag like
`volatility_z`, not a rating input. A live DART pull for Samsung Electronics
(corp_code `00126380`, 2026-07-01 to 08-25) returned **832 filings in 55
days** — mostly routine 임원·주요주주 ownership reports and related-party
transaction disclosures. Unfiltered, `filing` will fire on most sessions for
a high-filing-frequency name. This is stated in both collectors' module
docstrings and in the rendered footnote itself, rather than left for a
reader to discover. A v2 type filter is real follow-up work, not attempted
here — SPEC named no taxonomy to filter by, and picking one now would be
guessing.

**4. The continuity check: `check_filing_plausibility`, not
`check_trading_day_continuity`.** A filing is not one-row-per-session — a
company files zero or several times a day — so the standard check does not
fit. Zero filings for a company in a short window is the *normal* case, the
same "a quiet run is not a failure" principle CLAUDE.md states for
`kr_news`'s zero-row files, applied here for the first time to a non-news
source. `check_filing_plausibility` checks date-in-range, a sane distance
between `known_at_utc` and `date`, unique filing ids, and requested-CIK
membership — never non-emptiness. `check_missing_ratio`/`check_schema`
still fail on an empty frame by default (right for every other collector),
so both `validate_frame`s override `missing_ratio` to a synthesized pass on
zero rows, the same override `kr_news.validate_frame` already applies to its
own zero-row case. A new shared `validate.empty_frame(schema)` helper
produces a zero-row frame that still satisfies `check_schema`, since
`pd.DataFrame(columns=[...])` alone leaves every column `object` dtype.

## Live verification, dated

**SEC EDGAR submissions API** (2026-08-25): only a ticker-lookup call had
ever been verified live (2026-08-04, MANUAL-TASKS.md §0) — the filing-list
endpoint itself had not. `GET https://data.sec.gov/submissions/CIK{cik}.json`
returns `filings.recent` as **columnar** parallel arrays (not row objects),
capped at ~1000 entries, older history in paginated `filings.files` this
collector does not read (see the limitation note below). Confirmed against
Apple: `acceptanceDateTime` is a real intraday UTC timestamp, 0% empty in a
1001-row sample — used directly as `known_at_utc`, materially better than a
derived next-session-open fallback. `reportDate` is legitimately blank for
filings with no reporting period; 23% empty for Apple alone but **93.3%
empty measured across the full 40-ticker watchlist** — the single-company
number was not representative, and `MISSING_THRESHOLDS["report_date"]` is
calibrated on the real watchlist measurement (0.97), not the sample that
first motivated the column.

**A `KNOWN_VALUE` was pinned and cross-checked against sources other than
the API under test**: Apple's FY2025 10-K, filed 2025-10-31, accession
`0000320193-25-000079` — confirmed via last10k.com, fintel.io and
TradingView, not by re-querying EDGAR.

**The plausibility check went through two wrong versions before the third
survived contact with the full watchlist — recorded so the same mistake
isn't repeated:**

1. First version required `known_at_utc > date` strictly. Failed on 75 of
   1550 real rows — all JPMorgan 424B2/FWP filings. Cause: SEC assigns a
   filing's regulatory `date` as the next business day for anything accepted
   after its 5:30pm ET cutoff, so a real `acceptanceDateTime` legitimately
   precedes midnight UTC of `date`. `known_at_utc` was correct in every one
   of these rows — it already carried the real acceptance timestamp, which
   is the number that matters for the look-ahead boundary. The check's
   assumption was wrong, not the data.
2. Second version bounded the gap at `date - 1 day`. Failed on 14 more rows,
   all filings accepted Friday evening whose next business day was the
   following Monday (JPM, BAC, GS, MS) — the 1-day bound didn't account for
   weekends.
3. Third version bounds `known_at_utc` to `[date - 5d, date + 5d]` — wide
   enough to tolerate an ordinary weekend or short holiday run without
   re-implementing SEC's own business-day calendar, while still catching a
   genuinely wrong derivation (a value a year off, say). Passed 3/3 on the
   full watchlist after this fix.

**DART DS001 (공시정보)** (2026-08-25): the gap `notes/calendar-collector-
plan.md` (2026-08-14) named — that plan read DS002/DS006 for the calendar
collector's different purpose but never DS001. Confirmed: 공시검색
(`apiId=2019001`), `GET https://opendart.fss.or.kr/api/list.json`, one
`corp_code` per call (no batch parameter — confirms the per-company loop
shape, matching `kr_flow.py`'s own pattern rather than `macro.py`'s
per-series one), `page_count` capped at 100 (this collector paginates via
`total_page`). Response fields confirmed against a live Samsung Electronics
call: `rcept_no`, `corp_cls`, `corp_name`, `corp_code`, `stock_code`,
`report_nm`, `rcept_dt` (date-only — no time component, unlike SEC's
`acceptanceDateTime`), `flr_nm`, `rm`. `known_at_utc` therefore uses the same
conservative `session_close_utc` derivation `kr_flow.py` uses for its own
date-only data.

**Rate limit confirmed from DART's own status-code table**, not a
third-party estimate: `status="020"` is literally defined as the
over-the-limit response (20,000 calls/day), and `status="013"` ("조회된
데이타가 없습니다") is a normal empty result, not an error — `fetch()`
treats it as such rather than raising.

**`KNOWN_VALUE`**: Samsung Electronics' 2026 half-year report,
`rcept_no=20260814003699`, filed exactly on its statutory deadline (period
end 2026-06-30 + 45 days = 2026-08-14) — cross-checked against that deadline
calculation, not against DART itself.

## Known, stated limitations (not fixed here)

- **`us_filings` backfill is not built.** `fetch()` reads only
  `filings.recent`; a multi-year backfill against a high-volume filer would
  silently stop short of full history without `filings.files` pagination.
  `scripts/backfill.py`'s `SOURCES` deliberately excludes `us_filings`, with
  a comment naming why, rather than shipping a backfill that quietly
  under-collects. `kr_filings` has no equivalent gap — `fetch()` paginates
  fully via `total_page`, so `backfill_kr_filings` is wired in.
- **`kr_filings` backfill has a known inefficiency, not a correctness bug.**
  A session where the whole watchlist filed nothing writes no file, so
  `_pending()` treats that date as still missing and re-requests it on every
  re-run. `kr_news` solves the analogous problem by writing an empty file as
  a record that the run happened; doing the same here would mean changing
  `_write_by_date`/`_pending()` for every backfill source, not just this
  one, so it is left as a stated cost.
- **The `filing` flag only covers KR tickers.** `render_scan` iterates
  `inputs.watchlist`, which `load_inputs` populates KR-only (`notes/
  us-rating-plan.md` put per-company US ratings out of scope). `us_filings`
  is still collected and validated daily — the same "collect before
  anything reads it" reasoning `kr_index.py` gives for the shadow-portfolio
  benchmark — and sits on `ReportInputs.us_filings`, unconsumed, for
  whenever a US-side use appears.
