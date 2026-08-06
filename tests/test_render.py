"""Tests for the briefing renderer. SPEC §2, SPEC §12 step 10.

Offline, against synthetic inputs built here. :func:`render` touches no disk by
design, which is what makes that possible.

The tests that matter most are the ones about **absence**. A renderer that
quietly drops a section it cannot build produces a page that looks complete and
is not — the same failure as a validation check reporting into logs nobody
reads, one level up. Most of this file is about proving the page says what it
does not know.
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from src.report.rating import rate
from src.report.render import (
    ABSENT_SECTIONS,
    ReportInputs,
    _transmission_correlation,
    ratings_frame,
    render,
    render_header,
    render_ratings,
    render_scan,
    volatility_z,
    write_ratings,
)
from src.util.config import WatchlistEntry

DAY = dt.date(2026, 8, 3)
AS_OF = pd.Timestamp("2026-08-03 06:30", tz="UTC")

RATING_CONFIG = {
    "weights": {
        "foreign_flow_5d": 0.30,
        "inst_flow_5d": 0.15,
        "news_polarity": 0.20,
        "rel_strength_20d": 0.15,
        "rev_4w": 0.15,
        "short_ratio": -0.10,
        "valuation_band": 0.05,
    },
    "cut_points": {"strong": 2.0, "moderate": 1.0, "weak": 0.4},
    "confidence": {"min_weight_coverage": 0.5, "max_rationale_terms": 4},
}


def watchlist(*entries: tuple[str, str]) -> list[WatchlistEntry]:
    return [
        WatchlistEntry(ticker=t, name=n, sector="반도체", held=False, market="KR")
        for t, n in entries
    ]


def features_frame(rows: dict[str, dict[str, float]]) -> pd.DataFrame:
    """One row per ticker for DAY, with the z-columns render reads."""
    built = []
    for ticker, scores in rows.items():
        row = {"date": pd.Timestamp(DAY), "ticker": ticker}
        for feature, value in scores.items():
            row[feature] = value
            row[f"{feature}_z"] = value
        built.append(row)
    return pd.DataFrame(built)


def inputs(**overrides) -> ReportInputs:
    base = {
        "day": DAY,
        "as_of": AS_OF,
        "watchlist": watchlist(("005930", "삼성전자")),
        "rating_config": RATING_CONFIG,
    }
    base.update(overrides)
    return ReportInputs(**base)


# --- absence --------------------------------------------------------------


def test_every_unbuilt_section_is_rendered_with_its_reason():
    """The load-bearing test. Omission is what this renderer must never do."""
    page = render(inputs())
    for section, (title, reason) in ABSENT_SECTIONS.items():
        assert f"## {section} {title}" in page
        assert reason in page


def test_the_header_lists_the_unbuilt_sections():
    header = render_header(inputs())
    assert "미구현 섹션" in header
    for section in ABSENT_SECTIONS:
        assert section in header


def test_a_run_with_no_data_at_all_still_renders():
    """CLAUDE.md requires a partial report over no report, so empty inputs must
    produce a page rather than an exception."""
    page = render(ReportInputs(day=DAY, as_of=AS_OF))
    assert page.startswith("# 📅 2026-08-03")
    assert "이 섹션은 아직 없습니다" in page


def test_collector_failures_reach_the_header():
    header = render_header(inputs(collector_failures=["kr/investor_flow (비어 있음)"]))
    assert "수집 실패" in header
    assert "kr/investor_flow" in header


def test_lost_news_reaches_the_header():
    """The finding step 10 exists to close: check_feed_continuity detects this
    correctly and previously reported it only into Actions logs."""
    header = render_header(inputs(news_gaps=["etnews_economy 5.3시간"]))
    assert "뉴스 유실" in header
    assert "etnews_economy 5.3시간" in header


def test_a_failed_delivery_channel_reaches_the_header():
    header = render_header(inputs(delivery_failures=["email"]))
    assert "발송 실패" in header


def test_the_header_states_how_much_rating_weight_is_covered():
    header = render_header(inputs())
    assert "0.75/1.10" in header
    assert "news_polarity" in header and "rev_4w" in header


def test_an_ambiguous_ratio_over_the_threshold_is_flagged():
    calm = render_header(inputs(ambiguous_ratio=0.109, articles_seen=2876))
    hot = render_header(inputs(ambiguous_ratio=0.42, articles_seen=2876))
    assert "10.9%" in calm and "기준선" not in calm
    assert "42.0%" in hot and "기준선 30% 초과" in hot


# --- ① transmission -------------------------------------------------------


def _returns(dates: list[str], values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.to_datetime(dates))


def test_the_correlation_pairs_korea_with_the_previous_us_session():
    """A KR session closes before the US session sharing its date opens. Pairing
    them by date would ask whether Korea reacted to something that had not
    happened yet — a look-ahead violation dressed as a correlation."""
    days = [f"2026-06-{d:02d}" for d in range(1, 21)]
    us = _returns(days, [(-1) ** i * 0.01 for i in range(20)])
    # Korea repeats yesterday's US move exactly, one session later.
    kr = _returns(days, [0.0] + [(-1) ** i * 0.01 for i in range(19)])

    lead = _transmission_correlation(us, kr, window=20)
    assert lead is not None
    assert lead > 0.95, f"the one-session lead should be near-perfect, got {lead}"


def test_a_same_day_pairing_would_not_be_reported_as_the_lead():
    """Guards the direction of the join: a series that only matches same-day
    must not score as a lead relationship."""
    days = [f"2026-06-{d:02d}" for d in range(1, 21)]
    moves = [(-1) ** i * 0.01 for i in range(20)]
    identical = _returns(days, moves)

    lead = _transmission_correlation(identical, identical, window=20)
    assert lead is not None
    assert lead < 0, "alternating same-day series correlate negatively when lagged"


def test_too_little_history_yields_none_rather_than_a_number():
    us = _returns(["2026-06-01", "2026-06-02"], [0.01, -0.01])
    kr = _returns(["2026-06-02", "2026-06-03"], [0.01, -0.01])
    assert _transmission_correlation(us, kr, window=60) is None


# --- ② scan ---------------------------------------------------------------


def test_flow_flags_fire_on_the_spec_thresholds():
    frame = features_frame(
        {
            "005930": {"foreign_flow_5d": 2.0},
            "000660": {"foreign_flow_5d": -2.0},
            "042700": {"foreign_flow_5d": 0.1},
        }
    )
    page = render_scan(
        inputs(
            features=frame,
            watchlist=watchlist(("005930", "삼성"), ("000660", "하이닉스"), ("042700", "한미")),
        )
    )
    assert "`inflow`" in page and "`outflow`" in page


def test_the_scan_names_the_four_flags_it_cannot_compute():
    page = render_scan(inputs(features=features_frame({"005930": {"foreign_flow_5d": 0.5}})))
    for flag in ("valuation_band", "earnings_revision", "filing", "news_spike"):
        assert flag in page


def test_volatility_z_needs_a_full_window():
    """Fewer than 252 prior observations yields nothing, not a short-window
    z-score — the same rule as every other feature in SPEC §5."""
    dates = pd.date_range("2024-01-01", periods=100, freq="D")
    prices = pd.DataFrame(
        {
            "date": dates,
            "ticker": "005930",
            "close": [100 + i for i in range(100)],
        }
    )
    assert volatility_z(prices, dates[-1].date()) == {}


# --- ⑥ ratings ------------------------------------------------------------


def test_the_decomposition_reconciles_with_the_printed_score():
    """SPEC §2.2⑥ requires the printed lines to reach the printed score. Two
    things break that — truncation and renormalization — and only the first is
    named in SPEC. With three of seven features absent the subtotal is 64% of
    the score, so the renormalization step has to appear on the page."""
    # The four features the pipeline actually produces today: coverage 0.70/1.10.
    # Three would put coverage exactly on min_weight_coverage, where floating
    # point decides the outcome — 0.55/1.10 evaluates to 0.49999999999999994.
    z = {
        "foreign_flow_5d": 2.66,
        "inst_flow_5d": -1.06,
        "short_ratio": -0.70,
        "rel_strength_20d": -0.33,
    }
    result = rate("006800", z, RATING_CONFIG)

    page = render_ratings(
        inputs(features=features_frame({"006800": z}), watchlist=watchlist(("006800", "미래에셋"))),
        {"006800": result},
    )

    subtotal = sum(c.value for c in result.contributions)
    assert f"{subtotal:+.3f}" in page
    assert f"{result.weight_coverage:.0%}" in page
    assert f"{result.score:+.2f}" in page
    # The stated arithmetic is the real arithmetic.
    assert subtotal / result.weight_coverage == pytest.approx(result.score)


def test_a_truncated_rationale_shows_its_residual():
    z = {
        "foreign_flow_5d": 1.0,
        "inst_flow_5d": 1.0,
        "short_ratio": 1.0,
        "rel_strength_20d": 1.0,
        "valuation_band": 1.0,
    }
    result = rate("005930", z, RATING_CONFIG)
    page = render_ratings(
        inputs(features=features_frame({"005930": z})),
        {"005930": result},
    )
    assert len(result.contributions) > 4
    assert "그 외 1개 항목" in page


def test_the_rating_section_says_no_llm_wrote_it():
    """CLAUDE.md absolute rule 3, stated on the page rather than only in code."""
    result = rate("005930", {"foreign_flow_5d": 2.0}, RATING_CONFIG)
    page = render_ratings(inputs(), {"005930": result})
    assert "계산" in page and "LLM" in page


# --- ordering and persistence ---------------------------------------------


def test_sections_appear_in_spec_display_order():
    """SPEC §2.3: 헤더 → ⑧ → ① → ⑨ → ② → ③ → ④ → ⑥ → ⑤ → ⑦. IDs are stable
    identifiers, not positions, so ⑧ leads even though it is generated last."""
    page = render(inputs(features=features_frame({"005930": {"foreign_flow_5d": 0.5}})))
    order = ["## ⑧", "## ①", "## ⑨", "## ②", "## ③", "## ④", "## ⑥", "## ⑤", "## ⑦"]
    positions = [page.index(marker) for marker in order]
    assert positions == sorted(positions), "display order does not follow SPEC §2.3"


def test_ratings_are_persisted_without_overwriting(tmp_path):
    """A re-render under different config is a different published fact, not a
    correction of the first — the same reasoning as CLAUDE.md rule 1."""
    result = rate("005930", {"foreign_flow_5d": 2.0}, RATING_CONFIG)
    frame = ratings_frame(inputs(), {"005930": result})

    first = write_ratings(frame, tmp_path, DAY)
    second = write_ratings(frame, tmp_path, DAY)

    assert first is not None and second is not None
    assert first != second
    assert second.name.endswith("-v2.parquet")
    assert first.exists()


def test_an_empty_rating_frame_writes_nothing(tmp_path):
    assert write_ratings(pd.DataFrame(), tmp_path, DAY) is None


def test_the_footer_states_that_nothing_is_executed():
    """SPEC §0 principle 5 and CLAUDE.md absolute rule 2, on the page."""
    page = render(inputs())
    assert "매매를 실행하지 않습니다" in page


# --- the email body --------------------------------------------------------


def test_the_html_summary_carries_no_markdown_markers():
    """The defect this fixes: `**bold**`, `|---|` and `<details>` arrived as
    literal characters in the inbox."""
    from src.report.render import build_summary_html

    z = {"foreign_flow_5d": 2.0, "inst_flow_5d": 1.0, "short_ratio": -1.0}
    result = rate("005930", z, RATING_CONFIG)
    html = build_summary_html(inputs(features=features_frame({"005930": z})), {"005930": result})

    assert "**" not in html
    assert "|---" not in html
    assert "<details>" not in html
    assert "<table" in html and "</table>" in html


def test_the_html_summary_states_the_same_header_facts_as_the_page():
    """Two renderers assembling the header separately would drift, and the
    header is the part that must never be wrong about what is missing."""
    from src.report.render import build_summary_html, header_facts

    source = inputs(news_gaps=["etnews_economy 5.3시간"], ambiguous_ratio=0.087, articles_seen=846)
    _, _, warnings = header_facts(source)
    html = build_summary_html(source, {})

    for line in warnings:
        assert line in html, f"header line missing from the email: {line}"


def test_html_escapes_a_name_containing_markup():
    from src.report.render import build_summary_html

    source = inputs(watchlist=[WatchlistEntry("005930", "A<b>&C", "반도체", False, "KR")])
    result = rate("005930", {"foreign_flow_5d": 2.0}, RATING_CONFIG)
    html = build_summary_html(source, {"005930": result})
    assert "A&lt;b&gt;&amp;C" in html
    assert "A<b>&C" not in html


def test_to_plain_text_strips_the_markers_mail_shows_literally():
    from src.report.render import to_plain_text

    markdown = "# 제목\n\n- 소계 **+0.237** ÷ 충족도\n\n| 종목 | 등급 |\n|---|---|\n| 005930 | 매수 |\n<details>"
    plain = to_plain_text(markdown)

    assert "**" not in plain and "|---" not in plain
    assert "<details>" not in plain
    assert "제목" in plain and "005930  매수" in plain


# --- step 11: run resolution ----------------------------------------------


def test_evening_reports_on_the_session_that_closed_hours_earlier():
    from src.report.render import resolve_run

    at = pd.Timestamp("2026-08-06 12:37", tz="UTC")  # 21:37 KST Thursday
    day, persist = resolve_run("evening", at)
    assert day == dt.date(2026, 8, 6)
    assert persist, "the evening run is the canonical publication"


def test_a_late_evening_run_does_not_ask_for_tomorrows_session():
    """The 2026-08-06 incident. GitHub fired the 12:37 UTC evening run at
    14:56 UTC — two hours nineteen late, the slippage SPEC §1 documents — so
    the clock in Seoul read 00:00 on Friday the 7th. The old code asked for the
    7th because it was a trading day; that session had not opened, every
    feature was None, and thirty-one tickers published as 관망 at 0% coverage."""
    from src.report.render import resolve_run

    at = pd.Timestamp("2026-08-06 15:00", tz="UTC")  # 00:00 KST Friday
    day, _ = resolve_run("evening", at)
    assert day == dt.date(2026, 8, 6), "must report on the session that actually closed"


def test_a_run_during_the_session_reports_on_the_previous_one():
    """Mid-session prices are provisional; rating on them would state a close
    that has not happened."""
    from src.report.render import resolve_run

    at = pd.Timestamp("2026-08-06 02:00", tz="UTC")  # 11:00 KST, market open
    day, _ = resolve_run("evening", at)
    assert day == dt.date(2026, 8, 5)


def test_a_weekend_run_reports_on_fridays_session():
    from src.report.render import resolve_run

    at = pd.Timestamp("2026-08-09 03:00", tz="UTC")  # Sunday noon KST
    day, _ = resolve_run("evening", at)
    assert day == dt.date(2026, 8, 7)


def test_morning_reports_on_the_same_session_and_does_not_persist():
    """No KRX call happens between the evening and morning runs, so the
    morning ratings are identical — persisting them would write a -v2 every
    day saying nothing new."""
    from src.report.render import resolve_run

    at = pd.Timestamp("2026-08-06 22:07", tz="UTC")  # 07:07 KST Friday
    day, persist = resolve_run("morning", at)
    assert day == dt.date(2026, 8, 6)
    assert not persist


def test_an_unknown_run_is_rejected():
    from src.report.render import resolve_run

    with pytest.raises(ValueError, match="unknown run"):
        resolve_run("midday", pd.Timestamp("2026-08-06 12:37", tz="UTC"))


# --- the empty-features guard ---------------------------------------------


def test_a_run_with_no_features_says_so_instead_of_rating_everything_hold():
    """The half of the 2026-08-06 incident that made it invisible: an
    all-관망 page and a no-data page looked identical, and the report opened
    with '오늘은 전 종목이 관망입니다' as though that were a market view."""
    from src.report.render import render_ratings

    empty = {
        ticker: rate(ticker, dict.fromkeys(RATING_CONFIG["weights"]), RATING_CONFIG)
        for ticker in ("005930", "000660")
    }
    page = render_ratings(inputs(), empty)

    assert "피처가 하나도 없어" in page
    assert "오늘은 전 종목이" not in page
    # No rating table: thirty-one rows of +0.00 are what made the broken run
    # look like a finished one.
    assert "005930" not in page
    assert "전체 종목 등급" not in page


def test_the_header_flags_a_session_with_no_features():
    header = render_header(inputs(features=pd.DataFrame()))
    assert "등급 계산 불가" in header


def test_a_normal_run_keeps_the_ordinary_wording():
    z = {"foreign_flow_5d": 2.0, "inst_flow_5d": 1.0, "short_ratio": -1.0}
    result = rate("005930", z, RATING_CONFIG)
    page = render_ratings(inputs(features=features_frame({"005930": z})), {"005930": result})
    assert "피처가 하나도 없어" not in page
    assert "관망이 아닌 종목" in page


# --- step 11: the two US vendors ------------------------------------------


def _us_frame(dates: list[str], closes: list[float], ticker: str = "SPY") -> pd.DataFrame:
    return pd.DataFrame({"date": pd.to_datetime(dates), "ticker": ticker, "close": closes})


def test_preview_extends_the_display_series_and_is_labeled():
    from src.report.render import merge_us_preview

    canonical = _us_frame(["2026-08-04"], [770.0])
    preview = _us_frame(["2026-08-04", "2026-08-05"], [770.0, 772.0])

    merged, preview_dates, disagreements = merge_us_preview(canonical, preview)

    assert preview_dates == [dt.date(2026, 8, 5)]
    assert disagreements == []
    assert len(merged) == 2

    page = render_header(inputs(us_prices=merged, us_preview_dates=preview_dates))
    assert "US 2026-08-05 (Tiingo 프리뷰)" in page


def test_canonical_rows_win_where_both_vendors_cover_a_date():
    """The one-vendor-per-series property survives the merge: the preview only
    ever adds dates, never replaces Alpaca's rows."""
    from src.report.render import merge_us_preview

    canonical = _us_frame(["2026-08-04"], [770.00])
    preview = _us_frame(["2026-08-04"], [770.01])

    merged, _, _ = merge_us_preview(canonical, preview)
    assert len(merged) == 1
    assert float(merged.iloc[0]["close"]) == 770.00


def test_vendor_disagreement_beyond_tolerance_is_flagged():
    from src.report.render import merge_us_preview

    canonical = _us_frame(["2026-08-04"], [770.0])
    preview = _us_frame(["2026-08-04"], [780.0])  # 1.3% apart — adjustment-class

    _, _, disagreements = merge_us_preview(canonical, preview)
    assert len(disagreements) == 1
    assert "SPY" in disagreements[0]

    page = render_header(inputs(vendor_disagreements=disagreements))
    assert "벤더 불일치" in page


def test_the_measured_benign_difference_stays_silent():
    """472.65 vs 472.66 — the rounding-class difference measured on 2024-01-02.
    A line that fires on it would fire daily and train the reader to stop
    looking, which defeats the check."""
    from src.report.render import merge_us_preview

    canonical = _us_frame(["2026-08-04"], [472.65])
    preview = _us_frame(["2026-08-04"], [472.66])

    _, _, disagreements = merge_us_preview(canonical, preview)
    assert disagreements == []


# --- step 11: the status handoff ------------------------------------------


def _write_status(tmp_path, at: pd.Timestamp, collectors: dict) -> None:
    directory = tmp_path / "status"
    directory.mkdir(exist_ok=True)
    payload = {"run": "evening", "at": at.isoformat(), "collectors": collectors}
    (directory / f"evening-{at.strftime('%Y%m%dT%H%M%S')}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_a_fresh_status_file_feeds_the_header(tmp_path):
    from src.report.render import read_status

    _write_status(
        tmp_path,
        pd.Timestamp.now(tz="UTC"),
        {
            "kr_news": {
                "ok": False,
                "failures": [
                    {"name": "feed_continuity", "detail": "etnews_economy lost 5.3h"},
                    {"name": "fetch", "detail": "1 feed unreachable"},
                ],
            }
        },
    )

    failures, gaps = read_status(tmp_path)
    assert gaps == ["etnews_economy lost 5.3h"], "feed continuity is the news-loss signal"
    assert failures == ["kr_news/fetch"]


def test_per_ticker_check_failures_collapse_to_one_counted_entry(tmp_path):
    """Hit on the first live run: a not-yet-open US session failed continuity
    for all 48 tickers, and each would have been its own header item."""
    from src.report.render import read_status

    _write_status(
        tmp_path,
        pd.Timestamp.now(tz="UTC"),
        {
            "us_price_preview": {
                "ok": False,
                "failures": [
                    {"name": f"trading_day_continuity[{t}]", "detail": "1 missing"}
                    for t in ("SPY", "QQQ", "IWM")
                ],
            }
        },
    )

    failures, _ = read_status(tmp_path)
    assert failures == ["us_price_preview/trading_day_continuity×3"]


def test_a_stale_status_file_is_ignored(tmp_path):
    """Yesterday's gap re-printed today trains the reader to skip the line."""
    from src.report.render import read_status

    _write_status(
        tmp_path,
        pd.Timestamp.now(tz="UTC") - pd.Timedelta(2, "D"),
        {"kr_news": {"ok": False, "failures": [{"name": "fetch", "detail": "x"}]}},
    )
    assert read_status(tmp_path) == ([], [])


def test_no_status_directory_means_no_lines(tmp_path):
    from src.report.render import read_status

    assert read_status(tmp_path) == ([], [])
