"""Tests for the Alpaca US price path.

Split deliberately in two.

The offline tests cover the logic that is ours rather than Alpaca's: the
two-pass merge of raw and adjusted bars, pagination, and the refusal to fall
back to IEX when SIP is unavailable.

The ``network`` tests cover the three facts that were settled by measurement
rather than from the documentation, because the documentation was either silent
or self-contradictory on all three: that the free plan serves consolidated SIP
history, that a daily bar is stamped at Eastern midnight with an offset that
tracks daylight saving, and that the whole watchlist costs two requests. Each
is an assumption the collector would be wrong without, so each fails here rather
than by quietly seeding the report with bad numbers.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from src.collectors import us_price_alpaca as alpaca
from src.collectors.us_price import SCHEMA


@pytest.fixture(autouse=True)
def _keys(request, monkeypatch):
    """Placeholder credentials for the offline tests only.

    Network tests need the real key from the environment, so the fixture stands
    aside for them — otherwise it would substitute a dummy and every live test
    would fail authentication for a reason that has nothing to do with what it
    is checking.
    """
    if request.node.get_closest_marker("network"):
        return
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")


def bar(t: str, close: float, volume: int = 1_000_000) -> dict:
    return {"t": t, "o": close, "h": close, "l": close, "c": close, "v": volume, "n": 1}


# --- the two-pass merge ---------------------------------------------------


def test_raw_and_adjusted_passes_land_in_separate_columns():
    """Alpaca adjusts the whole bar, so the two passes must not overwrite.

    `close` has to stay unadjusted — it anchors the known-value check, and an
    adjusted close is restated on every dividend. `adj_close` has to be the
    adjusted one, or a split reads as a crash.
    """
    raw = {"SPY": [bar("2024-01-02T05:00:00Z", 472.65)]}
    adjusted = {"SPY": [bar("2024-01-02T05:00:00Z", 470.10)]}

    df = alpaca.normalize(raw, adjusted)
    assert df.loc[0, "close"] == 472.65
    assert df.loc[0, "adj_close"] == 470.10


def test_a_bar_with_no_adjusted_counterpart_is_an_error():
    # Filling it silently would make every return wrong by the size of the
    # next dividend, which is the failure mode the Tiingo path guards too.
    raw = {"SPY": [bar("2024-01-02T05:00:00Z", 472.65)]}
    with pytest.raises(alpaca.AlpacaError, match="no adjusted counterpart"):
        alpaca.normalize(raw, {"SPY": []})


def test_the_frame_matches_the_tiingo_schema_exactly():
    # A second source is only a drop-in replacement if it satisfies the same
    # contract; anything else pushes the difference downstream.
    raw = {"SPY": [bar("2024-01-02T05:00:00Z", 472.65)]}
    df = alpaca.normalize(raw, raw)
    assert list(df.columns) == list(SCHEMA)


def test_known_at_is_the_session_close_not_midnight():
    raw = {"SPY": [bar("2024-01-02T05:00:00Z", 472.65)]}
    df = alpaca.normalize(raw, raw)
    known = pd.Timestamp(df.loc[0, "known_at_utc"])
    # 16:00 ET in January is 21:00 UTC. A bar must not be usable before its
    # own session has closed.
    assert known.hour == 21


def test_a_timestamp_is_read_as_an_eastern_session_date():
    """05:00Z on 2024-01-02 is midnight ET the same day, not the day before."""
    raw = {"SPY": [bar("2024-01-02T05:00:00Z", 472.65)]}
    df = alpaca.normalize(raw, raw)
    assert pd.Timestamp(df.loc[0, "date"]).date() == dt.date(2024, 1, 2)


def test_several_symbols_come_back_as_separate_rows():
    raw = {
        "SPY": [bar("2024-01-02T05:00:00Z", 472.65)],
        "QQQ": [bar("2024-01-02T05:00:00Z", 409.52)],
    }
    df = alpaca.normalize(raw, raw)
    assert set(df["ticker"]) == {"SPY", "QQQ"}
    assert len(df) == 2


def test_an_empty_response_yields_an_empty_frame_with_the_schema():
    df = alpaca.normalize({}, {})
    assert df.empty
    assert list(df.columns) == list(SCHEMA)


# --- pagination -----------------------------------------------------------


def test_pagination_is_followed_to_the_end(monkeypatch):
    """A backfill exceeds the 10,000-bar page limit, so stopping at page one
    would silently truncate the history rather than fail."""
    pages = [
        {"bars": {"SPY": [bar("2024-01-02T05:00:00Z", 1.0)]}, "next_page_token": "p2"},
        {"bars": {"SPY": [bar("2024-01-03T05:00:00Z", 2.0)]}, "next_page_token": None},
    ]
    seen_tokens: list[str | None] = []

    def fake_request(params, *, timeout):
        seen_tokens.append(params.get("page_token"))
        return pages[len(seen_tokens) - 1]

    monkeypatch.setattr(alpaca, "_request", fake_request)
    out = alpaca._fetch_bars(
        ["SPY"], dt.date(2024, 1, 2), dt.date(2024, 1, 3), adjustment="raw", feed="sip", timeout=1
    )
    assert len(out["SPY"]) == 2
    assert seen_tokens == [None, "p2"]


def test_runaway_pagination_stops_rather_than_looping(monkeypatch):
    # A token that never clears would otherwise hammer a metered API forever.
    monkeypatch.setattr(
        alpaca,
        "_request",
        lambda params, *, timeout: {"bars": {"SPY": []}, "next_page_token": "always"},
    )
    with pytest.raises(alpaca.AlpacaError, match="did not terminate"):
        alpaca._fetch_bars(
            ["SPY"],
            dt.date(2024, 1, 2),
            dt.date(2024, 1, 3),
            adjustment="raw",
            feed="sip",
            timeout=1,
        )


def test_every_symbol_travels_in_one_request(monkeypatch):
    """The entire reason for this module: 48 symbols must not cost 48 requests."""
    calls: list[dict] = []

    def fake_request(params, *, timeout):
        calls.append(params)
        return {"bars": {}, "next_page_token": None}

    monkeypatch.setattr(alpaca, "_request", fake_request)
    alpaca._fetch_bars(
        ["SPY", "QQQ", "IWM"],
        dt.date(2024, 1, 2),
        dt.date(2024, 1, 3),
        adjustment="raw",
        feed="sip",
        timeout=1,
    )
    assert len(calls) == 1
    assert calls[0]["symbols"] == "SPY,QQQ,IWM"


# --- the feed question ----------------------------------------------------


def test_a_refused_feed_raises_rather_than_falling_back(monkeypatch):
    """If the plan will not serve SIP, that must stop the run.

    A quiet fallback to IEX is the dangerous outcome: Alpaca's own FAQ puts
    AAPL at 923,134 shares on IEX against 51,861,083 on SIP for the same
    session, so the numbers would stay believable while being wrong by ~56x.
    """

    class Refused:
        status_code = 403
        text = "subscription does not permit querying recent SIP data"

    monkeypatch.setattr(alpaca.requests, "get", lambda *a, **k: Refused())
    with pytest.raises(alpaca.AlpacaFeedError, match="feed='sip'"):
        alpaca._fetch_bars(
            ["SPY"],
            dt.date(2024, 1, 2),
            dt.date(2024, 1, 2),
            adjustment="raw",
            feed="sip",
            timeout=1,
        )


def test_fetch_defaults_to_the_consolidated_feed(monkeypatch):
    captured: dict = {}

    def fake_fetch_bars(symbols, start, end, *, adjustment, feed, timeout):
        captured["feed"] = feed
        return {}

    monkeypatch.setattr(alpaca, "_fetch_bars", fake_fetch_bars)
    alpaca.fetch(["SPY"], dt.date(2024, 1, 2), dt.date(2024, 1, 2))
    assert captured["feed"] == "sip"


def test_missing_credentials_raise_as_configuration(monkeypatch):
    # Every symbol would fail identically, which makes this configuration
    # rather than a data condition — the same split the Tiingo path makes.
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    with pytest.raises(alpaca.AlpacaError, match="not set"):
        alpaca._headers()


# --- reporting ------------------------------------------------------------


def test_symbols_that_return_nothing_are_named(monkeypatch):
    def fake_fetch_bars(symbols, start, end, *, adjustment, feed, timeout):
        return {"SPY": [bar("2024-01-02T05:00:00Z", 472.65)]}

    monkeypatch.setattr(alpaca, "_fetch_bars", fake_fetch_bars)
    _, report = alpaca.fetch(["SPY", "GONE"], dt.date(2024, 1, 2), dt.date(2024, 1, 2))
    fetch = next(c for c in report.results if c.name == "fetch")
    assert not fetch.passed
    assert "GONE" in fetch.detail


# --- live -----------------------------------------------------------------


@pytest.mark.network
def test_the_free_plan_still_serves_consolidated_data():
    """The one assumption the whole switch rests on.

    If Alpaca ever moves historical SIP behind the paid plan, this fails here
    rather than by quietly seeding the report with one venue's volume. The
    pinned close is the discriminator: IEX answered 472.66 on 1.6% of the true
    volume when this was measured, which is wrong in a way that still looks
    plausible.
    """
    bars = alpaca._fetch_bars(
        ["SPY"],
        dt.date(2024, 1, 2),
        dt.date(2024, 1, 2),
        adjustment="raw",
        feed="sip",
        timeout=30,
    )
    spy = bars["SPY"][0]
    assert spy["c"] == 472.65
    assert spy["v"] > 50_000_000, "volume this low means IEX bars, not consolidated"


@pytest.mark.network
def test_a_bar_is_stamped_at_eastern_midnight_in_both_dst_states():
    """The offset must track daylight saving, which is why the date is taken
    in America/New_York rather than by slicing the string."""
    winter = alpaca._fetch_bars(
        ["SPY"],
        dt.date(2024, 1, 2),
        dt.date(2024, 1, 2),
        adjustment="raw",
        feed="sip",
        timeout=30,
    )["SPY"][0]
    summer = alpaca._fetch_bars(
        ["SPY"],
        dt.date(2026, 7, 27),
        dt.date(2026, 7, 27),
        adjustment="raw",
        feed="sip",
        timeout=30,
    )["SPY"][0]
    assert winter["t"].endswith("T05:00:00Z")  # EST
    assert summer["t"].endswith("T04:00:00Z")  # EDT


@pytest.mark.network
def test_the_whole_watchlist_costs_two_requests():
    """The entire justification for this module over the Tiingo path."""
    from src.collectors.us_price import INDEX_ETFS
    from src.util.config import load_watchlist

    symbols = [e.ticker for e in load_watchlist(market="US")] + list(INDEX_ETFS)
    calls = {"n": 0}
    original = alpaca._request

    def counting(params, *, timeout):
        calls["n"] += 1
        return original(params, timeout=timeout)

    alpaca._request = counting
    try:
        df, report = alpaca.fetch(symbols, dt.date(2026, 7, 27), dt.date(2026, 7, 31))
    finally:
        alpaca._request = original

    assert calls["n"] == 2, f"{len(symbols)} symbols should cost 2 requests, not {calls['n']}"
    assert set(df["ticker"]) == set(symbols)
    assert next(c for c in report.results if c.name == "fetch").passed
