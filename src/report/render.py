"""Render the daily briefing. SPEC §2, §12 step 10.

Turns the deterministic pipeline's output into the markdown document a human
reads. **No LLM is involved anywhere in this module**, which is why sections ⑤
and ⑧ appear here only as stated absences.

The plan and the reasoning behind the section-by-section verdicts are in
``notes/step10-plan.md``; the conclusions are carried here.

**A section that cannot be built is rendered, not omitted.** This is the reason
step 10 was brought forward ahead of the LLM stages. ``check_feed_continuity``
already detects lost news correctly and reports it into GitHub Actions logs that
nothing reads — a renderer that silently dropped what it could not build would
reproduce that same failure one level up. CLAUDE.md is explicit: missing data
appears in the report header, not only in logs.

**Rendering is separated from loading.** :func:`render` takes a
:class:`ReportInputs` and touches no disk, so a test can hand it a synthetic
frame; :func:`load_inputs` does the reading. The two failure modes — "the
numbers are wrong" and "the page is unreadable" — are then testable apart.

Five of the nine sections ship complete (①⑨⑥⑦ and the header), two ship
partial (②③), and three are absent (④⑤⑧). The header says so every day.

⑦ moved from partial to complete on 2026-08-13, when
:mod:`src.eval.shadow_portfolio` gave it PREREGISTRATION §8.5's construction.
It still prints an explanation rather than numbers until the measurement window
has a session with both a rating and its next-day return — which is the section
working, not the section missing.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.features.compute import FEATURES, z_scores_for
from src.features.normalize import rolling_z
from src.report.rating import Rating, RatingResult, rate
from src.util.config import WatchlistEntry
from src.util.session import to_kst, to_utc

# SPEC §2.2① — the transmission correlation window, in KR sessions.
CORRELATION_WINDOW = 60

# SPEC §2.2⑨ — the medium-term trend window.
REGIME_WINDOW = 120

# SPEC §2.2② flag thresholds. `news_spike` has no constant here on purpose: it
# is defined as a z-score of post-dedup mention volume, and a z-score needs a
# distribution. News collection began 2026-08-03, so there are days of history
# where 252 sessions are required. The threshold arrives with the data.
FLOW_FLAG_Z = 1.5
VOLATILITY_FLAG_Z = 1.5

_WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")

# Sections with no implementation, and the reason each one gives the reader.
# Written as data rather than inline prose so that the header's "미구현 섹션"
# line and the section bodies can never disagree about what is missing.
#
# Removing "⑧" is a two-file change, not one. src/report/consistency.py
# (the guard that compares every rating label in the LLM's prose against ⑥'s
# computed rating, CLAUDE.md rule 3's "checked before publication" property)
# already exists and is tested, but nothing calls it yet — grep confirms
# render() is not among its callers. Wiring real ⑧ content into render()
# without wiring consistency.py's check in the same change reopens CLAUDE.md
# rule 3: an LLM-authored paragraph could publish already contradicting ⑥
# with nothing to catch it. Found by /project-review on 2026-08-14; do both
# halves together.
ABSENT_SECTIONS: dict[str, tuple[str, str]] = {
    "⑤": ("반증 (red team)", "LLM 단계입니다 — SPEC §12 6~8단계 완료 후 켜집니다"),
    "⑧": ("AI 총평", "LLM 단계입니다 — SPEC §12 6~8단계 완료 후 켜집니다"),
}


@dataclass
class ReportInputs:
    """Everything :func:`render` needs, already loaded.

    Every field defaults to empty so a partial run still renders. A missing
    input becomes a stated absence in its section rather than an exception —
    CLAUDE.md requires a partial report over no report.
    """

    day: dt.date
    as_of: pd.Timestamp
    watchlist: Sequence[WatchlistEntry] = ()
    features: pd.DataFrame = field(default_factory=pd.DataFrame)
    kr_prices: pd.DataFrame = field(default_factory=pd.DataFrame)
    us_prices: pd.DataFrame = field(default_factory=pd.DataFrame)
    macro: pd.DataFrame = field(default_factory=pd.DataFrame)
    calendar: pd.DataFrame = field(default_factory=pd.DataFrame)
    sector_mapping: Sequence[Mapping[str, object]] = ()
    rating_config: Mapping[str, object] = field(default_factory=dict)
    news_counts: Mapping[str, int] = field(default_factory=dict)
    news_headlines: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    ambiguous_ratio: float | None = None
    articles_seen: int = 0
    collector_failures: Sequence[str] = ()
    news_gaps: Sequence[str] = ()
    delivery_failures: Sequence[str] = ()
    # Dates in `us_prices` served by the Tiingo preview rather than the Alpaca
    # canonical series — the header labels them, because a preview number and a
    # canonical number carry different weight (notes/step11-plan.md).
    us_preview_dates: Sequence[dt.date] = ()
    # Close disagreements found where the two US vendors cover the same date.
    # Empty is the normal state; a line here means one vendor changed its
    # adjustment handling and is worth reading.
    vendor_disagreements: Sequence[str] = ()
    # The `data/` directory these inputs were loaded from. Carried rather than
    # rediscovered so ⑦ reads the same archive the rest of the run did — a test
    # pointing at a tmp_path must not have one section quietly read the real one.
    root: Path = field(default_factory=lambda: Path("data"))


# --- small helpers --------------------------------------------------------


def _fmt_pct(value: float | None, *, digits: int = 2) -> str:
    """Signed percentage, or an em dash when the number does not exist.

    An em dash rather than 0.00%: a missing return and a flat session are
    different facts, and printing zero for both is the quiet kind of wrong this
    project spends its effort avoiding.
    """
    if value is None or pd.isna(value):
        return "—"
    return f"{value * 100:+.{digits}f}%"


def _fmt_num(value: float | None, *, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:,.{digits}f}"


def _fmt_z(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:+.2f}"


def _absent(section: str) -> str:
    title, reason = ABSENT_SECTIONS[section]
    return f"## {section} {title}\n\n> **이 섹션은 아직 없습니다.** {reason}.\n"


def _series_at(frame: pd.DataFrame, name: str) -> pd.Series:
    """One macro series as a date-indexed float series, oldest first."""
    if frame.empty or "series" not in frame.columns:
        return pd.Series(dtype="float64")
    rows = frame[frame["series"] == name]
    if rows.empty:
        return pd.Series(dtype="float64")
    out = rows.sort_values("date").set_index("date")["value"]
    return pd.to_numeric(out, errors="coerce")


def _daily_returns(prices: pd.DataFrame, ticker: str) -> pd.Series:
    """Close-to-close returns for one ticker, date-indexed."""
    if prices.empty:
        return pd.Series(dtype="float64")
    rows = prices[prices["ticker"] == ticker]
    if rows.empty:
        return pd.Series(dtype="float64")
    closes = rows.sort_values("date").set_index("date")["close"]
    return pd.to_numeric(closes, errors="coerce").pct_change()


def _latest(series: pd.Series) -> float | None:
    clean = series.dropna()
    return float(clean.iloc[-1]) if len(clean) else None


def _change_over(series: pd.Series, window: int) -> float | None:
    """Level change across ``window`` observations, in the series' own units."""
    clean = series.dropna()
    if len(clean) <= window:
        return None
    return float(clean.iloc[-1] - clean.iloc[-1 - window])


# --- header ---------------------------------------------------------------


def header_facts(inputs: ReportInputs) -> tuple[str, str, list[str]]:
    """The header's content as data: (title, market line, warning lines).

    Extracted so the markdown page and the HTML email state the *same* facts.
    Two renderers each assembling the header from scratch would drift, and the
    header is the one part of the briefing that must never be wrong about what
    the briefing does not know.
    """
    # Titled with the *reading* moment, not the session date. The morning run
    # reads on day D+1 about session D; a title saying D reads as yesterday's
    # mail. Which sessions the numbers come from is the 데이터 기준 line's job.
    kst = to_kst(inputs.as_of)
    weekday = _WEEKDAY_KO[kst.weekday()]
    title = f"📅 {kst:%Y-%m-%d} ({weekday}) {kst:%H:%M} KST 브리핑"

    market = []
    for symbol, label in (("SPY", "S&P 500"), ("QQQ", "NASDAQ"), ("SMH", "SOX")):
        market.append(f"{label} {_fmt_pct(_latest(_daily_returns(inputs.us_prices, symbol)))}")
    usdkrw = _series_at(inputs.macro, "usdkrw")
    level = _latest(usdkrw)
    if level is not None:
        change = usdkrw.dropna().pct_change().iloc[-1] if len(usdkrw.dropna()) > 1 else None
        market.append(f"USDKRW {level:,.0f} ({_fmt_pct(change, digits=1)})")

    warnings: list[str] = []

    # Which sessions the numbers actually come from — required by the step 11
    # plan: a stale number that says so is a different thing from a stale
    # number that does not. Derived from the loaded frames, never from intent.
    coverage_parts = []
    if not inputs.kr_prices.empty:
        coverage_parts.append(f"KR {pd.to_datetime(inputs.kr_prices['date']).max():%Y-%m-%d}")
    if not inputs.us_prices.empty:
        us_max = pd.to_datetime(inputs.us_prices["date"]).max()
        preview = " (Tiingo 프리뷰)" if us_max.date() in set(inputs.us_preview_dates) else ""
        coverage_parts.append(f"US {us_max:%Y-%m-%d}{preview}")
    # Macro earns its own entry because it runs on a different clock from the
    # price frames. The FX series publish about a week behind, so the USDKRW
    # level on the market line above is routinely older than the KR and US dates
    # beside it — on 2026-08-10 it was 07-31 data shown next to 08-07 prices,
    # with nothing in the header to say so.
    if not inputs.macro.empty:
        coverage_parts.append(f"MACRO {pd.to_datetime(inputs.macro['date']).max():%Y-%m-%d}")
    if coverage_parts:
        warnings.append(f"ℹ 데이터 기준: {' · '.join(coverage_parts)}")

    if inputs.collector_failures:
        warnings.append(f"⚠ 수집 실패: {', '.join(inputs.collector_failures)}")
    if inputs.news_gaps:
        warnings.append(f"⚠ 뉴스 유실: {'; '.join(inputs.news_gaps)}")
    if inputs.vendor_disagreements:
        warnings.append(f"⚠ 미국 시세 벤더 불일치: {'; '.join(inputs.vendor_disagreements)}")
    if inputs.delivery_failures:
        warnings.append(f"⚠ 발송 실패: {', '.join(inputs.delivery_failures)}")

    coverage = _weight_coverage(inputs)
    if coverage is not None:
        present, total, missing = coverage
        mark = "⚠" if missing else "ℹ"
        detail = f" — {', '.join(missing)} 부재" if missing else ""
        warnings.append(
            f"{mark} 등급 근거 충족도: {present:.2f}/{total:.2f} ({present / total:.0%}){detail}"
        )

    # The line above can only report what the config asks for, so on its own it
    # would read 100% once the unbuilt features were taken out of `weights` —
    # better-supported than the rating actually is. This names them instead.
    deferred = _deferred_weights(inputs)
    if deferred:
        named = ", ".join(f"{name}({weight:.2f})" for name, weight in deferred)
        share = sum(weight for _, weight in deferred)
        designed = share + (coverage[1] if coverage else 0.0)
        warnings.append(
            f"⚠ 미구현 피처: {named} — 설계 가중치 {designed:.2f}의 {share / designed:.0%}"
        )

    # An all-관망 page is a legitimate outcome and an empty-feature page is a
    # failure, and they look identical to a reader. On 2026-08-06 the second
    # was published as the first — thirty-one tickers rated 관망 at 0% coverage
    # because the requested session had not opened — and nothing on the page
    # said so. If no ticker has a single feature, the header says it outright.
    if inputs.features.empty and inputs.watchlist:
        warnings.append(
            f"⚠ **등급 계산 불가: {inputs.day.isoformat()} 세션의 피처가 없습니다.** "
            "아래 등급은 전부 근거 0%이며 시장에 대한 판단이 아닙니다"
        )

    absent = ", ".join(f"{key} {name}" for key, (name, _) in ABSENT_SECTIONS.items())
    warnings.append(f"⚠ 미구현 섹션: {absent}")

    if inputs.ambiguous_ratio is not None:
        note = (
            " — 기준선 30% 초과, config/aliases.yaml 보강 필요"
            if inputs.ambiguous_ratio > 0.30
            else ""
        )
        warnings.append(
            f"ℹ 엔티티 모호 비율: {inputs.ambiguous_ratio:.1%} "
            f"(기사 {inputs.articles_seen:,}건){note}"
        )

    return title, " | ".join(market), warnings


def render_header(inputs: ReportInputs) -> str:
    """SPEC §2.1, as markdown. Every degradation of the briefing is stated here.

    The order is deliberate: what the reader can act on first, then everything
    that makes today's briefing less complete than it should be. A reader who
    stops after the header still knows what the document is not telling them.
    """
    title, market, warnings = header_facts(inputs)
    lines = [f"# {title}", ""]
    if market:
        lines += [market, ""]
    return "\n".join(lines + warnings + [""])


def _weight_coverage(inputs: ReportInputs) -> tuple[float, float, list[str]] | None:
    """Share of the intended rating weight the features actually cover.

    Read off ``config/rating.yaml`` rather than off any one ticker's result, so
    the header states a property of the *system* — which features exist at all —
    rather than of whichever ticker happened to sort first.
    """
    weights = inputs.rating_config.get("weights")
    if not isinstance(weights, Mapping) or not weights:
        return None
    total = sum(abs(float(w)) for w in weights.values())
    present = sum(abs(float(w)) for name, w in weights.items() if name in FEATURES)
    missing = [name for name in weights if name not in FEATURES]
    return present, total, missing


def _deferred_weights(inputs: ReportInputs) -> list[tuple[str, float]]:
    """Designed weight for features nothing produces yet, largest first.

    Separate from :func:`_weight_coverage` because the two answer different
    questions. That one asks whether the *active* config is honest — a weight
    for a feature with no producer still trips its warning, which is the defect
    this key was introduced to stop repeating. This one asks how far the working
    rating sits from the designed one, which the header would otherwise stop
    saying the moment the unbuilt weights were taken out of ``weights``.
    """
    deferred = inputs.rating_config.get("deferred_weights")
    if not isinstance(deferred, Mapping) or not deferred:
        return []
    named = [(name, abs(float(weight))) for name, weight in deferred.items()]
    return sorted(named, key=lambda item: (-item[1], item[0]))


# --- ① US → KR transmission ----------------------------------------------


def _transmission_correlation(
    us_returns: pd.Series, kr_returns: pd.Series, window: int
) -> float | None:
    """Correlation between each KR session and the US session *before* it.

    Not a same-day correlation. A KR session closes before the US session of the
    same calendar date opens, so pairing them by date would ask whether Korea
    reacted to something that had not happened yet. Each KR session is joined to
    the most recent strictly-earlier US session with :func:`pandas.merge_asof`,
    which is what "US leads Korea by one session" means once holidays make the
    two calendars disagree.
    """
    us, kr = us_returns.dropna(), kr_returns.dropna()
    if len(us) < 2 or len(kr) < 2:
        return None

    left = pd.DataFrame({"date": kr.index, "kr": kr.to_numpy()}).sort_values("date")
    right = pd.DataFrame({"date": us.index, "us": us.to_numpy()}).sort_values("date")
    # allow_exact_matches=False is the lead: a KR session never sees the US
    # session sharing its date.
    joined = pd.merge_asof(left, right, on="date", direction="backward", allow_exact_matches=False)

    paired = joined.dropna(subset=["kr", "us"]).tail(window)
    if len(paired) < window // 2:
        return None
    value = paired["kr"].corr(paired["us"])
    return None if pd.isna(value) else float(value)


def _sector_returns(prices: pd.DataFrame, tickers: Sequence[str]) -> pd.Series:
    """Equal-weighted daily return of a set of KR tickers."""
    if prices.empty or not tickers:
        return pd.Series(dtype="float64")
    frames = [_daily_returns(prices, ticker) for ticker in tickers]
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.Series(dtype="float64")
    return pd.concat(frames, axis=1).mean(axis=1)


def render_transmission(inputs: ReportInputs) -> str:
    """SPEC §2.2①. The briefing's front page, and entirely LLM-free."""
    lines = ["## ① 미국 → 한국 전이", ""]

    if inputs.us_prices.empty:
        return "\n".join(lines + ["> 미국 시세가 없어 이 섹션을 만들 수 없습니다.", ""])

    lines += ["**미국 지수·섹터 (전일 종가 기준)**", ""]
    lines += ["| 심볼 | 설명 | 전일 | 20일 |", "|---|---|---:|---:|"]
    labels = {
        "SPY": "S&P 500",
        "QQQ": "NASDAQ 100",
        "SMH": "반도체",
        "XLK": "기술",
        "XLE": "에너지",
        "XLF": "금융",
        "XBI": "바이오",
        "IWM": "러셀 2000",
    }
    for symbol, label in labels.items():
        returns = _daily_returns(inputs.us_prices, symbol)
        if not len(returns.dropna()):
            continue
        closes = inputs.us_prices[inputs.us_prices["ticker"] == symbol].sort_values("date")["close"]
        closes = pd.to_numeric(closes, errors="coerce")
        twenty = closes.pct_change(20).dropna()
        month = float(twenty.iloc[-1]) if len(twenty) else None
        lines.append(f"| {symbol} | {label} | {_fmt_pct(_latest(returns))} | {_fmt_pct(month)} |")

    lines += ["", "**전이 매핑과 60거래일 상관계수**", ""]
    if not inputs.sector_mapping:
        lines += ["> `config/sector_mapping.yaml`에 매핑이 없습니다.", ""]
        return "\n".join(lines)

    lines += ["| 미국 | 한국 바스켓 | 종목수 | 상관 (60일) | 해석 |", "|---|---|---:|---:|---|"]
    for mapping in inputs.sector_mapping:
        symbol = str(mapping.get("us", ""))
        kr_sector = str(mapping.get("kr_sector", ""))
        # Explicit tickers, not a sector-label join — see config/sector_mapping.yaml.
        members = [str(t) for t in mapping.get("tickers", []) or []]
        correlation = _transmission_correlation(
            _daily_returns(inputs.us_prices, symbol),
            _sector_returns(inputs.kr_prices, members),
            CORRELATION_WINDOW,
        )
        if correlation is None:
            reading = "이력 부족 — 판단 보류"
        elif correlation >= 0.3:
            reading = "전이 유효"
        elif correlation >= 0.1:
            reading = "약함 — 참고만"
        else:
            reading = "**끊어짐 — 이 신호는 무시**"
        shown = "—" if correlation is None else f"{correlation:+.2f}"
        lines.append(f"| {symbol} | {kr_sector} | {len(members)} | {shown} | {reading} |")

    lines += [
        "",
        "> 상관계수는 각 한국 세션을 **그 직전 미국 세션**과 짝지어 계산합니다. "
        "같은 날짜끼리 맞추면 아직 열리지도 않은 미국장에 한국이 반응했는지를 "
        "묻는 셈이 됩니다.",
        "",
    ]
    return "\n".join(lines)


# --- ⑨ medium-term regime -------------------------------------------------


def render_regime(inputs: ReportInputs) -> str:
    """SPEC §2.2⑨. Also LLM-free, and deliberately not a rating input."""
    lines = ["## ⑨ 중장기 국면", ""]
    if inputs.macro.empty:
        return "\n".join(lines + ["> 매크로 데이터가 없어 이 섹션을 만들 수 없습니다.", ""])

    lines += [f"| 지표 | 현재 | {REGIME_WINDOW}일 변화 |", "|---|---:|---:|"]
    rows = (
        ("미국 10년-2년 금리차", "yield_curve_10y2y", 2),
        ("달러 인덱스", "dollar_index", 2),
        ("원/달러", "usdkrw", 1),
        ("WTI", "wti", 2),
        ("VIX", "vix", 2),
    )
    for label, name, digits in rows:
        series = _series_at(inputs.macro, name)
        level = _latest(series)
        if level is None:
            continue
        change = _change_over(series, REGIME_WINDOW)
        shown = "—" if change is None else f"{change:+,.{digits}f}"
        lines.append(f"| {label} | {_fmt_num(level, digits=digits)} | {shown} |")

    if not inputs.us_prices.empty:
        lines += ["", f"**미국 섹터 로테이션 — {REGIME_WINDOW}일 수익률 − SPY**", ""]
        spy = inputs.us_prices[inputs.us_prices["ticker"] == "SPY"].sort_values("date")["close"]
        spy_ret = pd.to_numeric(spy, errors="coerce").pct_change(REGIME_WINDOW).dropna()
        base = float(spy_ret.iloc[-1]) if len(spy_ret) else None
        if base is not None:
            lines += ["| 섹터 ETF | 초과 수익 |", "|---|---:|"]
            for symbol in ("SMH", "XLK", "XLE", "XLF", "XBI", "IWM"):
                closes = inputs.us_prices[inputs.us_prices["ticker"] == symbol].sort_values("date")
                ret = pd.to_numeric(closes["close"], errors="coerce")
                ret = ret.pct_change(REGIME_WINDOW).dropna()
                if not len(ret):
                    continue
                lines.append(f"| {symbol} | {_fmt_pct(float(ret.iloc[-1]) - base)} |")

    lines += [
        "",
        "> 이 지표들은 **⑥ 등급에 들어가지 않습니다.** 매크로는 모든 종목에 "
        "공통이라 PREREGISTRATION §8.4의 섹터 초과수익 평가에서 구조적으로 "
        "상쇄됩니다 (SPEC §2.2⑨).",
        "",
    ]
    return "\n".join(lines)


# --- ② watchlist scan -----------------------------------------------------


def volatility_z(prices: pd.DataFrame, day: dt.date, *, window: int = 20) -> dict[str, float]:
    """20-session realized volatility, z-scored the §5 way, per ticker.

    Computed here rather than in ``features/compute.py`` because it is a §2.2②
    display flag, not a rating input — putting it in ``FEATURES`` would change
    what ``rate()`` consumes, and SPEC's weights do not include it.
    """
    if prices.empty:
        return {}

    frame = prices.sort_values(["ticker", "date"]).copy()
    frame["ret"] = frame.groupby("ticker", observed=True)["close"].transform(
        lambda s: pd.to_numeric(s, errors="coerce").pct_change()
    )
    frame["vol"] = frame.groupby("ticker", observed=True)["ret"].transform(
        lambda s: s.rolling(window).std(ddof=1)
    )
    frame["vol_z"] = frame.groupby("ticker", observed=True, group_keys=False)["vol"].apply(
        lambda s: rolling_z(s)
    )

    today = frame[pd.to_datetime(frame["date"]).dt.date == day]
    return {
        row.ticker: float(row.vol_z)
        for row in today.itertuples()
        if row.vol_z is not None and not pd.isna(row.vol_z)
    }


def _flags_for(
    ticker: str, z: Mapping[str, float | None], volatility: Mapping[str, float]
) -> list[str]:
    """The §2.2② flags that are actually computable. See the section footnote."""
    flags = []
    flow = z.get("foreign_flow_5d")
    if flow is not None and flow > FLOW_FLAG_Z:
        flags.append("`inflow`")
    elif flow is not None and flow < -FLOW_FLAG_Z:
        flags.append("`outflow`")

    vol = volatility.get(ticker)
    if vol is not None and vol > VOLATILITY_FLAG_Z:
        flags.append("`volatility`")
    return flags


def render_scan(inputs: ReportInputs) -> str:
    """SPEC §2.2②. Three of the seven flags are buildable; the rest are named."""
    lines = ["## ② 종목 스캔", ""]
    if inputs.features.empty or not inputs.watchlist:
        return "\n".join(lines + ["> 피처가 없어 이 섹션을 만들 수 없습니다.", ""])

    volatility = volatility_z(inputs.kr_prices, inputs.day)
    header = (
        "| 종목 | 이름 | 외국인 5일 z | 기관 5일 z | 공매도 z "
        "| 상대강도 z | 변동성 z | 기사 | 플래그 |"
    )
    lines += [header, "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for entry in inputs.watchlist:
        z = z_scores_for(inputs.features, entry.ticker, inputs.day)
        if all(value is None for value in z.values()):
            continue
        flags = _flags_for(entry.ticker, z, volatility)
        count = inputs.news_counts.get(entry.ticker, 0)
        name = entry.name or ""
        row = (
            f"| {entry.ticker} | {name} | {_fmt_z(z.get('foreign_flow_5d'))} "
            f"| {_fmt_z(z.get('inst_flow_5d'))} | {_fmt_z(z.get('short_ratio'))} "
            f"| {_fmt_z(z.get('rel_strength_20d'))} | {_fmt_z(volatility.get(entry.ticker))} "
            f"| {count or '—'} | {' '.join(flags) or '—'} |"
        )
        lines.append(f"**{row}**" if flags else row)

    lines += [
        "",
        "> SPEC §2.2②는 플래그 7개를 정의하지만 **3개만 계산됩니다.** "
        "`inflow`/`outflow`는 외국인 5일 z ≷ ∓1.5, `volatility`는 20일 실현변동성 "
        "z > 1.5입니다.",
        ">",
        # 세션 수를 적지 않는 것은 의도적이다. 저장 창은 KRX 세션마다 1씩 늘어나서
        # 여기 박아 둔 숫자는 다음 날부터 틀린다 — 실제로 728로 박혀 있던 동안
        # 창은 732가 됐다. 도달일은 거래일 달력에서 나오는 고정된 사실이라 안 썩는다.
        "> 나머지 4개가 없는 이유: `valuation_band`는 756세션이 필요한데 백필 창이 "
        "아직 못 미칩니다(백필을 늘리지 않아도 2026-09-11에 30종목, 2026-11-12에 "
        "454910이 도달합니다), `earnings_revision`은 컨센서스 EPS 소스 없음, "
        "`filing`은 공시 "
        "수집기 없음, **`news_spike`는 일별 언급량의 z-score가 필요한데 뉴스 "
        "수집이 며칠치뿐이라 아직 분포가 없습니다** — 기사 열은 z가 아니라 "
        "당일 건수입니다.",
        "",
    ]
    return "\n".join(lines)


# --- ③ news aggregation ---------------------------------------------------


def render_news(inputs: ReportInputs) -> str:
    """SPEC §2.2③, without the scores. Counts are deterministic; polarity is not.

    The section ships as counts and headlines rather than waiting for §6.2,
    because mention volume is itself evidence and it is available today. What is
    absent — polarity, uncertainty, intensity — is named, so nobody reads the
    counts as sentiment.
    """
    lines = ["## ③ 뉴스 집계", ""]
    if not inputs.news_counts:
        return "\n".join(lines + ["> 이 날짜에 워치리스트 종목과 매칭된 기사가 없습니다.", ""])

    lines += ["| 종목 | 기사 | 대표 헤드라인 |", "|---|---:|---|"]
    ranked = sorted(inputs.news_counts.items(), key=lambda kv: -kv[1])
    names = {entry.ticker: (entry.name or "") for entry in inputs.watchlist}
    for ticker, count in ranked:
        if not count:
            continue
        headline, link = inputs.news_headlines.get(ticker, ("", ""))
        shown = f"[{headline}]({link})" if headline and link else (headline or "—")
        lines.append(f"| {ticker} {names.get(ticker, '')} | {count} | {shown} |")

    lines += [
        "",
        "> **점수가 아니라 건수입니다.** SPEC §2.2③의 polarity·uncertainty·"
        "intensity는 §6.2 LLM 스코어링 단계에서 나오는데 그 단계가 아직 "
        "없습니다. 건수 자체는 결정론적이라 지금도 유효한 증거입니다.",
        "",
    ]
    return "\n".join(lines)


# --- ④ calendar -------------------------------------------------------------


def render_calendar(inputs: ReportInputs) -> str:
    """SPEC §2.2④, partial. CPI/employment/FOMC/options expiry are built;
    US 개별 종목 실적 발표일 and KR 배당락·IPO are named absent inline, the
    way ②'s four unbuilt flags and ③'s missing score dimensions already name
    their own remaining gaps — this section is no longer in ABSENT_SECTIONS,
    so hiding nothing here is what keeps CLAUDE.md's "state what's missing"
    rule intact for the two sub-sources still not built
    (notes/calendar-collector-plan.md).
    """
    lines = ["## ④ 캘린더", ""]
    if inputs.calendar.empty:
        lines.append("> 캘린더 데이터가 없어 이 섹션을 만들 수 없습니다.")
    else:
        today = pd.Timestamp(inputs.day)
        # Positional form with an explicit unit, not the days= keyword: the
        # keyword form builds a NumPy timedelta with no unit, which NumPy has
        # deprecated (src/collectors/macro.py's _parse has the same note).
        tomorrow = today + pd.Timedelta(1, "D")
        window = inputs.calendar[
            pd.to_datetime(inputs.calendar["date"]).isin([today, tomorrow])
        ].sort_values("date")
        if window.empty:
            lines.append("> 오늘·내일 예정된 이벤트가 없습니다.")
        else:
            lines += ["| 날짜 | 이벤트 | 설명 |", "|---|---|---|"]
            for row in window.itertuples():
                when = "오늘" if row.date.date() == inputs.day else "내일"
                lines.append(f"| {row.date:%Y-%m-%d} ({when}) | {row.event} | {row.label} |")

    lines += [
        "",
        "> SPEC §2.2④의 5개 항목 중 CPI·고용지표·FOMC·옵션 만기 4개가 구현됐습니다. "
        "**US 개별 종목 실적 발표일: 아직 없음** — 무료 소스를 못 찾았습니다 "
        "(Alpaca corporate-actions API는 배당·분할·합병만 제공하고 실적 캘린더가 "
        "없습니다; notes/us-rating-plan.md가 미국 개별 종목을 §2.2⑥ 범위 밖으로 "
        "둔 것과 같은 이유로 우선순위도 낮습니다). "
        "**KR 배당락·IPO 일정: 아직 없음** — pykrx 공개 함수 90개를 전수 확인한 "
        "결과 해당 기능이 없고, DART API 문서는 아직 검토 전입니다.",
        "",
    ]
    return "\n".join(lines)


# --- ⑥ directional rating -------------------------------------------------


def rate_all(inputs: ReportInputs) -> dict[str, RatingResult]:
    """Rate every watchlist ticker for ``inputs.day``."""
    if inputs.features.empty or not inputs.rating_config:
        return {}
    results = {}
    for entry in inputs.watchlist:
        z = z_scores_for(inputs.features, entry.ticker, inputs.day)
        results[entry.ticker] = rate(entry.ticker, z, inputs.rating_config)
    return results


_FEATURE_LABEL_KO = {
    "foreign_flow_5d": "외국인 5일 순매수",
    "inst_flow_5d": "기관 5일 순매수",
    "short_ratio": "공매도 잔고 비중",
    "rel_strength_20d": "20일 섹터 상대강도",
    "valuation_band": "3년 PBR 밴드",
    "news_polarity": "뉴스 polarity",
    "rev_4w": "4주 컨센서스 EPS 변화",
}


def render_ratings(inputs: ReportInputs, results: Mapping[str, RatingResult]) -> str:
    """SPEC §2.2⑥. Computed, never written — CLAUDE.md absolute rule 3."""
    lines = ["## ⑥ 방향성 등급", ""]
    if not results:
        return "\n".join(lines + ["> 등급을 계산할 피처가 없습니다.", ""])

    limit = int(inputs.rating_config.get("confidence", {}).get("max_rationale_terms", 4))
    names = {entry.ticker: (entry.name or "") for entry in inputs.watchlist}

    ordered = sorted(results.values(), key=lambda r: -abs(r.score))
    actionable = [r for r in ordered if r.rating is not Rating.HOLD]

    # Every ticker at zero coverage means no feature reached rate() at all.
    # That is a broken run, not a quiet market, and the section must not open
    # with a sentence that reads like a market view.
    if ordered and all(result.weight_coverage == 0 for result in ordered):
        session = inputs.day.isoformat()
        return "\n".join(
            lines
            + [
                f"> ⚠ **{session} 세션의 피처가 하나도 없어 등급을 낼 수 없습니다.**",
                ">",
                "> 전 종목이 근거 0%로 `관망`이 되는데, 이건 시장이 조용하다는 뜻이 아니라 "
                "**계산에 쓸 데이터가 없다**는 뜻입니다. 등급표는 생략합니다.",
                "",
            ]
        )

    lines += [
        f"관망이 아닌 종목 **{len(actionable)}개** / 전체 {len(ordered)}개.",
        "",
    ]

    for result in actionable:
        lines += _render_one_rating(result, names.get(result.ticker, ""), limit)

    if not actionable:
        lines += ["오늘은 전 종목이 `관망`입니다.", ""]

    lines += ["<details><summary>전체 종목 등급</summary>", ""]
    lines += ["| 종목 | 이름 | 등급 | 점수 | 근거 충족도 |", "|---|---|---|---:|---:|"]
    for result in ordered:
        lines.append(
            f"| {result.ticker} | {names.get(result.ticker, '')} | {result.rating} "
            f"| {result.score:+.2f} | {result.weight_coverage:.0%} |"
        )
    lines += ["", "</details>", ""]

    lines += [
        "> 등급은 `config/rating.yaml`의 가중합으로 **계산**됩니다. "
        "LLM이 등급이나 그 근거를 문장으로 쓰는 일은 없습니다 "
        "(CLAUDE.md 절대 규칙 3). 근거가 부족한 종목은 자신 있게 매기지 않고 "
        "`관망`으로 내려갑니다.",
        "",
    ]
    return "\n".join(lines)


def _render_one_rating(result: RatingResult, name: str, limit: int) -> list[str]:
    """One ticker's rating, decomposed so the printed lines reach the printed score.

    Two things break naive reconciliation, and SPEC §2.2⑥ only names the first:

    1. **Truncation.** ``rationale()`` returns the top ``limit`` contributions;
       the rest are summarized in a residual line.
    2. **Renormalization.** ``rate()`` computes ``score = Σ(weight × z) / coverage``
       so that a partially-covered ticker sits on the same scale as a complete
       one. With three of seven features absent, coverage is 0.64 and the
       subtotal is only 64% of the score — so even printing *every* contribution
       leaves the arithmetic looking wrong. SPEC's worked example assumes near
       full coverage and does not cover this case.

    Both steps are therefore shown. A number on the page that does not reconcile
    teaches the reader to stop checking, which defeats the reason the rationale
    is a decomposition at all.
    """
    lines = [f"### {result.ticker} {name} — {result.rating} ({result.score:+.2f})", ""]

    shown = result.rationale(limit)
    for contribution in shown:
        label = _FEATURE_LABEL_KO.get(contribution.feature, contribution.feature)
        lines.append(f"- {label} z={contribution.z_score:+.2f} → 기여 {contribution.value:+.3f}")

    subtotal = sum(c.value for c in result.contributions)
    dropped = len(result.contributions) - len(shown)
    if dropped > 0:
        residual = subtotal - sum(c.value for c in shown)
        lines.append(f"- 그 외 {dropped}개 항목 → 기여 {residual:+.3f}")

    if result.weight_coverage < 1.0:
        absent = ", ".join(_FEATURE_LABEL_KO.get(m, m) for m in result.missing)
        lines.append(
            f"- 소계 **{subtotal:+.3f}** ÷ 근거 충족도 {result.weight_coverage:.0%} "
            f"= **{result.score:+.2f}**"
        )
        lines.append(f"  - _부재: {absent} — 나머지 가중치로 재정규화됨_")
    else:
        lines.append(f"- 합계 **{subtotal:+.2f}**")

    lines.append("")
    return lines


# --- ⑦ shadow portfolio ---------------------------------------------------


def render_shadow(inputs: ReportInputs, history: pd.DataFrame | None) -> str:
    """SPEC §2.2⑦. Empty until a rating history accumulates.

    Rendered from day one anyway, holding the count of sessions recorded. A
    section that appears only once it has something to say is a section nobody
    knows is coming.

    The construction is PREREGISTRATION §8.5's and is computed by
    :mod:`src.eval.shadow_portfolio`; nothing about it is decided here. The
    import is local because the briefing must still render if the evaluation
    layer is broken — a failed P&L is a missing section, never a missing report.
    """
    lines = ["## ⑦ 섀도 포트폴리오", ""]
    sessions = 0 if history is None or history.empty else history["date"].nunique()
    if sessions < 2:
        lines += [
            f"> 기록된 등급이 **{sessions}일치**뿐이라 아직 성과를 낼 수 없습니다. "
            "등급은 매 실행마다 `data/ratings/`에 쌓이고, 이 섹션은 이력이 "
            "모이면 자동으로 채워집니다.",
            "",
        ]
        return "\n".join(lines)

    try:
        from src.eval.shadow_portfolio import load, summary

        stats = summary(load(inputs.root))
    except (OSError, KeyError, ValueError) as exc:  # noqa: BLE001 - reported, not raised
        lines += [f"> ⚠ 성과를 계산하지 못했습니다: {exc}", ""]
        return "\n".join(lines)

    if not stats["sessions"]:
        lines += [
            f"> 등급 이력 {sessions}일치 기록됨. PREREGISTRATION §8.5의 측정 창이 "
            "2026-08-13에 열렸고, 한 세션은 등급과 그 다음 세션 수익률이 **둘 다** "
            "있어야 들어갑니다. 아직 그런 세션이 없습니다.",
            "",
        ]
        return "\n".join(lines)

    portfolio, benchmark = stats["portfolio"], stats["benchmark"]
    verdict = "앞섬" if stats["excess"] > 0 else "뒤짐"
    lines += [
        "> 상위 20% 동일가중 롱온리, 매 세션 다음 시가에 리밸런스. "
        f"**{stats['sessions']}세션** 누적.",
        "",
        "| | 누적 수익률 |",
        "|---|---:|",
        f"| 섀도 포트폴리오 | {portfolio:+.2%} |",
        f"| KODEX 200 매수후보유 | {benchmark:+.2%} |",
        f"| **차이** | **{stats['excess']:+.2%}** ({verdict}) |",
        "",
        "> 수수료·거래세·슬리피지는 반영되지 않았습니다 (PREREGISTRATION §8.5가 6개월 "
        "게이트로 미룬 항목). KODEX 200은 분배금이 가격에 반영되지 않아 벤치마크가 "
        "그만큼 불리하게 잡힙니다 — **근소한 우위는 우위가 아닙니다.**",
        "",
        "> 이 계좌는 주문을 내지 않습니다 (SPEC §0 원칙 5). 3개월 게이트 판독은 2026-11-13입니다.",
        "",
    ]
    return "\n".join(lines)


# --- the whole document ---------------------------------------------------


def render(inputs: ReportInputs, *, rating_history: pd.DataFrame | None = None) -> str:
    """Render the full briefing in SPEC §2.3 display order.

    §2.3 order is ``헤더 → ⑧ → ① → ⑨ → ② → ③ → ④ → ⑥ → ⑤ → ⑦``. Section IDs are
    stable identifiers and are not display positions, so ⑧ leads the document
    even though it is generated last — and today, absent.
    """
    results = rate_all(inputs)
    parts = [
        render_header(inputs),
        _absent("⑧"),
        render_transmission(inputs),
        render_regime(inputs),
        render_scan(inputs),
        render_news(inputs),
        render_calendar(inputs),
        render_ratings(inputs, results),
        _absent("⑤"),
        render_shadow(inputs, rating_history),
        _footer(inputs),
    ]
    return "\n".join(parts)


def _footer(inputs: ReportInputs) -> str:
    kst = to_kst(inputs.as_of)
    return (
        "---\n\n"
        f"_생성 {kst:%Y-%m-%d %H:%M} KST · 이 문서는 매매를 실행하지 않습니다 "
        "(SPEC §0 원칙 5). 등급은 계산된 의견이고, 방아쇠는 사람이 당깁니다._\n"
    )


def ratings_frame(inputs: ReportInputs, results: Mapping[str, RatingResult]) -> pd.DataFrame:
    """The published ratings, as the row-per-ticker record kept in ``data/ratings/``."""
    rows = []
    for ticker, result in sorted(results.items()):
        rows.append(
            {
                "date": pd.Timestamp(inputs.day),
                "ticker": ticker,
                "rating": str(result.rating),
                "score": result.score,
                "weight_coverage": result.weight_coverage,
                "missing": ",".join(result.missing),
            }
        )
    return pd.DataFrame(rows)


def write_ratings(frame: pd.DataFrame, root: Path, day: dt.date) -> Path | None:
    """Persist one day's ratings, never overwriting an earlier write.

    Same ``-v2`` discipline as the collectors, for the same reason: what was
    published is a fact about that run, and a re-render with different config
    produces a different fact rather than a correction of the first.

    A frame the report refuses to *publish* is not archived either. The
    condition is the one :func:`render_ratings` already uses to suppress the
    rating table — every ticker at zero coverage means no feature reached
    :func:`rate` at all, which is a broken run rather than a quiet market.
    `data/ratings/2026-08-07.parquet` is what this prevents: thirty-one 관망
    ratings for a session that had not opened, written because persistence ran
    before the render-side check. Both guards must move together.
    """
    if frame.empty:
        return None
    if (frame["weight_coverage"] == 0).all():
        return None
    directory = root / "ratings"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}.parquet"
    suffix = 2
    while path.exists():
        path = directory / f"{day.isoformat()}-v{suffix}.parquet"
        suffix += 1
    frame.to_parquet(path, index=False)
    return path


# --- loading --------------------------------------------------------------


def news_for_day(
    root: Path, day: dt.date, entries: Mapping[str, object]
) -> tuple[dict[str, int], dict[str, tuple[str, str]], float | None, int]:
    """Resolve one day's collected articles onto watchlist tickers.

    Returns per-ticker counts, a representative headline each, the ambiguous
    ratio for the header, and how many articles were seen. Resolution runs here
    rather than being read from a stored file because ``config/aliases.yaml`` is
    hand-edited: re-resolving means an alias fixed today improves every report
    rendered afterwards, including for past days.
    """
    from src.entity.resolve import resolve

    directory = root / "kr" / "news" / day.isoformat()
    if not directory.exists():
        return {}, {}, None, 0

    articles = []
    for path in sorted(directory.glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            articles.extend(json.loads(line) for line in handle)
    if not articles:
        return {}, {}, None, 0

    seen: set[str] = set()
    unique = []
    for article in articles:
        if article["article_id"] in seen:
            continue
        seen.add(article["article_id"])
        unique.append(article)

    matches, report = resolve(unique, entries)
    by_id = {article["article_id"]: article for article in unique}

    counts: dict[str, int] = {}
    headlines: dict[str, tuple[str, str]] = {}
    if not matches.empty:
        for ticker, group in matches.groupby("ticker"):
            counts[str(ticker)] = int(group["article_id"].nunique())
            first = by_id.get(group.iloc[0]["article_id"], {})
            headlines[str(ticker)] = (first.get("title", ""), first.get("link", ""))

    return counts, headlines, report.ambiguous_ratio, report.articles


def load_inputs(
    day: dt.date,
    *,
    root: Path | None = None,
    as_of: pd.Timestamp | None = None,
) -> ReportInputs:
    """Read everything the briefing needs off disk.

    ``as_of`` is the look-ahead boundary and is passed straight through to
    :func:`src.features.compute.compute`, so the report never contains a number
    that was not knowable when it claims to have been written.

    **It defaults to now, not to the session close, and the difference is a
    one-instant off-by-one that used to disable the guard entirely.**
    ``kr_flow`` stamps ``known_at_utc`` for session `D` as *exactly*
    ``session_close_utc("KR", D)``, and ``compute._visible`` filters on strict
    ``<`` — deliberately, since a row is not usable at the instant it becomes
    known. Defaulting the boundary to that same close therefore excluded the
    rated session's own row: every feature came back ``None``, every ticker fell
    to 관망 at 0% coverage, and ``write_ratings`` would have archived nothing.
    The previous code sidestepped that by passing ``as_of=None`` into
    ``compute()`` — no boundary at all — while this docstring claimed the
    boundary was applied. Nothing was actually wrong in the published numbers,
    because every downstream window (``rolling_z``, ``rolling_percentile``,
    ``pct_change``) is trailing by construction; but that was the only thing
    holding the property, and nothing checked it.

    Now is the correct default because a briefing about session `D` is written
    *after* `D` closes — the scheduled evening run publishes six hours later, and
    ``main()`` already passes the render instant for both scheduled runs. One
    consequence worth knowing: a manual replay (``--day``) is titled with the
    real generation moment rather than the replayed session's close, since
    :func:`header_facts` titles on ``as_of``. Pass ``as_of`` explicitly to
    reproduce a past run's boundary.
    """
    from src.features.compute import compute, load_raw
    from src.util.config import load_aliases, load_rating, load_sector_mapping, load_watchlist
    from src.util.session import now_utc

    root = root or Path("data")
    raw = root / "raw"
    as_of = as_of or now_utc()

    watchlist = load_watchlist(market="KR")
    failures: list[str] = []

    def read(source: str, key: tuple[str, ...] = ("date", "ticker")) -> pd.DataFrame:
        try:
            frame = load_raw(raw, source, key=key)
        except (OSError, KeyError, ValueError) as exc:
            failures.append(f"{source} ({type(exc).__name__})")
            return pd.DataFrame()
        if frame.empty:
            failures.append(f"{source} (비어 있음)")
        return frame

    flow = read("kr/investor_flow")
    kr_prices = read("kr/price")
    us_prices = read("us/price")
    macro = read("macro", key=("date", "series"))
    calendar = read("calendar", key=("event", "date"))

    # The Tiingo preview extends the *display* series past the Alpaca recency
    # boundary. Features never see it: `compute()` reads only KR frames, and
    # the canonical us/price path stays single-vendor. Absence is normal here
    # (every evening run, any day before the first morning run), so no failure
    # is recorded for an empty preview.
    try:
        preview = load_raw(raw, "us/price_preview")
    except (OSError, KeyError, ValueError) as exc:
        failures.append(f"us/price_preview ({type(exc).__name__})")
        preview = pd.DataFrame()

    us_prices, preview_dates, disagreements = merge_us_preview(us_prices, preview)

    features = pd.DataFrame()
    if not flow.empty:
        features = compute(flow, kr_prices, watchlist, as_of=as_of)

    counts, headlines, ambiguous, articles = news_for_day(raw, day, load_aliases())
    status_failures, news_gaps = read_status(root)

    return ReportInputs(
        day=day,
        as_of=as_of,
        watchlist=watchlist,
        features=features,
        kr_prices=kr_prices,
        us_prices=us_prices,
        macro=macro,
        calendar=calendar,
        sector_mapping=load_sector_mapping(),
        rating_config=load_rating(),
        news_counts=counts,
        news_headlines=headlines,
        ambiguous_ratio=ambiguous,
        articles_seen=articles,
        collector_failures=[*failures, *status_failures],
        news_gaps=news_gaps,
        us_preview_dates=preview_dates,
        vendor_disagreements=disagreements,
        root=root,
    )


# Relative close difference between the two US vendors above which the header
# says so. The benign case measured 2024-01-02 (SPY 472.65 vs 472.66) is 0.002%;
# a dividend or split handled differently moves a close by whole percents. The
# threshold sits between the two regimes.
VENDOR_TOLERANCE = 0.001


def merge_us_preview(
    canonical: pd.DataFrame, preview: pd.DataFrame
) -> tuple[pd.DataFrame, list[dt.date], list[str]]:
    """Extend the display series with preview rows, and compare the overlap.

    Returns the merged frame, the dates only the preview supplied (the header
    labels them), and any close disagreements on dates both vendors cover.
    Canonical rows always win — the preview only ever *adds* dates, so the
    feature-facing property "one vendor per series" survives the merge because
    the merged frame is display-only.
    """
    if preview.empty:
        return canonical, [], []
    if canonical.empty:
        return preview, sorted(pd.to_datetime(preview["date"]).dt.date.unique()), []

    canonical_days = set(pd.to_datetime(canonical["date"]).dt.date)
    preview_only = preview[~pd.to_datetime(preview["date"]).dt.date.isin(canonical_days)]
    preview_dates = sorted(pd.to_datetime(preview_only["date"]).dt.date.unique())
    merged = (
        pd.concat([canonical, preview_only], ignore_index=True) if len(preview_only) else canonical
    )

    # The cross-check this project kept Tiingo for: where both vendors state a
    # close for the same (date, ticker), they must agree to within tolerance.
    overlap = canonical.merge(preview, on=["date", "ticker"], suffixes=("_alpaca", "_tiingo"))
    disagreements: list[str] = []
    if len(overlap):
        alpaca = pd.to_numeric(overlap["close_alpaca"], errors="coerce")
        tiingo = pd.to_numeric(overlap["close_tiingo"], errors="coerce")
        diff = (tiingo - alpaca).abs() / alpaca.where(alpaca != 0)
        # A zero or unparseable canonical close makes `diff` NaN, and `NaN >
        # tolerance` is False — so the row that most deserves the header line
        # was the one silently skipped. A close of zero is not a small
        # disagreement, it is a broken quote, and it is reported as such.
        bad = overlap[(diff > VENDOR_TOLERANCE) | diff.isna()]
        for row in bad.itertuples():
            disagreements.append(
                f"{row.ticker} {pd.Timestamp(row.date):%Y-%m-%d} "
                f"Tiingo {row.close_tiingo} vs Alpaca {row.close_alpaca}"
            )
        if len(disagreements) > 3:
            disagreements = [*disagreements[:3], f"외 {len(disagreements) - 3}건"]
    return merged, preview_dates, disagreements


# Status files older than this are stale — yesterday's gap warning re-printed
# today would train the reader to ignore the line that matters.
_STATUS_MAX_AGE_HOURS = 20


def read_status(root: Path) -> tuple[list[str], list[str]]:
    """Check failures and news gaps from the most recent collection run.

    ``scripts/collect_daily.py`` writes one JSON per run under ``data/status/``.
    This is the bridge CLAUDE.md's failure-handling section requires: without
    it, a failed check exists only in an Actions log nobody reads, which is the
    exact defect the 2026-08-06 review found in the news pipeline.
    """
    directory = root / "status"
    if not directory.exists():
        return [], []

    newest, newest_at = None, None
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            at = pd.Timestamp(payload["at"])
        except (OSError, ValueError, KeyError):
            continue  # one malformed file must not cost the header its lines
        if newest_at is None or at > newest_at:
            newest, newest_at = payload, at

    if newest is None or (pd.Timestamp.now(tz="UTC") - newest_at).total_seconds() > (
        _STATUS_MAX_AGE_HOURS * 3600
    ):
        return [], []

    counted: dict[str, int] = {}
    gaps: list[str] = []
    for name, outcome in newest.get("collectors", {}).items():
        for check in outcome.get("failures", []):
            # Feed continuity is the news-loss signal and gets its own header
            # line; everything else is a plain collection failure.
            if check.get("name") == "feed_continuity":
                gaps.append(check.get("detail", name)[:200])
                continue
            # Per-ticker checks are named `continuity[005930]`; collapsing on
            # the bracket keeps a 48-ticker failure to one counted entry
            # instead of 48 header items nobody will read past.
            kind = str(check.get("name", "?")).split("[")[0]
            counted[f"{name}/{kind}"] = counted.get(f"{name}/{kind}", 0) + 1

    failures = [key if n == 1 else f"{key}×{n}" for key, n in counted.items()]
    return failures, gaps


_ARCHIVED = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-v(\d+))?$")


def latest_rating_files(directory: Path) -> list[Path]:
    """One file per session — the newest version of each.

    :func:`write_ratings` never overwrites, so a session re-rendered four times
    leaves four parquets. Reading all of them weights that session four times
    against one rendered once, which is invisible today (⑦ only counts distinct
    dates) and corrupting at PREREGISTRATION §8.4, where these rows become an IC
    computation.

    **Newest wins.** A re-render happens because the earlier run was wrong, and
    keeping a known-wrong rating for evaluation measures the bug rather than the
    method. It also keeps this directory agreeing with ``reports/``, which a
    re-render overwrites in place — two artefacts stating different ratings for
    one day would be worse than either choice on its own.

    **Sorting cannot do this.** ``-`` (0x2D) sorts before ``.`` (0x2E), so
    ``2026-08-06-v2.parquet`` precedes ``2026-08-06.parquet`` lexically, and
    ``-v10`` precedes ``-v2``. The version has to be parsed as a number.

    A name this cannot parse is kept under its own stem rather than dropped:
    nothing else writes here, so an unrecognised file is a surprise worth
    surfacing in the data, and silently discarding it is exactly the failure
    mode CLAUDE.md puts first.
    """
    best: dict[str, tuple[int, Path]] = {}
    for path in sorted(directory.glob("*.parquet")):
        match = _ARCHIVED.match(path.stem)
        session = match.group(1) if match else path.stem
        version = int(match.group(2) or 1) if match else 1
        if version > best.get(session, (0, path))[0]:
            best[session] = (version, path)
    return [path for _, (_, path) in sorted(best.items())]


def load_rating_history(root: Path) -> pd.DataFrame:
    """Every rating this system has published, for ⑦ — one version per session."""
    directory = root / "ratings"
    if not directory.exists():
        return pd.DataFrame()
    frames = [pd.read_parquet(path) for path in latest_rating_files(directory)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def resolve_run(run: str, at: pd.Timestamp) -> tuple[dt.date, bool]:
    """Which KR session a scheduled run reports on, and whether it persists ratings.

    Both runs report on **the last session whose close has passed**, which is
    the only definition that survives a late start. Resolved from the running
    clock rather than from a calendar date: the earlier version asked for
    "today, if today is a trading day", and when GitHub fired the evening run
    two hours and nineteen minutes late on 2026-08-06 the clock had rolled past
    midnight in Seoul onto a Friday. It requested a session that had not
    opened, found no features, and published thirty-one 관망 ratings at 0%
    coverage. See :func:`src.util.session.last_closed_session`.

    * ``evening`` (21:37 KST) — normally the session that closed six hours
      earlier. The canonical publication: fresh KRX data arrived minutes ago,
      so its ratings are the ones ⑦ tracks.
    * ``morning`` (07:07 KST) — the same session the evening run reported on,
      since no KRX call happens between them. Its ratings would therefore be a
      duplicate, so it does not persist them; what it adds is the overnight US
      session, which is §2.2①'s subject.
    """
    from src.util.session import last_closed_session

    if run not in {"morning", "evening"}:
        raise ValueError(f"unknown run {run!r}")
    return last_closed_session("KR", at), run == "evening"


def build_summary(inputs: ReportInputs, results: Mapping[str, RatingResult]) -> str:
    """The ``body: summary`` form — header plus ⑥, SPEC §2.1's mobile scan.

    The full document is hundreds of lines of tables that read badly on a
    phone; the header already carries every degradation, and ⑥ is the section
    a thirty-second reader acts on. The vault copy stays complete.
    """
    return "\n".join([render_header(inputs), render_ratings(inputs, results)])


# --- the HTML email body ---------------------------------------------------
#
# Generated directly rather than converted from the markdown above. Markdown in
# an email arrives as literal `**bold**`, `|---|` and `<details>` text — Ricky's
# phone showed exactly that on 2026-08-06 — and parsing it back would mean a
# dependency plus a parser to keep correct. The facts come from `header_facts`
# and the `RatingResult` objects, so both forms state the same thing.
#
# Styling is inline: Gmail strips <style> blocks in some clients. Colours follow
# the Korean convention (red up, blue down) and are chosen to stay legible on
# both light and dark backgrounds, since the reader's client picks one and the
# message cannot know which.

_UP = "#e05252"
_DOWN = "#4a9eff"
_MUTED = "#8a8a8a"


def _esc(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _rating_color(result: RatingResult) -> str:
    if result.rating is Rating.HOLD:
        return _MUTED
    return _UP if result.score > 0 else _DOWN


def build_summary_html(inputs: ReportInputs, results: Mapping[str, RatingResult]) -> str:
    """The same summary as HTML, for the email channel."""
    title, market, warnings = header_facts(inputs)
    names = {entry.ticker: (entry.name or "") for entry in inputs.watchlist}
    limit = int(inputs.rating_config.get("confidence", {}).get("max_rationale_terms", 4))

    out = [
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',"
        "'Malgun Gothic',sans-serif;font-size:15px;line-height:1.55;max-width:680px\">",
        f'<h2 style="margin:0 0 4px;font-size:19px;">{_esc(title)}</h2>',
    ]
    if market:
        out.append(f'<div style="font-size:14px;margin-bottom:12px;">{_esc(market)}</div>')

    if warnings:
        out.append(
            '<div style="border-left:3px solid #999;padding:8px 12px;margin:0 0 18px;'
            'font-size:13px;">' + "<br>".join(_esc(line) for line in warnings) + "</div>"
        )

    ordered = sorted(results.values(), key=lambda r: -abs(r.score))
    actionable = [r for r in ordered if r.rating is not Rating.HOLD]

    out.append(
        f'<p style="margin:0 0 12px;"><b>관망이 아닌 종목 {len(actionable)}개</b> '
        f"/ 전체 {len(ordered)}개</p>"
    )

    for result in actionable:
        color = _rating_color(result)
        out.append(
            f'<div style="margin:0 0 14px;padding:10px 12px;border:1px solid #ccc;'
            f'border-radius:6px;">'
            f'<div style="font-size:16px;margin-bottom:6px;">'
            f"<b>{_esc(result.ticker)} {_esc(names.get(result.ticker, ''))}</b> "
            f'<span style="color:{color};font-weight:bold;">{_esc(result.rating)} '
            f"({result.score:+.2f})</span></div>"
        )
        rows = []
        for contribution in result.rationale(limit):
            label = _FEATURE_LABEL_KO.get(contribution.feature, contribution.feature)
            sign = _UP if contribution.value > 0 else _DOWN
            rows.append(
                f'<tr><td style="padding:1px 10px 1px 0;">{_esc(label)}</td>'
                f'<td style="padding:1px 10px 1px 0;text-align:right;">'
                f"z={contribution.z_score:+.2f}</td>"
                f'<td style="padding:1px 0;text-align:right;color:{sign};">'
                f"{contribution.value:+.3f}</td></tr>"
            )
        subtotal = sum(c.value for c in result.contributions)
        dropped = len(result.contributions) - len(result.rationale(limit))
        if dropped > 0:
            residual = subtotal - sum(c.value for c in result.rationale(limit))
            rows.append(
                f'<tr><td style="padding:1px 10px 1px 0;">그 외 {dropped}개</td>'
                f'<td></td><td style="padding:1px 0;text-align:right;">{residual:+.3f}</td></tr>'
            )
        out.append(
            '<table style="border-collapse:collapse;font-size:13px;width:100%;">'
            + "".join(rows)
            + "</table>"
        )
        if result.weight_coverage < 1.0:
            out.append(
                f'<div style="font-size:12px;color:{_MUTED};margin-top:6px;">'
                f"소계 {subtotal:+.3f} ÷ 근거 충족도 {result.weight_coverage:.0%} "
                f"= <b>{result.score:+.2f}</b></div>"
            )
        out.append("</div>")

    if not actionable:
        out.append("<p>오늘은 전 종목이 <b>관망</b>입니다.</p>")

    out.append('<p style="margin:18px 0 6px;"><b>전체 종목</b></p>')
    out.append(
        '<table style="border-collapse:collapse;font-size:13px;width:100%;">'
        '<tr style="border-bottom:1px solid #999;">'
        '<th style="text-align:left;padding:3px 8px 3px 0;">종목</th>'
        '<th style="text-align:left;padding:3px 8px 3px 0;">등급</th>'
        '<th style="text-align:right;padding:3px 8px 3px 0;">점수</th>'
        '<th style="text-align:right;padding:3px 0;">근거</th></tr>'
    )
    for result in ordered:
        color = _rating_color(result)
        shown_name = _esc(names.get(result.ticker) or result.ticker)
        out.append(
            f'<tr style="border-bottom:1px solid #3a3a3a20;">'
            f'<td style="padding:3px 8px 3px 0;">{shown_name}</td>'
            f'<td style="padding:3px 8px 3px 0;color:{color};">{_esc(result.rating)}</td>'
            f'<td style="padding:3px 8px 3px 0;text-align:right;">{result.score:+.2f}</td>'
            f'<td style="padding:3px 0;text-align:right;color:{_MUTED};">'
            f"{result.weight_coverage:.0%}</td></tr>"
        )
    out.append("</table>")

    out.append(
        f'<p style="font-size:12px;color:{_MUTED};margin-top:18px;">'
        "등급은 <code>config/rating.yaml</code>의 가중합으로 <b>계산</b>됩니다. "
        "LLM이 등급이나 그 근거를 쓰는 일은 없습니다. "
        "이 문서는 매매를 실행하지 않습니다.</p>"
    )
    out.append("</div>")
    return "\n".join(out)


def to_plain_text(markdown: str) -> str:
    """Strip the markdown markers that arrive as literal characters in mail.

    The text/plain alternative, shown by clients that refuse HTML. Not a full
    converter — it removes exactly the markers this renderer emits.
    """
    lines = []
    for raw in markdown.splitlines():
        line = raw.replace("**", "").replace("`", "")
        stripped = line.strip()
        if stripped.startswith("<") and stripped.endswith(">"):
            continue  # <details>/<summary> tags
        if set(stripped) <= set("|-: ") and "|" in stripped:
            continue  # table rule rows
        if stripped.startswith("#"):
            line = line.lstrip("# ").strip()
        if "|" in line:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            line = "  ".join(cell for cell in cells if cell)
        lines.append(line.replace("_", ""))
    return "\n".join(lines)


def _failure_notice(run: str, reading_date: dt.date, detail: str) -> str:
    return (
        f"# ⚠ 브리핑 생성 실패 — {reading_date.isoformat()} {run}\n\n"
        f"오류: {detail}\n\n"
        "리포트가 아예 만들어지지 못해 이 통지가 대신 발송되었습니다 — "
        "무소식이 최악이라는 CLAUDE.md 실패 규칙에 따른 것입니다.\n\n"
        "확인: GitHub Actions 로그 → `uv run python -m src.report.render`\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Render one run's briefing and deliver it. SPEC §12 steps 10–11."""
    import argparse
    import traceback

    from src.notify.base import deliver, unavailable_channels
    from src.util.config import load_delivery
    from src.util.session import now_utc

    parser = argparse.ArgumentParser(description="Render the daily market briefing.")
    parser.add_argument("--run", choices=("morning", "evening"), default=None)
    parser.add_argument(
        "--date", default=None, help="reading date, YYYY-MM-DD (default: today KST)"
    )
    parser.add_argument("--day", default=None, help="explicit KR session date (manual/historical)")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--no-deliver", action="store_true", help="render to stdout only")
    args = parser.parse_args(argv)

    now = now_utc()
    reading_date = dt.date.fromisoformat(args.date) if args.date else to_kst(now).date()

    if args.run:
        # `--date` shifts the whole run, clock included, so a replay resolves
        # the session the same way the live run would have.
        at = now if not args.date else to_utc(f"{reading_date.isoformat()} 12:37")
        day, persist_ratings = resolve_run(args.run, at)
        label: str | None = args.run
        as_of: pd.Timestamp | None = now
    else:
        # Manual/historical invocation: --day names the session directly, the
        # file carries no run label, and as_of defaults to the session close.
        day = dt.date.fromisoformat(args.day) if args.day else reading_date
        reading_date = day
        label, persist_ratings, as_of = None, True, None

    root = Path(args.data_root)
    channels = load_delivery().get("channels", [])

    try:
        inputs = load_inputs(day, root=root, as_of=as_of)
        inputs.delivery_failures = unavailable_channels(channels)
        results = rate_all(inputs)

        # Ratings are persisted before the report is rendered, so ⑦ counts
        # today's run in its history — rendering first would state one session
        # fewer than data/ratings/ holds the moment the file lands.
        if persist_ratings and not args.no_deliver:
            written = write_ratings(ratings_frame(inputs, results), root, day)
            if written:
                print(f"ratings  {written}")
            else:
                print(f"ratings  not archived — {day} produced no rateable feature")

        report = render(inputs, rating_history=load_rating_history(root))
        summary = to_plain_text(build_summary(inputs, results))
        summary_html = build_summary_html(inputs, results)
    except Exception as exc:  # noqa: BLE001 - the notice is the point: no report may pass silently
        traceback.print_exc()
        notice = _failure_notice(args.run or "manual", reading_date, f"{type(exc).__name__}: {exc}")
        if not args.no_deliver:
            for result in deliver(notice, channels, day=reading_date, label="FAILED"):
                print(result)
        return 1

    if args.no_deliver:
        print(report)
        return 0

    for result in deliver(
        report,
        channels,
        day=reading_date,
        label=label,
        summary=summary,
        summary_html=summary_html,
    ):
        print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
