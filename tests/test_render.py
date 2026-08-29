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
from pathlib import Path

import pandas as pd
import pytest

from src.report.rating import rate
from src.report.render import (
    ABSENT_SECTIONS,
    ReportInputs,
    _absent,
    _llm_section,
    _llm_section_unavailable,
    _transmission_correlation,
    load_rating_history,
    ratings_frame,
    render,
    render_calendar,
    render_header,
    render_ratings,
    render_scan,
    render_shadow,
    volatility_z,
    write_ratings,
)
from src.util.config import AliasEntry, WatchlistEntry

DAY = dt.date(2026, 8, 3)
AS_OF = pd.Timestamp("2026-08-03 06:30", tz="UTC")

RATING_CONFIG = {
    "weights": {
        "foreign_flow_5d": 0.30,
        "inst_flow_5d": 0.15,
        "rel_strength_20d": 0.15,
        "short_ratio": -0.10,
        "valuation_band": 0.05,
    },
    "deferred_weights": {"news_polarity": 0.20, "rev_4w": 0.15},
    "cut_points": {"strong": 2.0, "moderate": 1.0, "weak": 0.4},
    "confidence": {"min_weight_coverage": 0.5, "max_rationale_terms": 4},
}

# The shape config/rating.yaml carried until 2026-08-08: a weight declared for a
# feature nothing produces. Kept so the header's warning for that mistake stays
# tested after the committed config stopped making it.
LEGACY_RATING_CONFIG = {
    **RATING_CONFIG,
    # rev_4w, not news_polarity — news_polarity has had a real producer
    # since 2026-08-27 (src.llm.daily_scoring + src.features.compute), so
    # it no longer demonstrates "a weight for a feature with no producer".
    # rev_4w is permanently dropped and will never have one.
    "weights": {**RATING_CONFIG["weights"], "rev_4w": 0.15},
    "deferred_weights": {},
}


def watchlist(*entries: tuple[str, str]) -> list[WatchlistEntry]:
    return [
        WatchlistEntry(ticker=t, name=n, sector="반도체", held=False, market="KR")
        for t, n in entries
    ]


def aliases_for(*entries: tuple[str, str]) -> dict[str, AliasEntry]:
    """One ``AliasEntry`` per ``(ticker, canonical name)``, for check_commentary().

    ``ReportInputs.aliases`` defaults to ``{}``, which is safe for every test
    that never touches ⑤/⑧ — but *not* for a consistency-drop test, since an
    empty mapping means ``check_commentary()`` recognizes no tickers at all
    and every commentary trivially "passes".
    """
    return {
        t: AliasEntry(ticker=t, canonical=n, aliases=(n,), exclude=(), ambiguous_parents=())
        for t, n in entries
    }


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


def _stub_synthesis(text: str) -> str:
    return "**오늘의 한 줄:** 테스트 총평"


def _stub_redteam(text: str) -> str:
    return "- **테스트** — 테스트 반증"


# --- absence --------------------------------------------------------------


def test_a_genuinely_unbuilt_section_is_rendered_with_its_reason(monkeypatch):
    """The load-bearing test for ABSENT_SECTIONS/`_absent()`. ⑤/⑧ were wired
    2026-08-25 and no longer go through this path (see the ⑤/⑧ tests below) —
    ABSENT_SECTIONS is `{}` in production today, so this test injects a
    synthetic entry rather than asserting on an empty dict, which would pass
    vacuously and silently lose coverage of the mechanism itself."""
    monkeypatch.setitem(ABSENT_SECTIONS, "⑨", ("테스트 섹션", "테스트 사유"))
    assert _absent("⑨") == "## ⑨ 테스트 섹션\n\n> **이 섹션은 아직 없습니다.** 테스트 사유.\n"


def test_the_header_lists_a_genuinely_unbuilt_section(monkeypatch):
    monkeypatch.setitem(ABSENT_SECTIONS, "⑨", ("테스트 섹션", "테스트 사유"))
    header = render_header(inputs())
    assert "미구현 섹션" in header
    assert "⑨" in header


def test_the_header_says_nothing_about_unbuilt_sections_when_there_are_none():
    """ABSENT_SECTIONS is `{}` as of 2026-08-25 (⑤/⑧ wired). The header must
    not print a dangling `⚠ 미구현 섹션: ` line for an empty dict."""
    assert not ABSENT_SECTIONS
    header = render_header(inputs())
    assert "미구현 섹션" not in header


def test_the_header_names_the_features_that_do_not_exist_yet():
    """The line above now reads 100%, which on its own would say the rating is
    better-supported than it is. This is the line that keeps it honest."""
    header = render_header(inputs())

    assert "미구현 피처: news_polarity(0.20), rev_4w(0.15)" in header
    assert "설계 가중치 1.10의 32%" in header


def test_the_header_says_nothing_about_deferred_weights_when_there_are_none():
    config = {**RATING_CONFIG, "deferred_weights": {}}
    header = render_header(inputs(rating_config=config))

    assert "미구현 피처" not in header
    assert "등급 근거 충족도: 0.75/0.75 (100%)" in header


def test_a_weight_for_a_feature_with_no_producer_is_still_flagged():
    """The mistake `deferred_weights` was added to stop. Moving the two names
    out of `weights` must not disarm the check that catches the next one."""
    header = render_header(inputs(rating_config=LEGACY_RATING_CONFIG))

    assert "⚠ 등급 근거 충족도: 0.75/0.90 (83%) — rev_4w 부재" in header


def test_the_data_basis_line_dates_macro_separately_from_prices():
    """Macro runs on its own clock: the FRED FX series publish about a week
    behind, so the USDKRW level on the market line is routinely older than the
    prices beside it. On 2026-08-10 the header showed a 07-31 rate next to
    08-07 prices with nothing to say so."""
    macro = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-07-30"), pd.Timestamp("2026-07-31")],
            "series": ["usdkrw", "usdkrw"],
            "value": [1424.05, 1436.81],
        }
    )
    prices = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-08-03")],
            "ticker": ["005930"],
            "close": [79600.0],
        }
    )
    header = render_header(inputs(macro=macro, kr_prices=prices))

    assert "KR 2026-08-03" in header
    assert "MACRO 2026-07-31" in header


def test_the_data_basis_line_omits_macro_when_there_is_none():
    header = render_header(inputs())
    assert "MACRO" not in header


def test_a_run_with_no_data_at_all_still_renders():
    """CLAUDE.md requires a partial report over no report, so empty inputs must
    produce a page rather than an exception."""
    page = render(
        ReportInputs(day=DAY, as_of=AS_OF), synthesize_fn=_stub_synthesis, redteam_fn=_stub_redteam
    )
    assert page.startswith("# 📅 2026-08-03")
    assert "이 섹션을 만들 수 없습니다" in page


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
    assert "0.75/0.75 (100%)" in header


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


def test_the_scan_names_the_three_flags_it_cannot_compute():
    page = render_scan(inputs(features=features_frame({"005930": {"foreign_flow_5d": 0.5}})))
    for flag in ("valuation_band", "earnings_revision", "news_spike"):
        assert flag in page


def _filings_frame(ticker: str, rcept_dt: dt.date) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "corp_code": "00000000",
                "ticker": ticker,
                "rcept_no": "1",
                "report_nm": "test",
                "flr_nm": "test",
                "date": pd.Timestamp(rcept_dt),
                "known_at_utc": pd.Timestamp(rcept_dt, tz="UTC"),
            }
        ]
    )


def test_filed_tickers_uses_the_previous_trading_day_not_day_itself():
    """SPEC §2.2②'s literal wording — the *previous* day's filing, not DAY's."""
    from src.report.render import filed_tickers
    from src.util.session import previous_trading_day

    filed_on = previous_trading_day("KR", DAY)
    assert filed_tickers(_filings_frame("005930", filed_on), DAY) == {"005930"}


def test_filed_tickers_excludes_two_sessions_back():
    from src.report.render import filed_tickers
    from src.util.session import previous_trading_day

    two_sessions_back = previous_trading_day("KR", previous_trading_day("KR", DAY))
    assert filed_tickers(_filings_frame("005930", two_sessions_back), DAY) == set()


def test_filed_tickers_is_empty_on_an_empty_frame():
    from src.report.render import filed_tickers

    assert filed_tickers(pd.DataFrame(), DAY) == set()


def test_the_filing_flag_fires_in_a_rendered_page():
    from src.util.session import previous_trading_day

    filed_on = previous_trading_day("KR", DAY)
    page = render_scan(
        inputs(
            features=features_frame({"005930": {"foreign_flow_5d": 0.1}}),
            kr_filings=_filings_frame("005930", filed_on),
        )
    )
    assert "`filing`" in page


def test_the_filing_flag_ignores_a_different_tickers_filing():
    """A page-level ``not in`` check would false-pass: the footnote's own
    explanatory sentence always mentions `` `filing` ``. Checking at the
    ``_flags_for`` level avoids that collision."""
    from src.report.render import _flags_for, filed_tickers
    from src.util.session import previous_trading_day

    filed_on = previous_trading_day("KR", DAY)
    filed = filed_tickers(_filings_frame("000660", filed_on), DAY)
    assert "`filing`" not in _flags_for("005930", {}, {}, filed)


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


# --- ④ calendar -------------------------------------------------------------


def calendar_frame(*rows: tuple[dt.date, str, str]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"date": pd.Timestamp(d), "event": event, "label": label} for d, event, label in rows]
    )


def test_calendar_with_no_data_says_so():
    page = render_calendar(inputs())
    assert "캘린더 데이터가 없어" in page


def test_calendar_with_data_but_nothing_today_or_tomorrow_says_so():
    page = render_calendar(
        inputs(calendar=calendar_frame((dt.date(2026, 9, 1), "cpi", "CPI 발표")))
    )
    assert "오늘·내일 예정된 이벤트가 없습니다" in page


def test_calendar_shows_todays_and_tomorrows_events_only():
    frame = calendar_frame(
        (DAY, "cpi", "CPI 발표"),
        (DAY + dt.timedelta(days=1), "fomc", "FOMC 회의 (08/04~08/05)"),
        (DAY + dt.timedelta(days=2), "employment_situation", "고용지표 발표"),  # outside the window
    )
    page = render_calendar(inputs(calendar=frame))
    assert "CPI 발표" in page
    assert "(오늘)" in page
    assert "FOMC 회의" in page
    assert "(내일)" in page
    assert "고용지표 발표" not in page


def test_calendar_names_the_two_sub_sources_still_absent():
    """The section is no longer in ABSENT_SECTIONS once built, so the two
    remaining gaps have to be named inline — the same shape ② and ③ already
    use for their own unbuilt pieces."""
    page = render_calendar(inputs(calendar=calendar_frame((DAY, "cpi", "CPI 발표"))))
    assert "US 개별 종목 실적 발표일" in page
    assert "KR 배당락·IPO 일정" in page


def test_calendar_is_not_in_absent_sections_once_built():
    assert "④" not in ABSENT_SECTIONS


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


# --- ⑤/⑧ LLM sections, wired 2026-08-25 ------------------------------------


def _known_rating_inputs() -> ReportInputs:
    """A ReportInputs whose §2.2⑥ rating for 005930 is real, known (매수, full
    coverage), and named in `aliases` — the shape a consistency-drop test
    needs. An empty `aliases` would make check_commentary() recognize no
    tickers at all and every commentary trivially "pass"."""
    z = {
        "foreign_flow_5d": 2.0,
        "inst_flow_5d": 2.0,
        "rel_strength_20d": 2.0,
        "short_ratio": 2.0,
        "valuation_band": 2.0,
    }
    return inputs(
        watchlist=watchlist(("005930", "삼성전자")),
        features=features_frame({"005930": z}),
        aliases=aliases_for(("005930", "삼성전자")),
    )


def test_llm_section_unavailable_matches_the_absent_shape():
    body = _llm_section_unavailable("⑧", "AI 총평", "테스트 사유")
    assert body == "## ⑧ AI 총평\n\n> **이 섹션은 이번 실행에서 생략됐습니다.** 테스트 사유.\n"


def test_llm_section_reports_a_raised_exception_without_crashing():
    def boom():
        raise RuntimeError("vendor down")

    body, warning = _llm_section("⑧", "AI 총평", "총평", "§2.2⑧", boom, {}, {})
    assert "이번 실행에서 생략됐습니다" in body
    assert warning == "⚠ 총평 생략: LLM 호출 실패: vendor down (§2.2⑧)"


def test_llm_section_publishes_on_a_clean_pass():
    ratings = {"005930": rate("005930", {"foreign_flow_5d": 2.0}, RATING_CONFIG)}
    body, warning = _llm_section("⑧", "AI 총평", "총평", "§2.2⑧", lambda: "본문", ratings, {})
    assert body == "## ⑧ AI 총평\n\n본문\n"
    assert warning is None


def test_the_commentary_section_is_rendered_from_the_injected_synthesize_fn():
    page = render(
        _known_rating_inputs(),
        synthesize_fn=lambda text: "**오늘의 한 줄:** 진짜 총평",
        redteam_fn=_stub_redteam,
    )
    assert "## ⑧ AI 총평" in page
    assert "진짜 총평" in page


def test_the_redteam_section_is_rendered_from_the_injected_redteam_fn():
    page = render(
        _known_rating_inputs(),
        synthesize_fn=_stub_synthesis,
        redteam_fn=lambda text: "- **테스트** — 진짜 반증",
    )
    assert "## ⑤ 반증 (red team)" in page
    assert "진짜 반증" in page


def test_a_contradicting_commentary_is_dropped_with_the_spec_format_header_line():
    page = render(
        _known_rating_inputs(),
        synthesize_fn=lambda text: "- **005930 삼성전자 (매도, -1.00)** — 임의 반박",
        redteam_fn=_stub_redteam,
    )
    assert "⚠ 총평 생략: 등급과 모순 — 005930 (§2.2⑧)" in page
    assert "## ⑧ AI 총평\n\n> **이 섹션은 이번 실행에서 생략됐습니다.**" in page


def test_a_contradicting_redteam_is_dropped_with_the_spec_format_header_line():
    """A ⑤ output stating a rating label at all would already break the prompt's
    own rule — this proves the safety net catches it independently anyway."""
    page = render(
        _known_rating_inputs(),
        synthesize_fn=_stub_synthesis,
        redteam_fn=lambda text: "- **005930 삼성전자 (매도)** — 임의 반박",
    )
    assert "⚠ 반증 생략: 등급과 모순 — 005930 (§2.2⑤)" in page


def test_an_llm_failure_in_the_commentary_degrades_to_a_stated_absence_not_a_crash():
    def boom(text: str) -> str:
        raise RuntimeError("vendor down")

    page = render(inputs(), synthesize_fn=boom, redteam_fn=_stub_redteam)
    assert "⚠ 총평 생략: LLM 호출 실패: vendor down (§2.2⑧)" in page


def test_an_llm_failure_in_the_redteam_degrades_to_a_stated_absence_not_a_crash():
    def boom(text: str) -> str:
        raise RuntimeError("vendor down")

    page = render(inputs(), synthesize_fn=_stub_synthesis, redteam_fn=boom)
    assert "⚠ 반증 생략: LLM 호출 실패: vendor down (§2.2⑤)" in page


def test_the_redteam_input_never_includes_the_ratings_section():
    """SPEC's literal scope for ⑤ is ①-④ — ⑥ and ⑨ are deliberately excluded,
    per src/llm/prompts/v1_redteam.md."""
    captured: list[str] = []

    def record(text: str) -> str:
        captured.append(text)
        return "- ok"

    render(
        inputs(features=features_frame({"005930": {"foreign_flow_5d": 0.5}})),
        synthesize_fn=_stub_synthesis,
        redteam_fn=record,
    )
    assert "방향성 등급" not in captured[0]
    assert "중장기 국면" not in captured[0]
    assert "미국 → 한국" in captured[0]


def test_the_commentary_input_never_includes_the_redteam_output():
    """⑧ reads only rendered deterministic sections — feeding one LLM's prose
    into another as input would compound hallucination risk."""
    captured: list[str] = []

    def record(text: str) -> str:
        captured.append(text)
        return "**오늘의 한 줄:** 총평"

    render(inputs(), synthesize_fn=record, redteam_fn=lambda text: "고유한마커반증텍스트")
    assert "고유한마커반증텍스트" not in captured[0]


def test_render_defaults_to_the_real_synthesize_functions_when_not_injected(monkeypatch):
    """Proves the `None` -> lazy import -> real function wiring actually works,
    at the lowest seam (adapter._call) rather than by trusting the default
    parameter resolves correctly."""
    from src.llm import adapter

    class _Response:
        def __init__(self, content: str) -> None:
            message = type("M", (), {"content": content})()
            self.choices = [type("C", (), {"message": message})()]
            self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()

    def fake_call(**kwargs):
        schema = kwargs["response_format"]["json_schema"]["schema"]
        key = next(iter(schema["properties"]))
        return _Response(json.dumps({key: "실제 함수 경로 확인"}))

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(adapter, "_call", fake_call)
    monkeypatch.setattr(adapter, "_cost", lambda response: None)

    page = render(_known_rating_inputs())
    assert page.count("실제 함수 경로 확인") == 2  # both ⑤ and ⑧


def test_unverifiable_commentary_is_dropped_with_a_distinct_header_warning():
    page = render(
        _known_rating_inputs(),
        synthesize_fn=lambda text: "강한 매수 의견이다.",
        redteam_fn=_stub_redteam,
    )
    assert "⚠ 총평 생략: 등급 검증 불가 — 1건 (§2.2⑧)" in page
    assert "## ⑧ AI 총평\n\n> **이 섹션은 이번 실행에서 생략됐습니다.**" in page


def test_empty_rating_execution_drops_both_llm_sections():
    page = render(
        inputs(),
        synthesize_fn=_stub_synthesis,
        redteam_fn=_stub_redteam,
    )
    assert "⚠ 총평 생략: 등급 검증 불가 — 1건 (§2.2⑧)" in page
    assert "⚠ 반증 생략: 등급 검증 불가 — 1건 (§2.2⑤)" in page


# --- ordering and persistence ---------------------------------------------


def test_sections_appear_in_spec_display_order():
    """SPEC §2.3: 헤더 → ⑧ → ① → ⑨ → ② → ③ → ④ → ⑥ → ⑤ → ⑦. IDs are stable
    identifiers, not positions, so ⑧ leads even though it is generated last."""
    page = render(
        inputs(features=features_frame({"005930": {"foreign_flow_5d": 0.5}})),
        synthesize_fn=_stub_synthesis,
        redteam_fn=_stub_redteam,
    )
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


def test_an_all_zero_coverage_frame_is_not_archived(tmp_path):
    """What produced data/ratings/2026-08-07.parquet: 31 관망 for a session that
    had not opened. render_ratings already refuses to publish this; persistence
    ran first and archived it anyway."""
    blank = {
        t: rate(t, dict.fromkeys(RATING_CONFIG["weights"]), RATING_CONFIG) for t in ("005930",)
    }
    frame = ratings_frame(inputs(), blank)

    assert not frame.empty
    assert write_ratings(frame, tmp_path, DAY) is None
    assert not (tmp_path / "ratings").exists()


def test_one_covered_ticker_is_enough_to_archive(tmp_path):
    """The guard must catch a broken run, not a thin one. A single ticker with a
    single feature is a real session that happens to be sparse."""
    results = {
        "005930": rate("005930", {"foreign_flow_5d": 2.0}, RATING_CONFIG),
        "000660": rate("000660", dict.fromkeys(RATING_CONFIG["weights"]), RATING_CONFIG),
    }
    assert write_ratings(ratings_frame(inputs(), results), tmp_path, DAY) is not None


# --- the archive, read back ------------------------------------------------


def _archive(directory: Path, name: str, day: dt.date, tickers: int = 2) -> None:
    """One parquet under `directory`, shaped like ratings_frame()'s output."""
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "date": [pd.Timestamp(day)] * tickers,
            "ticker": [f"{i:06d}" for i in range(tickers)],
            "rating": ["관망"] * tickers,
            "score": [0.0] * tickers,
            "weight_coverage": [0.5] * tickers,
            "missing": [""] * tickers,
        }
    ).to_parquet(directory / name, index=False)


def test_history_keeps_one_version_per_session(tmp_path):
    """A session re-rendered four times is one session, not four.

    Counting it four times is invisible today — ⑦ reads date.nunique() — and
    corrupting at PREREGISTRATION §8.4, where these rows become an IC.
    """
    directory = tmp_path / "ratings"
    for name in ("2026-08-06.parquet", "2026-08-06-v2.parquet", "2026-08-06-v3.parquet"):
        _archive(directory, name, dt.date(2026, 8, 6))
    _archive(directory, "2026-08-07.parquet", dt.date(2026, 8, 7))

    history = load_rating_history(tmp_path)

    assert len(history) == 4
    assert history["date"].nunique() == 2


def test_the_newest_version_is_the_one_kept(tmp_path):
    directory = tmp_path / "ratings"
    _archive(directory, "2026-08-06.parquet", dt.date(2026, 8, 6), tickers=1)
    _archive(directory, "2026-08-06-v2.parquet", dt.date(2026, 8, 6), tickers=3)

    assert len(load_rating_history(tmp_path)) == 3


def test_version_ten_beats_version_two(tmp_path):
    """Lexical sorting cannot do this. '-' (0x2D) precedes '.' (0x2E), so
    `-v2.parquet` sorts before `.parquet`, and `-v10` before `-v2`."""
    directory = tmp_path / "ratings"
    _archive(directory, "2026-08-06-v2.parquet", dt.date(2026, 8, 6), tickers=2)
    _archive(directory, "2026-08-06-v10.parquet", dt.date(2026, 8, 6), tickers=7)

    assert len(load_rating_history(tmp_path)) == 7


def test_an_unreadable_filename_is_kept_rather_than_dropped(tmp_path):
    """Nothing else writes here, so a name this cannot parse is a surprise.
    Silently discarding it is the failure mode CLAUDE.md puts first."""
    directory = tmp_path / "ratings"
    _archive(directory, "2026-08-06.parquet", dt.date(2026, 8, 6), tickers=2)
    _archive(directory, "backup.parquet", dt.date(2026, 8, 6), tickers=2)

    assert len(load_rating_history(tmp_path)) == 4


def test_an_absent_archive_is_not_an_error(tmp_path):
    assert load_rating_history(tmp_path).empty


def test_the_shadow_section_counts_sessions_not_rows(tmp_path):
    """⑦ must not read a re-rendered day as extra history."""
    directory = tmp_path / "ratings"
    for name in ("2026-08-06.parquet", "2026-08-06-v2.parquet"):
        _archive(directory, name, dt.date(2026, 8, 6))

    section = render_shadow(inputs(), load_rating_history(tmp_path))

    assert "1일치" in section


def test_the_shadow_section_reports_a_pnl_once_the_window_has_sessions(tmp_path, monkeypatch):
    """⑦ stops being a placeholder the moment shadow_portfolio can compute.

    The section is fed `inputs.root` rather than a rediscovered path, so a run
    pointed at a tmp_path cannot have this one section quietly read the real
    archive — which is what the assertion on the *tmp* numbers checks.
    """
    import src.eval.shadow_portfolio as shadow

    directory = tmp_path / "ratings"
    for day in (dt.date(2026, 8, 6), dt.date(2026, 8, 7)):
        _archive(directory, f"{day.isoformat()}.parquet", day)

    track = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-08-13"), pd.Timestamp("2026-08-14")],
            "portfolio": [0.02, 0.05],
            "benchmark": [0.01, 0.01],
        }
    )
    monkeypatch.setattr(shadow, "load", lambda root=None: track)

    section = render_shadow(inputs(root=tmp_path), load_rating_history(tmp_path))

    assert "+5.00%" in section
    assert "+1.00%" in section
    assert "앞섬" in section
    assert "근소한 우위는 우위가 아닙니다" in section


def test_the_shadow_section_survives_a_broken_evaluation_layer(tmp_path, monkeypatch):
    """CLAUDE.md: a partial report beats no report. A failed P&L is a missing
    section, never a missing briefing."""
    import src.eval.shadow_portfolio as shadow

    directory = tmp_path / "ratings"
    for day in (dt.date(2026, 8, 6), dt.date(2026, 8, 7)):
        _archive(directory, f"{day.isoformat()}.parquet", day)

    def boom(root=None):
        raise ValueError("benchmark archive is empty")

    monkeypatch.setattr(shadow, "load", boom)

    section = render_shadow(inputs(root=tmp_path), load_rating_history(tmp_path))
    assert "계산하지 못했습니다" in section
    assert "benchmark archive is empty" in section


def test_the_footer_states_that_nothing_is_executed():
    """SPEC §0 principle 5 and CLAUDE.md absolute rule 2, on the page."""
    page = render(inputs(), synthesize_fn=_stub_synthesis, redteam_fn=_stub_redteam)
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


def test_a_zero_canonical_close_is_flagged_rather_than_skipped():
    """The relative difference divides by the Alpaca close, so a zero made
    `diff` NaN and `NaN > tolerance` is False — the row that most deserved the
    header line was the one silently dropped. A close of zero is not a small
    disagreement, it is a broken quote."""
    from src.report.render import merge_us_preview

    canonical = _us_frame(["2026-08-04"], [0.0])
    preview = _us_frame(["2026-08-04"], [472.66])

    _, _, disagreements = merge_us_preview(canonical, preview)
    assert len(disagreements) == 1
    assert "SPY" in disagreements[0]


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
