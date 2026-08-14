# Calendar collector (§2.2④): plan

Written 2026-08-14, before implementation. Not a SPEC §12 numbered step —
`step10-plan.md` (line 38) already named ④ as "no earnings/FOMC/IPO collector
exists," and `step11-plan.md` confirms it stayed a stated absence through the
report renderer and workflow steps. This is the plan that finally picks it up,
scoped narrower than SPEC's original four sub-sources.

## Scope

SPEC §2.2④ names four sub-sources. Readiness differs sharply per source, and
that difference is the reason this collector builds two of them now and
leaves two explicitly absent rather than either blocking on all four or
faking partial coverage.

**Building now:** US macro release dates (CPI, Employment Situation, FOMC)
and options expiration dates.

**Deferred, named absent with reasons (not silently missing):**
- **US individual-company earnings dates.** Alpaca's corporate-actions API
  covers dividends/splits/mergers only — confirmed against Alpaca's own docs
  (`docs.alpaca.markets/reference/corporateactions-1`), no earnings-calendar
  endpoint exists there. No other free source was found. Lower priority
  anyway: `notes/us-rating-plan.md` already put individual US-ticker
  directional ratings out of scope, so per-company US earnings dates feed
  nothing currently active in the report.
- **KR ex-dividend and IPO schedule.** `pykrx` has no relevant function —
  checked all 90 public functions in the installed package; the only match
  by keyword was `get_index_listing_date`, which is index-level, not
  per-company. Needs a DART OpenAPI docs read that hasn't happened yet.
  DART is one of the sources CLAUDE.md flags as requiring verification
  before writing calls, so this isn't a quick add-on to the current pass.

## Source verification (all live, none assumed)

### CPI and Employment Situation — FRED, confirmed working

`https://api.stlouisfed.org/fred/release/dates`, `release_id=10` (CPI) and
`release_id=50` (Employment Situation). Both return real future dates when
called correctly.

**Trap, documented in FRED's own docs but easy to miss:**
`include_release_dates_with_no_data` defaults to `false`, and the docs state
explicitly this "excludes future release dates." Verified live: CPI with the
flag `false` returned 1 row (the one past date in the query window); with
`true`, 5 rows including 4 future ones. **Must pass
`include_release_dates_with_no_data=true`** or the calendar section is
silently always empty going forward — the collector would "work" in every
test that doesn't check for future dates and be useless in production.

Employment Situation dates line up with "first Friday of the month" for
every date checked (Aug–Dec 2026) — a plausibility check, not a hardcoded
rule (BLS has historically shifted this around holidays in ways not worth
encoding as an invariant).

### FOMC — FRED does NOT work; a different source is required

**Trap not documented anywhere, found only by testing live:** `release_id=101`
("FOMC Press Release") does not return meeting dates. It returns a *daily*
publication series' dates — 153 rows over one test window, essentially one
per calendar day. Using this release_id would silently produce an "FOMC
today" flag firing on every single day, with no error, no missing-data
signal, nothing — exactly the failure mode CLAUDE.md's warning about
substituting pykrx's trailing EPS for `rev_4w`'s consensus EPS describes:
a number that looks like the feature and isn't, which is worse than absence
because `rate()`/a report reader has no way to tell.

**Correct source:** `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm`.
Public-domain US government site; no `robots.txt` restriction on this path
(verified live — the block that exists on `federalreserve.gov` is for
unrelated content, this page isn't disallowed). The page (164KB) covers
2021–2027 in a consistent structure: `fomc-meeting__month` and
`fomc-meeting__date` CSS classes per meeting.

Four parsing hazards, all confirmed present in the live page:

1. **Month-spanning meetings** (`"Apr/May"` + date `"17-18*"`, meaning the
   meeting starts in April and ends in May). The first day belongs to the
   first month named, the second day to the second — must be read off the
   month cell, not inferred from day-number comparisons.
2. **Month-end/month-start rollovers** (`"30-1"`) — same hazard, different
   shape: day "1" is *not* less than day "30" here, it's the next month.
   A day-number heuristic ("if second number < first, roll over") happens
   to get this one right but the *reason* it's right is the month cell
   saying two different months, not the numeric comparison — parse off the
   month cell for both cases, don't rely on arithmetic that coincidentally
   works for one and would silently mis-handle a hypothetical case where it
   doesn't.
3. **Non-meeting notation entries** (`"22 (notation vote)"`) — not a 2-day
   meeting. Detect via the parenthetical and store as a distinctly-labeled
   single-day event, or exclude — don't fold into meeting-date parsing as if
   it were a normal 2-day range.
4. **Trailing `*` — resolved, not a hazard.** The page's own legend states
   verbatim: "`* Meeting associated with a Summary of Economic
   Projections.`" It marks the quarterly SEP/press-conference meetings.
   Confirmed by reading the legend text directly, not guessed from the
   pattern of which meetings have it.

**Dependency decision: stdlib, not `lxml`/`beautifulsoup4`.** Neither is
currently installed (`import lxml` / `import bs4` both fail in `.venv`).
`requests` already fetches the page (already a project dependency, used by
`kr_news.py` and `us_price.py`). Parsing needs only `re` matching on two
CSS-class-delimited fields plus the legend line — simple, well-anchored,
single-purpose extractions, not general HTML tree traversal. The project's
own `pyproject.toml` justifies every dependency by what it uniquely provides
(parquet engine, market-calendar library, etc.); a full HTML-parsing library
for two regex-anchorable fields on one page doesn't clear that bar. Flagged
for Ricky's confirmation before adding anything, per CLAUDE.md's "ask before
adding a dependency" rule — the recommendation is not to add one.

### Options expiration — no vendor, but not naive date math either

Standard monthly expiration is the third Friday of the month, **except when
that Friday is a US market holiday, when it moves to the preceding trading
day** (OCC rule — confirmed it moves backward, never forward to the
following Monday). A bare "3rd Friday" formula would be silently wrong
roughly once a year.

Must be computed against `src.util.session.trading_days("US", ...)`, not
independent holiday logic — this project already has the authoritative US
market calendar and computing expiry against a second, separate holiday
list would be exactly the kind of drift CLAUDE.md's `_CALENDAR_CORRECTIONS`
mechanism exists to prevent elsewhere.

This produces a concrete, dated known-value check for free: **June 2026
expiration falls on Thursday 2026-06-18, not Friday 2026-06-19, because
2026-06-19 is Juneteenth and NYSE is closed.** Verified against a real
public example, then cross-checked directly against this repo's own
`trading_days("US", ...)`:

```
>>> from src.util.session import trading_days
>>> import datetime as dt
>>> trading_days("US", dt.date(2026,6,15), dt.date(2026,6,22))
[2026-06-15, 2026-06-16, 2026-06-17, 2026-06-18, 2026-06-22]
```

`2026-06-18` is present, `2026-06-19` correctly excluded. This is the
strongest kind of known-value check available for this feature — it
exercises the same session-calendar dependency the collector's own expiry
computation uses, the same principle behind `macro.py`'s SPY-price check
being read from an independent source (Yahoo Finance) rather than Tiingo,
its own data vendor.

## Design

This section was drafted, then independently re-designed by a Plan agent
given the same source verification plus its own fresh reading of
`macro.py`/`validate.py`/`render.py`/`collect_daily.py`/`test_macro.py`. The
agent's version is more precise in three places that matter for correctness,
not just style, so it supersedes the first draft below — reasoning kept
where it explains *why*.

### Module: `src/collectors/calendar.py`

Reversed from the first draft's `calendar_events.py`. The stdlib-shadowing
worry was tested empirically rather than assumed either way: a throwaway
`src/collectors/calendar.py` with `import calendar` inside it, run the way
this project actually runs everything (`python -m src.collectors.calendar`
/ `uv run python -m ...` from the repo root, per CLAUDE.md's Toolchain
section and `pythonpath = ["."]` in `pyproject.toml`), resolves `calendar`
to the **stdlib** module correctly — Python's absolute-import model makes
the file's own module identity `src.collectors.calendar`, not bare
`calendar`. The shadowing hazard is real only if someone `cd`s into
`src/collectors/` and runs `python calendar.py` directly, which nothing in
this project's test suite, `collect_daily.py`, or CI ever does. Confirmed
live, both ways. `calendar.py` matches SPEC's own section name and is safe
to use — worth one line in the module docstring recording the check, the
same habit `macro.py` has for its own verified-not-assumed facts. Using the
real stdlib `calendar` module (`calendar.month_name`/`month_abbr` for FOMC
month parsing, `calendar.Calendar().itermonthdates()` for the 3rd-Friday
computation) is preferable to hand-rolling either.

### Schema — `date_start` added; FOMC's two-day span is a real fact, not noise

The first draft's schema had one flaw: a bare `date` column can't honestly
represent a 2-day FOMC meeting (does `date` mean the first day or the
decision day?). The corrected schema keeps `date` as the canonical/decision
date — release day for CPI/employment, the meeting's *final* day for FOMC
(when the statement lands), the expiry Friday for options — and adds
`date_start` for the first day of multi-day events (equal to `date` for
every single-day event type):

```python
SCHEMA = {
    "date": "datetime64[s]",  # canonical/decision date — what
    # "same-day/next-day" comparisons use
    "date_start": "datetime64[s]",  # first day; equals `date` except for FOMC
    "event": "object",  # "cpi" | "employment_situation" | "fomc" |
    # "options_expiration_monthly"
    "label": "object",  # Korean, rendered as-is
    "source": "object",  # provenance — mirrors macro's series_id:
    # "FRED release_id=10" / "...=50" /
    # "federalreserve.gov/monetarypolicy/fomccalendars.htm"
    # / "computed:3rd-friday"
    "known_at_utc": "datetime64[ns, UTC]",
}
```

`MISSING_THRESHOLDS`: every column required (`0.0` each) — there's no
numeric column with a legitimate gap the way macro's `value` has FRED's
`"."` markers.

Optional, evidence-backed, cuttable without touching anything else: a
`has_sep: bool` column, since the FOMC page's own footer (see below) makes
"is this a Summary-of-Economic-Projections meeting" a confirmed fact rather
than noise worth discarding.

### `known_at_utc` — fetch-time, not `next_tradeable_open`; two concrete consequences

Confirms the first draft's conclusion (a CPI/FOMC/options-expiry date is
announced far ahead of the event, the reverse of macro.py's
publication-lags-observation model) and adds two things the first pass
missed:

1. **`validate.check_known_value` assumes a numeric column** — it calls
   `float(matched[column].iloc[0])` internally, which raises or misbehaves
   against a `datetime64` `Timestamp`. This collector's known facts are
   dates, so it needs its own comparator, `check_known_date` (below), rather
   than coercing dates into epoch-floats just to fit the shared function's
   signature.
2. **`fetch()` should not take macro.py's row-selective `as_of` parameter.**
   Every row from one `fetch()` call shares the same `known_at_utc` (the
   instant that call ran), so a per-row `as_of` filter would be all-or-
   nothing, not selective — meaningless here. State this as a deliberate,
   reasoned omission in the docstring, not a silent gap. If a future feature
   needs a look-ahead boundary against this frame, it filters directly on
   `known_at_utc < as_of` and that filter will not exclude anything in this
   pipeline's actual usage pattern, since every row is written by a run that
   happened strictly before the report reading it.

### Four validations

1. `check_schema(df, SCHEMA)` — unchanged pattern.
2. `check_missing_ratio` — kept for consistency, near-trivial here.
3. **`check_event_continuity`, concretely: a maximum-gap check per event
   type**, not just "at least one row of each type." There's no daily
   calendar for event dates to be measured against (the same reasoning that
   justifies macro.py's own `check_coverage` substitution, one layer looser
   since these rows aren't even nominally daily). The honest version checks
   that no gap between consecutive occurrences of the same event exceeds
   that event's own known maximum spacing, plus weekend/Friday sanity:

   ```python
   MAX_GAP_DAYS = {
       "cpi": 45,  # monthly BLS release, slack for holidays
       "employment_situation": 40,  # first-Friday-of-month cadence
       "fomc": 60,  # 8 meetings/year; longest real gap ~7 weeks
       "options_expiration_monthly": 35,  # exactly monthly by construction
   }
   ```
   For each event type: flag if the first row is more than `MAX_GAP_DAYS`
   after the window start, the last row is more than `MAX_GAP_DAYS` before
   the window end, any interior gap between consecutive rows exceeds
   `MAX_GAP_DAYS`, any date is a duplicate, any date falls on a weekend, or
   (for `employment_situation`/`options_expiration_monthly` specifically)
   any date isn't a Friday. A widened gap means the fetch lost rows, not
   that the calendar is legitimately sparse — same spirit as
   `check_trading_day_continuity` catching a silent hole, translated to a
   series that was never daily.
4. `check_known_date` (collector-local, not `validate.check_known_value` —
   see the float-coercion note above) — candidates verified live, not
   proposed blind:
   - **FOMC:** federalreserve.gov's own 2026 panel, September row —
     `date=2026-09-16`, `date_start=2026-09-15`. Read directly off the
     primary-source page during this planning pass, not inferred.
   - **Options expiry:** `2026-06-18` (Thursday) — cross-checked directly
     against this repo's own `trading_days("US", ...)`, per the source
     verification above. The stronger of the two since it exercises the
     collector's own session-calendar dependency.
   - **Still needed before merge:** one CPI or Employment Situation date
     cross-checked against BLS's own published release schedule
     (`bls.gov/schedule/news_release/cpi.htm` / `.../empsit.htm`) — the same
     role `home.treasury.gov` plays for macro.py's `us_10y` check.
     Checking FRED against FRED here would prove nothing, since FRED merely
     redistributes BLS's own schedule; this needs an independent source,
     not a second read of the same feed.

### FOMC HTML parsing — `html.parser.HTMLParser` (stdlib), not raw regex on tags

Refines the first draft's "stdlib, not a new dependency" call with a more
specific and better-justified recommendation. Fetching the live page during
this planning pass confirmed the actual markup varies row to row in ways
that make regex-on-raw-HTML a real hazard here, not a hypothetical one:
some rows carry an extra `fomc-meeting--shaded` class token before
`fomc-meeting__month`/`fomc-meeting__date`, and notation-vote rows
(`"22 (notation vote)"`) sit at different HTML nesting than regular 2-day
rows, while still carrying the same class *substrings*. A class-substring-
matching `HTMLParser` subclass — tracking "am I inside a tag whose `class`
attribute contains `fomc-meeting__month`/`__date`" and capturing the next
`handle_data` call — handles both variations with no regex on tags at all;
regex is then only applied to the already-extracted text content
(`"27-28"`, `"22 (notation vote)"`), which is the narrow, safe use of it.
This is still stdlib (`html.parser`), so the "don't add `lxml`/
`beautifulsoup4` for one page, two fields" conclusion holds — it's a
stronger tool within the same no-new-dependency recommendation, not a
reversal of it. Flagged for Ricky's confirmation before adding anything
external, per CLAUDE.md — the recommendation remains not to add a
dependency.

**Confirmed structural details, from the live page fetched during
planning** (supersedes "check before finalizing" from the first pass):
- Each year sits in its own `panel panel-default` block, headed by
  `<a id="...">YYYY FOMC Meetings</a>` (2026's panel id is `42828`; 2021–2027
  are all present) — parse by tracking "current year" from this heading and
  applying it to subsequent month/date pairs until the next one.
- Each year's panel closes with `<div class="panel-footer">* Meeting
  associated with a Summary of Economic Projections.</div>` — the exact,
  confirmed meaning of the trailing `*`, present verbatim in every year's
  footer, not just once.
- A real month-spanning + rollover example, 2024 panel: month cell
  `Apr/May`, date cell `30-1`, linking to `monetary20240501a1.pdf` —
  confirms April 30 → May 1, 2024. First day belongs to the first month
  token, second day to the second, read off the month cell rather than
  inferred from day-number comparison (day "1" is not less than day "30"
  here in the naive sense — it's the *reason* two months are named that
  matters, not an arithmetic shortcut that happens to also work).
- A real notation-vote example, 2027 panel: month `August`, date
  `22 (notation vote)` — not a 2-day meeting, store as a distinctly-labeled
  single-day event or exclude, detected via the parenthetical.
- No live example of a **cross-year** rollover (e.g. a hypothetical Dec/Jan
  pair) appears in the captured 2021–2027 range, but the parser should
  still handle it explicitly (`date_start`'s year = current year, `date`'s
  year = current year + 1) and get one test case even without a real
  example to draw from.

### Wiring

`scripts/collect_daily.py` — window split into lookback/lookahead rather
than one `CALENDAR_WINDOW_DAYS`, since this collector isn't compensating for
a slow publisher (unlike `MACRO_WINDOW_DAYS`'s reason for being 30 against
the generic 8) — it's sizing how far forward the ④ section needs to see:

```python
CALENDAR_LOOKBACK_DAYS = 30  # short trailing history, for check_event_continuity
CALENDAR_LOOKAHEAD_DAYS = 120  # a full quarter of scheduled releases/meetings

PATHS["calendar"] = RAW / "calendar"
KEYS["calendar"] = ["event", "date"]
```
`collect_calendar(start, end)` mirrors `collect_macro`'s shape (`del start`;
the collector sizes its own forward/backward window rather than the
driver's), calls `calendar.fetch(...)`, `write_daily("calendar", df)`,
returns `(detail, report)`. Registered in **both** `RUNS["morning"]` and
`RUNS["evening"]`, same as `macro` — no KRX dependency, nothing to keep out
of the KRX-free morning run.

One storage consequence worth stating rather than discovering later:
`write_daily` groups rows by the date *inside* the row (`write_daily` groups
on `pd.to_datetime(df["date"]).dt.date`), so a September FOMC row fetched in
August is written under `data/raw/calendar/2026-09-16.parquet` — named for
the future event date, not the collection date. Re-running daily converges
to a no-op once the schedule is stable, and only produces a legitimate
`-v2` if the Fed actually reschedules a meeting — rule 1's intended
behavior, not a defect.

`src/report/render.py`:
- Remove `"④"` from `ABSENT_SECTIONS` entirely — this matches how ②/③ are
  already handled (neither is in `ABSENT_SECTIONS`; each names its own
  remaining gap inline, at the end of its own render function). Moving ④
  the same way is the precedented move, not a new pattern.
- Add `calendar: pd.DataFrame = field(default_factory=pd.DataFrame)` to
  `ReportInputs`, next to the existing `macro` field.
- New `render_calendar(inputs) -> str`: real CPI/employment/FOMC/
  options-expiry rows for today/tomorrow, **plus an inline blockquote naming
  US earnings and KR dividend/IPO as still absent**, with reasons — the
  same shape ②'s and ③'s trailing gap-notices already use.
- Replace the `_absent("④")` call in `render()`'s assembly list with
  `render_calendar(inputs)`, same position in the SPEC §2.3 order.
- `load_inputs()`: add `calendar = read("calendar", key=("date", "event"))`
  alongside the existing `macro = read(...)` line, pass `calendar=calendar`
  into the returned `ReportInputs`.
- The header's "미구현 섹션" line, driven by the same `ABSENT_SECTIONS`
  dict, drops ④ automatically once removed — no separate header edit.

### Tests (`tests/test_calendar.py`)

- Fixtures: `tests/fixtures/calendar_fred_release_dates.json` (a captured
  live response for `release_id=10` and `release_id=50`, with
  `include_release_dates_with_no_data=true` — **the exact JSON field name
  for the date list was never actually observed against a real
  `FRED_API_KEY`-authenticated call in this planning pass**, only its
  filtering *behavior*; read the real response shape before finalizing the
  parser, the same "verify before coding" discipline CLAUDE.md applies to
  FRED/pykrx/DART/KIS) and `tests/fixtures/fomc_calendar_2026.html` (the
  full captured page, committed as-is, matching how `macro_fred_2024.json`
  is a full year rather than a hand-trimmed excerpt).
- Fixture-sanity tests guarding the fixture itself against silent drift:
  still has a notation-vote entry, still has a month-spanning meeting, still
  has the SEP footnote — mirroring `test_macro.py`'s two-tier structure
  (fixture sanity, then behavior).
- One behavior test per parsing hazard: month-span resolves start/end to the
  right months, a notation-vote row is excluded from `event="fomc"` output
  **and** surfaces in a `CheckResult` rather than silently vanishing
  (mirroring how macro.py's `failures` list surfaces partial trouble, not
  a full-stop error).
- `test_dtypes_match_the_declared_schema`, `test_a_clean_frame_passes_every_check`
  / a tampered-value test — direct analogs of `test_macro.py`'s pair.
- `test_options_expiration_hits_the_correct_friday_for_a_known_month` — pure
  computed fact (no external source needed), e.g. September 2026 → 2026-09-18,
  December 2026 → 2026-12-18.
- Check-3 tests: a real window passes, an interior FOMC gap fails, an
  event with zero rows is named (not silently OK), a weekend row is caught,
  an off-Friday `employment_situation`/`options_expiration_monthly` row is
  caught.
- Config-consistency sanity tests, same family as `test_macro.py`'s "every
  declared series has a FRED id": `test_every_declared_fred_release_has_an_id`,
  `test_max_gap_days_covers_every_declared_event`
  (`set(EVENTS) <= set(MAX_GAP_DAYS)`), `test_the_known_values_are_wired_to_events_we_actually_fetch`.
- `tests/test_collect_daily.py` gets `assert "calendar" in RUNS["morning"]`
  and `RUNS["evening"]`, matching the existing membership-assertion pattern.
- `@pytest.mark.network` live tests, excluded from the default run: one
  hitting FRED, one confirming the live FOMC page still uses the expected
  CSS classes (catches federalreserve.gov changing its markup under the
  fixture).

## Documented deviations from CLAUDE.md's four-check collector rule

Stated here up front, the way `macro.py`'s docstring documents its own
`check_coverage` substitution, rather than left implicit in the code:

1. **Check 3 doesn't apply as written** — no daily exchange calendar an
   event-date row can be measured against. Replaced by
   `check_event_continuity`, a maximum-inter-occurrence-gap check per event
   type.
2. **Check 4's shared helper assumes a numeric column** —
   `validate.check_known_value` does `float(...)` internally, which breaks
   on a date. This collector defines its own `check_known_date` rather than
   coercing dates into epoch-floats to fit the existing signature.
3. **`known_at_utc` does not use `next_tradeable_open`** — the
   publication-lags-observation model that helper encodes is inverted here
   (announcement precedes event, not follows it); `fetch()` uses its own
   fetch-time stamp and deliberately takes no `as_of` parameter, since one
   shared `known_at_utc` per call makes a per-row filter meaningless.
4. **No `config/watchlist.yaml` / `WatchlistEntry` dependency** — confirmed
   correct, not merely assumed: macro events and computed options expiry are
   watchlist-independent, exactly like `macro.py`'s six FRED series, which
   also never import `load_watchlist`.
5. **No `main()`/CLI entry point** — `kr_news.py` is the only collector with
   one, because a separate hourly Actions workflow calls it directly outside
   `collect_daily.py`'s morning/evening cadence. This collector has no such
   standalone cadence requirement, so it matches `macro.py` and skips it.

All five are deviations from the *letter* of the existing pattern in
service of the same look-ahead-prohibition and correctness *intent*, not
exceptions to it.
