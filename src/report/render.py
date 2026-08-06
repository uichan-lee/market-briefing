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

Four of the nine sections ship complete (①⑨⑥ and the header), three ship
partial (②③⑦), and three are absent (④⑤⑧). The header says so every day.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.features.compute import FEATURES, z_scores_for
from src.features.normalize import rolling_z
from src.report.rating import Rating, RatingResult, rate
from src.util.config import WatchlistEntry
from src.util.session import to_kst

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
ABSENT_SECTIONS: dict[str, tuple[str, str]] = {
    "④": ("캘린더", "실적·FOMC·IPO 일정 수집기가 아직 없습니다 (SPEC §12 미착수)"),
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
    sector_mapping: Sequence[Mapping[str, object]] = ()
    rating_config: Mapping[str, object] = field(default_factory=dict)
    news_counts: Mapping[str, int] = field(default_factory=dict)
    news_headlines: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    ambiguous_ratio: float | None = None
    articles_seen: int = 0
    collector_failures: Sequence[str] = ()
    news_gaps: Sequence[str] = ()
    delivery_failures: Sequence[str] = ()


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


def render_header(inputs: ReportInputs) -> str:
    """SPEC §2.1. Every degradation of the briefing is stated here.

    The order is deliberate: what the reader can act on first, then everything
    that makes today's briefing less complete than it should be. A reader who
    stops after the header still knows what the document is not telling them.
    """
    kst = to_kst(inputs.as_of)
    weekday = _WEEKDAY_KO[inputs.day.weekday()]
    lines = [f"# 📅 {inputs.day.isoformat()} ({weekday}) {kst:%H:%M} KST 브리핑", ""]

    market = []
    for symbol, label in (("SPY", "S&P 500"), ("QQQ", "NASDAQ"), ("SMH", "SOX")):
        market.append(f"{label} {_fmt_pct(_latest(_daily_returns(inputs.us_prices, symbol)))}")
    usdkrw = _series_at(inputs.macro, "usdkrw")
    level = _latest(usdkrw)
    if level is not None:
        change = usdkrw.dropna().pct_change().iloc[-1] if len(usdkrw.dropna()) > 1 else None
        market.append(f"USDKRW {level:,.0f} ({_fmt_pct(change, digits=1)})")
    if market:
        lines += [" | ".join(market), ""]

    warnings = []
    if inputs.collector_failures:
        warnings.append(f"⚠ 수집 실패: {', '.join(inputs.collector_failures)}")
    if inputs.news_gaps:
        warnings.append(f"⚠ 뉴스 유실: {'; '.join(inputs.news_gaps)}")
    if inputs.delivery_failures:
        warnings.append(f"⚠ 발송 실패: {', '.join(inputs.delivery_failures)}")

    coverage = _weight_coverage(inputs)
    if coverage is not None:
        present, total, missing = coverage
        detail = f" — {', '.join(missing)} 부재" if missing else ""
        warnings.append(
            f"⚠ 등급 근거 충족도: {present:.2f}/{total:.2f} ({present / total:.0%}){detail}"
        )

    absent = ", ".join(f"{key} {title}" for key, (title, _) in ABSENT_SECTIONS.items())
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

    lines += warnings + [""]
    return "\n".join(lines)


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
        "> 나머지 4개가 없는 이유: `valuation_band`는 756세션이 필요한데 백필이 "
        "728세션, `earnings_revision`은 컨센서스 EPS 소스 없음, `filing`은 공시 "
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

    lines += [f"> 등급 이력 {sessions}일치 기록됨. 성과 계산은 SPEC §12 이후 단계입니다.", ""]
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
        _absent("④"),
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
    """
    if frame.empty:
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

    ``as_of`` defaults to the KR session close of ``day``, which is the boundary
    that makes the day's flow data usable. Features are computed with that same
    boundary, so the report never contains a number that was not knowable when
    it claims to have been written.
    """
    from src.features.compute import compute, load_raw
    from src.util.config import load_aliases, load_rating, load_sector_mapping, load_watchlist
    from src.util.session import session_close_utc

    root = root or Path("data")
    raw = root / "raw"
    as_of = as_of or session_close_utc("KR", day)

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

    features = pd.DataFrame()
    if not flow.empty:
        features = compute(flow, kr_prices, watchlist, as_of=None)

    counts, headlines, ambiguous, articles = news_for_day(raw, day, load_aliases())

    return ReportInputs(
        day=day,
        as_of=as_of,
        watchlist=watchlist,
        features=features,
        kr_prices=kr_prices,
        us_prices=us_prices,
        macro=macro,
        sector_mapping=load_sector_mapping(),
        rating_config=load_rating(),
        news_counts=counts,
        news_headlines=headlines,
        ambiguous_ratio=ambiguous,
        articles_seen=articles,
        collector_failures=failures,
    )


def load_rating_history(root: Path) -> pd.DataFrame:
    """Every rating this system has published, for ⑦."""
    directory = root / "ratings"
    if not directory.exists():
        return pd.DataFrame()
    frames = [pd.read_parquet(path) for path in sorted(directory.glob("*.parquet"))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main(argv: Sequence[str] | None = None) -> int:
    """Render one day's briefing and deliver it. SPEC §12 step 10."""
    import argparse

    from src.notify.base import deliver, unavailable_channels
    from src.util.config import load_delivery

    parser = argparse.ArgumentParser(description="Render the daily market briefing.")
    parser.add_argument("--day", default=None, help="KR session date, YYYY-MM-DD (default: today)")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--no-deliver", action="store_true", help="render to stdout only")
    args = parser.parse_args(argv)

    day = dt.date.fromisoformat(args.day) if args.day else dt.date.today()
    root = Path(args.data_root)

    channels = load_delivery().get("channels", [])
    inputs = load_inputs(day, root=root)
    inputs.delivery_failures = unavailable_channels(channels)
    results = rate_all(inputs)

    # Ratings are persisted before the report is rendered, so ⑦ counts today's
    # run in its history. Rendering first would report one session fewer than
    # exists the moment the file lands — a number the reader cannot reconcile
    # against data/ratings/.
    if not args.no_deliver:
        written = write_ratings(ratings_frame(inputs, results), root, day)
        if written:
            print(f"ratings  {written}")

    report = render(inputs, rating_history=load_rating_history(root))

    if args.no_deliver:
        print(report)
        return 0

    for result in deliver(report, channels, day=day):
        print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
