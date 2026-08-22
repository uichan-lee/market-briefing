"""Tests for the PREREGISTRATION §8.5 2-week gate-read tool.

Offline, against hand-written report markdown fixtures under `tmp_path` —
never the real `reports/` directory, so these tests describe the parser's
contract rather than today's data. The one test that reaches for the real
calendar (`trading_days`) is the 2026-08-17 exclusion regression, because
that trap is specifically about the real KRX holiday calendar disagreeing
with a naive date-range glob.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.eval.gate_2week import (
    GateWindow,
    ReportRecord,
    criterion_1,
    criterion_2,
    criterion_3,
    load,
    parse_report,
    report,
)
from src.util.session import trading_days


def write_report(reports_dir, name: str, body: str) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / name).write_text(body, encoding="utf-8")


# --- parse_report: the header grammar --------------------------------------


def test_parse_report_extracts_ambiguous_ratio_and_article_count_with_a_comma(tmp_path):
    write_report(tmp_path, "2026-08-13-evening.md", "ℹ 엔티티 모호 비율: 9.2% (기사 1,190건)\n")
    record = parse_report(tmp_path / "2026-08-13-evening.md")
    assert record.ambiguous_pct == pytest.approx(0.092)
    assert record.article_count == 1190
    assert record.date == dt.date(2026, 8, 13)
    assert record.run == "evening"


def test_parse_report_classifies_confirmed_ambiguous_and_excluded_checks(tmp_path):
    write_report(
        tmp_path,
        "2026-08-13-morning.md",
        "⚠ 수집 실패: kr_flow/schema, kr_news/structural_invariants, kr_flow/missing_ratio\n",
    )
    record = parse_report(tmp_path / "2026-08-13-morning.md")
    classes = {f.check: f.classification for f in record.failures}
    assert classes == {
        "schema": "confirmed",
        "structural_invariants": "ambiguous",
        "missing_ratio": "excluded",
    }


def test_parse_report_captures_the_multiplier_but_still_excludes_the_check(tmp_path):
    write_report(
        tmp_path, "2026-08-13-morning.md", "⚠ 수집 실패: kr_price/trading_day_continuity×31\n"
    )
    record = parse_report(tmp_path / "2026-08-13-morning.md")
    item = record.failures[0]
    assert item.count == 31
    assert item.classification == "excluded"


def test_parse_report_routes_the_load_inputs_failure_shape_to_unparsed(tmp_path):
    """`render.py::load_inputs`'s own guard injects `"{source} ({Exception})"`
    into the *same* comma-joined line as `read_status`'s `collector/check`
    items — and the source name itself contains a slash
    (`"kr/investor_flow"`), which a naive split would misparse as a check."""
    write_report(
        tmp_path,
        "2026-08-13-morning.md",
        "⚠ 수집 실패: kr_flow/schema, kr/investor_flow (KeyError), us/price_preview (비어 있음)\n",
    )
    record = parse_report(tmp_path / "2026-08-13-morning.md")
    classes = [f.classification for f in record.failures]
    assert classes == ["confirmed", "unparsed", "unparsed"]
    assert record.failures[1].check is None
    assert record.failures[1].raw == "kr/investor_flow (KeyError)"


def test_parse_report_detects_vendor_disagreement(tmp_path):
    write_report(
        tmp_path,
        "2026-08-13-evening.md",
        "⚠ 미국 시세 벤더 불일치: SPY 2026-08-04 Tiingo 473.0 vs Alpaca 472.66\n",
    )
    record = parse_report(tmp_path / "2026-08-13-evening.md")
    assert record.vendor_disagreement


def test_parse_report_detects_a_news_gap(tmp_path):
    write_report(
        tmp_path,
        "2026-08-10-morning.md",
        "⚠ 뉴스 유실: 2 feeds did not answer, so their loss is unmeasured — "
        "asiae_stock unverified, last stored article 2026-08-09 09:29Z\n",
    )
    record = parse_report(tmp_path / "2026-08-10-morning.md")
    assert record.news_gap is not None
    assert "asiae_stock" in record.news_gap


def test_parse_report_on_a_clean_report_has_no_failures_or_warnings(tmp_path):
    write_report(
        tmp_path,
        "2026-08-13-evening.md",
        "ℹ 데이터 기준: KR 2026-08-13\nℹ 엔티티 모호 비율: 8.1% (기사 1,240건)\n",
    )
    record = parse_report(tmp_path / "2026-08-13-evening.md")
    assert record.failures == ()
    assert not record.vendor_disagreement
    assert record.news_gap is None


def test_parse_report_on_the_older_single_file_name(tmp_path):
    write_report(tmp_path, "2026-08-03.md", "ℹ 엔티티 모호 비율: 7.9% (기사 1,311건)\n")
    record = parse_report(tmp_path / "2026-08-03.md")
    assert record.run == "single"
    assert record.date == dt.date(2026, 8, 3)


def test_parse_report_on_an_unrecognised_filename_returns_none(tmp_path):
    write_report(tmp_path, "notes.md", "ℹ 엔티티 모호 비율: 5.0% (기사 10건)\n")
    assert parse_report(tmp_path / "notes.md") is None


# --- load(): the 2026-08-17 trap and session-date discipline ----------------


def test_2026_08_17_is_excluded_even_though_a_report_file_exists_for_it(tmp_path):
    """The regression this module exists to prevent: a naive glob over
    `reports/*.md` filtered by filename date would count three genuine
    `schema` failures on a substitute holiday PREREGISTRATION's window
    excludes. `load()` must derive sessions from `trading_days`, not from
    which files happen to be on disk."""
    reports_dir = tmp_path / "reports"
    write_report(
        reports_dir,
        "2026-08-17-evening.md",
        "⚠ 수집 실패: kr_price/schema, kr_flow/schema, kr_index/schema\n",
    )
    # A real in-window session too, clean.
    write_report(reports_dir, "2026-08-18-evening.md", "ℹ 엔티티 모호 비율: 9.0% (기사 1,084건)\n")

    window = load(tmp_path)
    assert dt.date(2026, 8, 17) not in window.sessions
    assert all(r.date != dt.date(2026, 8, 17) for r in window.reports)

    c2 = criterion_2(window)
    assert c2["confirmed"] == []


def test_a_report_for_a_non_session_weekend_date_is_never_opened(tmp_path):
    reports_dir = tmp_path / "reports"
    # 2026-08-15 is a Saturday, not a KRX session.
    write_report(reports_dir, "2026-08-15-evening.md", "⚠ 수집 실패: kr_flow/schema\n")
    window = load(tmp_path, sessions=[dt.date(2026, 8, 13), dt.date(2026, 8, 14)])
    assert window.reports == ()


def test_load_uses_the_real_holiday_calendar_by_default(tmp_path):
    """No fixtures needed — this pins that `load()`'s default `sessions`
    really does come from `trading_days`, not a hardcoded list that could
    silently drift from the calendar library."""
    window = load(tmp_path)
    assert window.sessions == tuple(trading_days("KR", dt.date(2026, 8, 12), dt.date(2026, 8, 26)))
    assert dt.date(2026, 8, 17) not in window.sessions


def test_morning_and_evening_both_load_for_one_session(tmp_path):
    reports_dir = tmp_path / "reports"
    write_report(reports_dir, "2026-08-13-morning.md", "ℹ 엔티티 모호 비율: 8.1% (기사 1,240건)\n")
    write_report(reports_dir, "2026-08-13-evening.md", "ℹ 엔티티 모호 비율: 9.2% (기사 1,190건)\n")
    window = load(tmp_path, sessions=[dt.date(2026, 8, 13)])
    assert {r.run for r in window.reports} == {"morning", "evening"}


def test_run_file_counts_are_read_per_session_date(tmp_path):
    news_dir = tmp_path / "data" / "raw" / "kr" / "news" / "2026-08-13"
    news_dir.mkdir(parents=True)
    (news_dir / "0800.jsonl.gz").write_bytes(b"")
    (news_dir / "0900.jsonl.gz").write_bytes(b"")
    window = load(tmp_path, sessions=[dt.date(2026, 8, 13), dt.date(2026, 8, 14)])
    assert window.run_file_counts[dt.date(2026, 8, 13)] == 2
    assert window.run_file_counts[dt.date(2026, 8, 14)] == 0


# --- criterion_1/2/3: aggregation -------------------------------------------


def _record(date, run, *, ambiguous_pct=None, articles=None, failures=(), vendor=False, gap=None):
    return ReportRecord(
        date=date,
        run=run,
        path=None,
        ambiguous_pct=ambiguous_pct,
        article_count=articles,
        failures=failures,
        vendor_disagreement=vendor,
        news_gap=gap,
    )


def test_criterion_3_reports_all_three_aggregation_variants():
    reports = (
        _record(dt.date(2026, 8, 13), "morning", ambiguous_pct=0.10, articles=100),
        _record(dt.date(2026, 8, 13), "evening", ambiguous_pct=0.20, articles=300),
    )
    window = GateWindow(sessions=(dt.date(2026, 8, 13),), reports=reports, run_file_counts={})
    c3 = criterion_3(window)
    assert c3["simple_mean"] == pytest.approx((0.10 + 0.20) / 2)
    assert c3["weighted_mean"] == pytest.approx((0.10 * 100 + 0.20 * 300) / 400)
    assert c3["evening_mean"] == pytest.approx(0.20)
    assert c3["n"] == 2


def test_criterion_3_on_no_reports_is_none_not_a_crash():
    window = GateWindow(sessions=(), reports=(), run_file_counts={})
    c3 = criterion_3(window)
    assert c3 == {"simple_mean": None, "weighted_mean": None, "evening_mean": None, "n": 0}


def test_criterion_2_separates_confirmed_ambiguous_and_vendor():
    from src.eval.gate_2week import FailureItem

    confirmed_item = FailureItem("kr_flow/schema", "kr_flow", "schema", 1, "confirmed")
    ambiguous_item = FailureItem("macro/coverage", "macro", "coverage", 1, "ambiguous")
    reports = (
        _record(dt.date(2026, 8, 13), "morning", failures=(confirmed_item,)),
        _record(dt.date(2026, 8, 19), "evening", failures=(ambiguous_item,), vendor=True),
    )
    window = GateWindow(sessions=(), reports=reports, run_file_counts={})
    c2 = criterion_2(window)
    assert len(c2["confirmed"]) == 1
    assert len(c2["ambiguous"]) == 1
    assert len(c2["vendor"]) == 1


def test_criterion_1_flags_zero_run_days():
    window = GateWindow(
        sessions=(dt.date(2026, 8, 13), dt.date(2026, 8, 14)),
        reports=(),
        run_file_counts={dt.date(2026, 8, 13): 30, dt.date(2026, 8, 14): 0},
    )
    c1 = criterion_1(window)
    assert c1["zero_run_days"] == [dt.date(2026, 8, 14)]


def test_criterion_1_collects_news_gap_detail():
    gapped = _record(dt.date(2026, 8, 10), "morning", gap="asiae_stock unverified")
    window = GateWindow(sessions=(), reports=(gapped,), run_file_counts={})
    c1 = criterion_1(window)
    assert c1["news_gaps"] == [(dt.date(2026, 8, 10), "morning", "asiae_stock unverified")]


# --- report(): surfaces, never renders a gate-wide verdict ------------------


def test_report_never_renders_a_gate_wide_verdict():
    window = GateWindow(sessions=(dt.date(2026, 8, 13),), reports=(), run_file_counts={})
    text = report(window).lower()
    for word in ("halt", "gate passed", "gate failed", "verdict", " ok ", "recommend"):
        assert word not in text, f"report used decision language: {word!r}"


def test_report_names_missing_sessions():
    window = GateWindow(
        sessions=(dt.date(2026, 8, 13), dt.date(2026, 8, 25)), reports=(), run_file_counts={}
    )
    text = report(window)
    assert "2026-08-25" in text


def test_report_does_not_crash_on_a_completely_empty_window():
    window = GateWindow(sessions=(), reports=(), run_file_counts={})
    text = report(window)
    assert "Criterion 3" in text
    assert "Criterion 2" in text
    assert "Criterion 1" in text
