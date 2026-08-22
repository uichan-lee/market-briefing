"""The 2-week PREREGISTRATION §8.5 gate read (criteria 1–3), automated where
the read source actually supports it.

This module measures; it does not decide — same stance `src.eval.ic` and
`src.eval.rating_calibration` take. It renders no pass/fail verdict for the
gate as a whole; §8.5 says what happens on a miss, and that response is not
this module's to choose.

**Reads `reports/` directly — never `data/status/`.** §8.5 states this
explicitly for criterion 2 ("From the committed report headers in
`reports/`... Not from `data/status/`"), and the reason generalizes: `reports/`
is git-committed and range-queryable over the whole window, while
`data/status/` is gitignored, holds only the single newest run's JSON
(`src.report.render.read_status`), and is explicitly ruled out by the doc for
exactly that reason.

**Session dates come from `src.util.session.trading_days`, never a filename
glob filtered by date.** This is not a style preference — it is required for
a correct count. `reports/2026-08-17-evening.md` is a real committed report
with three genuine `schema` failures (`kr_price/schema`, `kr_flow/schema`,
`kr_index/schema`), and 2026-08-17 is a substitute holiday for 광복절,
excluded from the window's 10 KRX sessions. A naive glob over
`reports/*.md` filtered by filename date would count those three failures
toward criterion 2 on a day PREREGISTRATION's own record calibrates to zero.

**Criterion 2 cannot be fully automated as specified, and this module says so
rather than guessing.** `structural_invariants` and `coverage` are each
*mixed* checks under §8.5 — PREREGISTRATION counts only specific
sub-conditions of each (named in the `detail` field), and everything else
under those names is availability, excluded. `src.report.render.read_status`
collapses a failure down to `{collector}/{check}` before it ever reaches a
report header, discarding `detail` — so the stated read source structurally
cannot tell which sub-condition fired. This module classifies every
`structural_invariants`/`coverage` occurrence as ``ambiguous`` and lists it
for Ricky to resolve by hand (against the Actions log or `data/status/` on
the day it happened), rather than assuming either direction. As of this
window this is not theoretical: `reports/2026-08-19-evening.md` has
`⚠ 수집 실패: macro/coverage, macro/fetch` in-window.

**Criterion 1 is reported as descriptive context, not proof.** 1(a)'s actual
guarantee ("every run that fires records a run file") is an invariant of two
call sites, pinned by
`tests/test_kr_news.py::test_a_run_with_nothing_new_still_records_that_it_ran`
and `tests/test_collect_daily.py::test_a_quiet_driver_run_still_records_that_it_ran`
— not something inspectable after the fact, since a run that fired and failed
to write leaves no trace to detect. 1(b)'s full record is the window's Actions
run conclusions, which live outside this repo at 90-day retention;
`reports/` only catches whatever `data/status/` happened to hold newest at
the next scheduled render, so a `⚠ 뉴스 유실` count here is a strict
undercount of actual `check_feed_continuity` evaluations, not a complete one.

**Criterion 3's "window average of the daily ratio" is genuinely ambiguous.**
Real reports differ enough between runs (848 to 1,311 articles across the
window) that a simple mean, an article-count-weighted mean, and an
evening-only per-session mean are not close. Rather than pick one silently,
:func:`criterion_3` reports all three.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.util.session import trading_days

ROOT = Path(__file__).resolve().parents[2]

# PREREGISTRATION §8.5, "The 2-week clock". Pinned, not derived, for the same
# reason src.eval.ic pins its 3-month window: the window cannot be chosen
# once the record inside it is known.
WINDOW_START = dt.date(2026, 8, 12)
WINDOW_END = dt.date(2026, 8, 26)

AMBIGUOUS_THRESHOLD = 0.30

# §8.5's explicit allowlist for "zero data-consistency errors". Every
# occurrence of these two counts unconditionally.
CONFIRMED_CHECKS = frozenset({"schema", "flow_identity"})

# §8.5 counts only specific sub-conditions of these two (named in `detail`,
# which report headers never carry — see the module docstring). Every
# occurrence is flagged for manual resolution rather than counted either way.
AMBIGUOUS_CHECKS = frozenset({"structural_invariants", "coverage"})

_REPORT_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<run>morning|evening))?\.md$")
_AMBIGUOUS_LINE = re.compile(
    r"^ℹ 엔티티 모호 비율: (?P<pct>[\d.]+)% \(기사 (?P<articles>[\d,]+)건\)", re.MULTILINE
)
_FAILURE_LINE = re.compile(r"^⚠ 수집 실패: (?P<items>.+)$", re.MULTILINE)
_VENDOR_LINE = re.compile(r"^⚠ 미국 시세 벤더 불일치: (?P<detail>.+)$", re.MULTILINE)
_NEWS_GAP_LINE = re.compile(r"^⚠ 뉴스 유실: (?P<detail>.+)$", re.MULTILINE)

# Anchored full-match on purpose. `render.py::load_inputs`'s own failure
# shape — `"kr/investor_flow (KeyError)"`, `"us/price_preview (비어 있음)"` —
# lands in the *same* comma-joined `⚠ 수집 실패` line as
# `read_status`'s `{collector}/{check}` items, and its source name also
# contains a slash. A partial match (e.g. splitting on the first `/`) would
# misparse it as collector="kr", check="investor_flow ..."; a piece that does
# not fully match this pattern is routed to ``unparsed`` instead.
_CHECK_ITEM = re.compile(r"^(?P<collector>[a-z_]+)/(?P<check>[a-zA-Z_]+)(?:×(?P<count>\d+))?$")


def _classify(check: str | None) -> str:
    if check is None:
        return "unparsed"
    if check in CONFIRMED_CHECKS:
        return "confirmed"
    if check in AMBIGUOUS_CHECKS:
        return "ambiguous"
    return "excluded"


@dataclass(frozen=True)
class FailureItem:
    """One comma-separated piece of a `⚠ 수집 실패` line."""

    raw: str
    collector: str | None
    check: str | None
    count: int
    classification: str  # "confirmed" | "ambiguous" | "excluded" | "unparsed"


def _parse_failure_items(items_text: str) -> tuple[FailureItem, ...]:
    items = []
    for piece in items_text.split(", "):
        match = _CHECK_ITEM.match(piece)
        if match is None:
            items.append(
                FailureItem(
                    raw=piece, collector=None, check=None, count=1, classification="unparsed"
                )
            )
            continue
        collector, check = match.group("collector"), match.group("check")
        count = int(match.group("count") or 1)
        items.append(
            FailureItem(
                raw=piece,
                collector=collector,
                check=check,
                count=count,
                classification=_classify(check),
            )
        )
    return tuple(items)


@dataclass(frozen=True)
class ReportRecord:
    """One parsed `reports/` file."""

    date: dt.date
    run: str  # "morning" | "evening" | "single"
    path: Path
    ambiguous_pct: float | None  # 0-1, not 0-100
    article_count: int | None
    failures: tuple[FailureItem, ...]
    vendor_disagreement: bool
    news_gap: str | None


def parse_report(path: Path) -> ReportRecord | None:
    """One `reports/` file → its gate-relevant facts, or ``None`` if the
    filename doesn't match the committed naming convention."""
    name_match = _REPORT_NAME.match(path.name)
    if name_match is None:
        return None
    date = dt.date.fromisoformat(name_match.group("date"))
    run = name_match.group("run") or "single"
    text = path.read_text(encoding="utf-8")

    ambiguous_match = _AMBIGUOUS_LINE.search(text)
    ambiguous_pct = float(ambiguous_match.group("pct")) / 100 if ambiguous_match else None
    article_count = (
        int(ambiguous_match.group("articles").replace(",", "")) if ambiguous_match else None
    )

    failures: list[FailureItem] = []
    for line_match in _FAILURE_LINE.finditer(text):
        failures.extend(_parse_failure_items(line_match.group("items")))

    vendor_disagreement = _VENDOR_LINE.search(text) is not None
    gap_match = _NEWS_GAP_LINE.search(text)
    news_gap = gap_match.group("detail") if gap_match else None

    return ReportRecord(
        date=date,
        run=run,
        path=path,
        ambiguous_pct=ambiguous_pct,
        article_count=article_count,
        failures=tuple(failures),
        vendor_disagreement=vendor_disagreement,
        news_gap=news_gap,
    )


def _reports_for_session(reports_dir: Path, day: dt.date) -> list[ReportRecord]:
    """Both runs for ``day`` if they exist, else the older single-file name."""
    found = []
    for run in ("morning", "evening"):
        path = reports_dir / f"{day.isoformat()}-{run}.md"
        if path.exists():
            record = parse_report(path)
            if record is not None:
                found.append(record)
    if not found:
        single = reports_dir / f"{day.isoformat()}.md"
        if single.exists():
            record = parse_report(single)
            if record is not None:
                found.append(record)
    return found


@dataclass(frozen=True)
class GateWindow:
    sessions: tuple[dt.date, ...]
    reports: tuple[ReportRecord, ...]
    run_file_counts: Mapping[dt.date, int]  # per session date, data/raw/kr/news/ file count


def load(root: Path = ROOT, *, sessions: Sequence[dt.date] | None = None) -> GateWindow:
    """Every in-window report, parsed, plus a per-day news-run-file count.

    ``sessions`` defaults to ``trading_days("KR", WINDOW_START, WINDOW_END)`` —
    pass it explicitly only to test against a different window.
    """
    resolved_sessions = (
        tuple(sessions)
        if sessions is not None
        else tuple(trading_days("KR", WINDOW_START, WINDOW_END))
    )

    reports_dir = root / "reports"
    records: list[ReportRecord] = []
    for day in resolved_sessions:
        records.extend(_reports_for_session(reports_dir, day))

    news_dir = root / "data" / "raw" / "kr" / "news"
    run_counts = {}
    for day in resolved_sessions:
        day_dir = news_dir / day.isoformat()
        run_counts[day] = len(list(day_dir.glob("*.jsonl.gz"))) if day_dir.exists() else 0

    return GateWindow(
        sessions=resolved_sessions, reports=tuple(records), run_file_counts=run_counts
    )


def criterion_1(window: GateWindow) -> dict[str, object]:
    """Descriptive context for "runs without interruption" — not proof; see
    the module docstring for why 1(a)/1(b) cannot be fully checked from
    stored artifacts."""
    zero_run_days = [day for day, n in window.run_file_counts.items() if n == 0]
    news_gaps = [(r.date, r.run, r.news_gap) for r in window.reports if r.news_gap]
    return {
        "run_file_counts": dict(window.run_file_counts),
        "zero_run_days": zero_run_days,
        "news_gaps": news_gaps,
    }


def criterion_2(window: GateWindow) -> dict[str, object]:
    """Data-consistency failures, classified. ``ambiguous`` needs Ricky's eyes."""
    confirmed = [
        (r.date, r.run, f)
        for r in window.reports
        for f in r.failures
        if f.classification == "confirmed"
    ]
    ambiguous = [
        (r.date, r.run, f)
        for r in window.reports
        for f in r.failures
        if f.classification == "ambiguous"
    ]
    unparsed = [
        (r.date, r.run, f)
        for r in window.reports
        for f in r.failures
        if f.classification == "unparsed"
    ]
    vendor = [(r.date, r.run) for r in window.reports if r.vendor_disagreement]
    return {"confirmed": confirmed, "ambiguous": ambiguous, "unparsed": unparsed, "vendor": vendor}


def criterion_3(window: GateWindow) -> dict[str, float | int | None]:
    """Three aggregation variants of the ambiguous ratio — see the module
    docstring for why "window average of the daily ratio" doesn't pick one."""
    with_ratio = [r for r in window.reports if r.ambiguous_pct is not None]
    if not with_ratio:
        return {"simple_mean": None, "weighted_mean": None, "evening_mean": None, "n": 0}

    simple_mean = sum(r.ambiguous_pct for r in with_ratio) / len(with_ratio)

    total_articles = sum(r.article_count or 0 for r in with_ratio)
    weighted_mean = (
        sum(r.ambiguous_pct * (r.article_count or 0) for r in with_ratio) / total_articles
        if total_articles
        else None
    )

    evenings = [r for r in with_ratio if r.run in ("evening", "single")]
    evening_mean = sum(r.ambiguous_pct for r in evenings) / len(evenings) if evenings else None

    return {
        "simple_mean": simple_mean,
        "weighted_mean": weighted_mean,
        "evening_mean": evening_mean,
        "n": len(with_ratio),
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def report(window: GateWindow) -> str:
    """The three-criterion read. States facts and sample sizes; renders no
    gate-wide verdict — §8.5 decides what a miss means, not this function."""
    reported_sessions = sorted({r.date for r in window.reports})
    missing_sessions = [d for d in window.sessions if d not in reported_sessions]

    lines = [
        "# 2-week gate read — PREREGISTRATION §8.5",
        "",
        f"Window **{WINDOW_START} → {WINDOW_END}**, {len(window.sessions)} KRX session(s): "
        f"{', '.join(d.isoformat() for d in window.sessions)}.",
        f"Reports found for {len(reported_sessions)}/{len(window.sessions)} session(s)"
        + (
            f"; missing: {', '.join(d.isoformat() for d in missing_sessions)}"
            if missing_sessions
            else ""
        )
        + ".",
        "",
        "> This table does not decide. It reports what `reports/` and "
        "`data/raw/kr/news/` show; §8.5 fixes what a miss means, and criterion "
        "4 (inter-model correlation) is tracked separately (already measured).",
        "",
    ]

    # --- criterion 3 ---------------------------------------------------
    c3 = criterion_3(window)
    lines += [
        "## Criterion 3 — `ambiguous` ratio < 30%",
        "",
        f"Over {c3['n']} report(s) with a stated ratio.",
        "",
        "| aggregation | value |",
        "|---|---:|",
        f"| simple mean | {_pct(c3['simple_mean'])} |",
        f"| article-count-weighted mean | {_pct(c3['weighted_mean'])} |",
        f"| evening-only mean | {_pct(c3['evening_mean'])} |",
        f"| threshold | {AMBIGUOUS_THRESHOLD:.0%} |",
        "",
    ]

    # --- criterion 2 ---------------------------------------------------
    c2 = criterion_2(window)
    lines += [
        "## Criterion 2 — zero data-consistency errors",
        "",
        f"`confirmed` (always counts): **{len(c2['confirmed'])}**. "
        f"`ambiguous` (needs manual resolution): **{len(c2['ambiguous'])}**. "
        f"vendor disagreement: **{len(c2['vendor'])}**. "
        f"unparsed items: {len(c2['unparsed'])}.",
        "",
    ]
    if c2["confirmed"]:
        lines += ["**Confirmed:**", ""]
        for date, run, item in c2["confirmed"]:
            lines.append(f"- {date} ({run}): `{item.raw}`")
        lines.append("")
    if c2["ambiguous"]:
        lines += [
            "**Ambiguous — cannot be classified from `reports/` alone "
            "(the underlying sub-condition isn't in the header text):**",
            "",
        ]
        for date, run, item in c2["ambiguous"]:
            lines.append(f"- {date} ({run}): `{item.raw}`")
        lines.append("")
    if c2["unparsed"]:
        lines += ["**Unparsed (did not match the expected `collector/check` shape):**", ""]
        for date, run, item in c2["unparsed"]:
            lines.append(f"- {date} ({run}): `{item.raw}`")
        lines.append("")

    # --- criterion 1 ---------------------------------------------------
    c1 = criterion_1(window)
    lines += [
        "## Criterion 1 — runs without interruption (descriptive, not proof)",
        "",
        "`data/raw/kr/news/` run-file counts, per session date:",
        "",
        "| date | run files |",
        "|---|---:|",
    ]
    for day in window.sessions:
        lines.append(f"| {day} | {c1['run_file_counts'].get(day, 0)} |")
    lines.append("")
    if c1["zero_run_days"]:
        lines.append(
            "⚠ **Zero run files on:** " + ", ".join(d.isoformat() for d in c1["zero_run_days"])
        )
        lines.append("")
    if c1["news_gaps"]:
        lines += [
            "`⚠ 뉴스 유실` occurrences in `reports/` (a strict undercount — see module docstring):",
            "",
        ]
        for date, run, detail in c1["news_gaps"]:
            lines.append(f"- {date} ({run}): {detail}")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PREREGISTRATION §8.5 2-week gate read")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report", help="print the 3-criterion read")

    args = parser.parse_args(argv)
    if args.command == "report":
        print(report(load()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
