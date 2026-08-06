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
