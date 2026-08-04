"""Tests for the KR flow / short interest / cap / fundamentals collector.

The fixture is a real pykrx response for 005930 over 2024-01-02..2024-01-19,
committed so the merge and validation paths run offline. That window is chosen
deliberately: it contains 신정 and covers the date whose values are pinned in
:data:`kr_flow.KNOWN_VALUE`.

The two checks worth reading first are ``flow_identity`` and ``implied_close``.
Both catch a class of error that a schema check cannot see — data that is
well-formed, plausible, and attached to the wrong column or the wrong day.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from src.collectors import kr_flow

FIXTURE = Path(__file__).parent / "fixtures" / "pykrx_flow_005930.json"


def _load() -> dict[str, pd.DataFrame]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    frames = {}
    for name, payload in raw.items():
        df = pd.DataFrame(payload["data"], columns=payload["columns"])
        df.index = pd.to_datetime(payload["index"])
        df.index.name = "날짜"
        frames[name] = df
    return frames


@pytest.fixture
def frames():
    return _load()


@pytest.fixture
def merged(frames):
    return kr_flow.normalize(
        frames["flow"], frames["cap"], frames["short"], frames["fundamental"], "005930"
    )


# --- the merge ------------------------------------------------------------


def test_the_four_endpoints_merge_to_one_row_per_session(merged):
    assert len(merged) == 14
    assert merged["date"].is_unique
    assert list(merged.columns) == list(kr_flow.SCHEMA)


def test_korean_columns_are_mapped_by_name(merged):
    # A positional rename would silently swap 기관합계 and 외국인합계, which are
    # adjacent in the response and carry the two largest rating weights.
    row = merged[merged["date"] == pd.Timestamp("2024-01-02")].iloc[0]
    assert row["foreign_net"] == 182_974_012_300
    assert row["inst_net"] == 45_093_469_200
    assert row["retail_net"] == -225_954_307_900


def test_missing_short_interest_does_not_delete_the_session(frames):
    """Short balance is legitimately absent for some names and days.

    Joining it inner would drop otherwise-good sessions and understate coverage,
    which reads downstream as a market that did not trade.
    """
    merged = kr_flow.normalize(
        frames["flow"], frames["cap"], frames["short"].iloc[:5], frames["fundamental"], "005930"
    )
    assert len(merged) == 14
    assert merged["short_balance"].isna().sum() == 9


def test_absent_flows_yield_an_empty_frame_rather_than_a_partial_one(frames):
    # pykrx signals failure with an empty frame. A row without flows is a failed
    # fetch, not a quiet session, so it must not reach the pipeline half-built.
    merged = kr_flow.normalize(
        pd.DataFrame(), frames["cap"], frames["short"], frames["fundamental"], "005930"
    )
    assert merged.empty
    assert list(merged.columns) == list(kr_flow.SCHEMA)


def test_known_at_is_the_kr_session_close(merged):
    known = pd.Timestamp(merged.iloc[0]["known_at_utc"])
    # 15:30 KST is 06:30 UTC. A row must not be usable before its session ends.
    assert known.hour == 6 and known.minute == 30


# --- the accounting identity ---------------------------------------------


def test_net_purchases_sum_to_zero(merged):
    assert kr_flow.check_flow_identity(merged).passed


def test_a_swapped_investor_column_breaks_the_identity(merged):
    """The failure this check exists for.

    Swapping foreign and institutional flows leaves every number individually
    plausible and every §2.2⑥ weight attached to the wrong series. Nothing about
    the schema, the dtypes or the magnitudes would look wrong — but the identity
    holds only for the correct assignment.
    """
    broken = merged.copy()
    broken.loc[0, "foreign_net"] = broken.loc[0, "foreign_net"] + 1
    result = kr_flow.check_flow_identity(broken)
    assert not result.passed
    assert "mismapped" in result.detail


def test_the_identity_names_the_rows_that_break_it(merged):
    broken = merged.copy()
    broken["inst_net"] = broken["inst_net"] + 1000
    result = kr_flow.check_flow_identity(broken)
    assert not result.passed
    assert "14 of 14" in result.detail


def test_the_identity_is_inert_on_an_empty_frame():
    assert kr_flow.check_flow_identity(pd.DataFrame(columns=list(kr_flow.SCHEMA))).passed


# --- the cross-collector check -------------------------------------------


def test_market_cap_over_shares_equals_the_price_collectors_close(merged):
    """Ties this collector to a number confirmed outside the project.

    kr_price pins 005930's 2024-01-02 close at 79,600 against Naver Finance.
    Deriving the same figure here means the two collectors agree about the same
    session; deriving a different one means the report would carry both.
    """
    result = kr_flow.check_implied_close(merged)
    assert result.passed
    assert "79,600" in result.detail


def test_a_wrong_market_cap_is_caught_by_the_implied_close(merged):
    broken = merged.copy()
    broken.loc[broken["date"] == pd.Timestamp("2024-01-02"), "market_cap"] *= 2
    result = kr_flow.check_implied_close(broken)
    assert not result.passed
    assert "disagree" in result.detail


def test_the_implied_close_is_silent_when_the_pinned_day_is_absent(merged):
    later = merged[merged["date"] > pd.Timestamp("2024-01-02")]
    result = kr_flow.check_implied_close(later)
    assert result.passed
    assert "nothing to check" in result.detail


# --- the known value ------------------------------------------------------


def test_shares_outstanding_matches_the_published_figure(merged):
    from src.collectors.validate import check_known_value

    assert check_known_value(merged, **kr_flow.KNOWN_VALUE).passed


# --- the report -----------------------------------------------------------


def test_an_empty_frame_over_real_sessions_is_a_failure():
    report = kr_flow.validate_frame(
        pd.DataFrame(columns=list(kr_flow.SCHEMA)),
        ["005930"],
        dt.date(2024, 1, 2),
        dt.date(2024, 1, 19),
        known_value=False,
    )
    not_empty = next(c for c in report.results if c.name == "not_empty")
    assert not not_empty.passed
    assert "empty frame on a failed request" in not_empty.detail


def test_holidays_are_not_reported_as_gaps(merged):
    # The window spans 신정. A calendar-driven continuity check must not count
    # it as missing data.
    report = kr_flow.validate_frame(
        merged, ["005930"], dt.date(2024, 1, 2), dt.date(2024, 1, 19), known_value=True
    )
    assert report.ok, report.failures


@pytest.mark.network
def test_the_live_endpoints_still_answer_in_the_shape_we_parse():
    df, report = kr_flow.fetch(
        ["005930"], dt.date(2024, 1, 2), dt.date(2024, 1, 19), sleep_seconds=0.3
    )
    assert not df.empty
    assert kr_flow.check_flow_identity(df).passed
    assert kr_flow.check_implied_close(df).passed
    assert report.ok, report.failures


# --- the session KRX may refuse to give ----------------------------------


def test_a_blocked_krx_login_is_reported_rather_than_raised(monkeypatch):
    """pykrx logs in during import, so a refused login raises at `import pykrx`.

    Observed 2026-08-05: after enough requests from one address, KRX answers the
    login endpoint with an HTML error page and pykrx surfaces a JSONDecodeError
    from deep inside the library. Left unguarded that aborts the whole pipeline
    run, when CLAUDE.md requires a failing collector to record the failure and
    let a partial report be published.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "pykrx":
            raise ValueError("Expecting value: line 13 column 1 (char 25)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    df, report = kr_flow.fetch(["005930"], dt.date(2024, 1, 2), dt.date(2024, 1, 19))

    assert df.empty
    session = next(c for c in report.results if c.name == "krx_session")
    assert not session.passed
    assert "rate-limiting" in session.detail
