"""Tests for the calendar collector (SPEC §2.2④, partial).

Offline tests run against two committed fixtures: a real FRED
``fred/release/dates`` response for CPI/Employment Situation
(``calendar_fred_release_dates.json``, fetched 2026-08-14 with
``include_release_dates_with_no_data=true``) and the real
``federalreserve.gov`` FOMC calendar page
(``fomc_calendar_2026.html``, same fetch date, 2021-2027 coverage).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from src.collectors.calendar import (
    EVENTS,
    FRED_RELEASES,
    KNOWN_VALUES,
    MAX_GAP_DAYS,
    SCHEMA,
    _fetch_fomc_rows,
    _monthly_options_expiration,
    _options_expiration_rows,
    _parse_fomc_row,
    check_event_continuity,
    check_known_date,
    fetch,
    validate_frame,
)
from src.util.session import trading_days

FRED_FIXTURE = Path(__file__).parent / "fixtures" / "calendar_fred_release_dates.json"
FOMC_FIXTURE = Path(__file__).parent / "fixtures" / "fomc_calendar_2026.html"

START = dt.date(2026, 1, 1)
END = dt.date(2026, 12, 31)


@pytest.fixture
def fred_payload() -> dict:
    return json.loads(FRED_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def fomc_html() -> str:
    return FOMC_FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def fomc_rows(fomc_html) -> list[dict]:
    rows, error = _fetch_fomc_rows(html=fomc_html)
    assert error is None
    return rows


def _fred_rows(payload: dict, event: str, release_id: int, label: str) -> list[dict]:
    return [
        {
            "date_start": dt.date.fromisoformat(item["date"]),
            "date": dt.date.fromisoformat(item["date"]),
            "event": event,
            "label": label,
            "source": f"FRED release_id={release_id}",
            "has_sep": False,
        }
        for item in payload[event]["release_dates"]
    ]


@pytest.fixture
def frame(fomc_html, fred_payload) -> pd.DataFrame:
    """A full, schema-shaped frame built entirely from the two committed
    fixtures — real CPI/Employment Situation dates, real FOMC meetings —
    plus computed options expiry. Real FRED data is used rather than
    hand-invented dates deliberately: an early version of this fixture
    invented CPI dates that landed on weekends and assumed every employment
    date is a Friday, and both were wrong — 2026-02-11 (Wednesday) and
    2026-07-02 (Thursday) are real BLS scheduling exceptions, not collector
    defects. Using the real fixture catches that instead of hiding it."""
    fomc_rows, error = _fetch_fomc_rows(html=fomc_html)
    assert error is None

    rows = [r for r in fomc_rows if START <= r["date"] <= END]
    rows += _fred_rows(fred_payload, "cpi", 10, "CPI 발표")
    rows += _fred_rows(fred_payload, "employment_situation", 50, "고용지표 발표")
    rows += _options_expiration_rows(START, END)

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[s]")
    df["date_start"] = pd.to_datetime(df["date_start"]).astype("datetime64[s]")
    df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
    df["known_at_utc"] = pd.Timestamp("2026-08-14T00:00:00Z")
    return df[list(SCHEMA)]


# --- the fixtures ------------------------------------------------------------


def test_the_fred_fixture_still_has_future_dates(fred_payload):
    """If FRED ever changes what include_release_dates_with_no_data=true
    returns, the parse below is testing nothing."""
    cpi_dates = [d["date"] for d in fred_payload["cpi"]["release_dates"]]
    assert len(cpi_dates) > 1, "fixture no longer exercises the future-dates path"


def test_the_fomc_fixture_still_has_a_notation_vote_entry(fomc_rows):
    notation = [r for r in fomc_rows if "notation vote" in r["label"]]
    assert notation, "fixture no longer exercises the notation-vote path"


def test_the_fomc_fixture_still_has_a_month_spanning_meeting(fomc_rows):
    spanning = [r for r in fomc_rows if r["date_start"].month != r["date"].month]
    assert spanning, "fixture no longer exercises the month-span path"


def test_the_fomc_fixture_still_has_the_sep_footnote(fomc_html):
    assert "Meeting associated with a Summary of Economic Projections" in fomc_html


# --- FOMC parsing --------------------------------------------------------------


def test_a_regular_two_day_meeting_parses(fomc_rows):
    row = next(r for r in fomc_rows if r["date_start"] == dt.date(2026, 1, 27))
    assert row["date"] == dt.date(2026, 1, 28)
    assert row["event"] == "fomc"
    assert row["has_sep"] is False


def test_a_sep_meeting_is_marked(fomc_rows):
    """September 2026: month 'September', date '15-16*' — the primary-source
    fact this collector's known-value check also relies on."""
    row = next(r for r in fomc_rows if r["date_start"] == dt.date(2026, 9, 15))
    assert row["date"] == dt.date(2026, 9, 16)
    assert row["has_sep"] is True


def test_a_month_spanning_meeting_resolves_start_and_end_correctly(fomc_rows):
    """2024 panel: month 'Apr/May', date '30-1'. The first day belongs to
    April, the second to May — read off the month cell, not inferred from
    '1 < 30'."""
    row = next(r for r in fomc_rows if r["date_start"] == dt.date(2024, 4, 30))
    assert row["date"] == dt.date(2024, 5, 1)


def test_a_january_february_rollover_resolves_correctly(fomc_rows):
    """2023 panel: month 'Jan/Feb', date '31-1'."""
    row = next(r for r in fomc_rows if r["date_start"] == dt.date(2023, 1, 31))
    assert row["date"] == dt.date(2023, 2, 1)


def test_an_october_november_rollover_resolves_correctly(fomc_rows):
    """2023 panel: month 'Oct/Nov', date '31-1'."""
    row = next(r for r in fomc_rows if r["date_start"] == dt.date(2023, 10, 31))
    assert row["date"] == dt.date(2023, 11, 1)


def test_a_notation_vote_is_a_single_day_not_a_two_day_meeting(fomc_rows):
    """2025 panel: month 'August', date '22 (notation vote)' — not a
    scheduled 2-day meeting."""
    row = next(r for r in fomc_rows if r["date_start"] == dt.date(2025, 8, 22))
    assert row["date"] == dt.date(2025, 8, 22)
    assert "notation vote" in row["label"]
    assert row["has_sep"] is False


def test_a_hypothetical_december_january_rollover_advances_the_year():
    """No real Dec/Jan pair appears in the live 2021-2027 range, but the
    parser must still handle it explicitly rather than accidentally."""
    row = _parse_fomc_row(2026, "Dec/Jan", "30-1")
    assert row["date_start"] == dt.date(2026, 12, 30)
    assert row["date"] == dt.date(2027, 1, 1)


def test_every_fomc_row_in_the_fixture_covers_eight_meetings_a_year(fomc_rows):
    """8 scheduled meetings/year is this module's MAX_GAP_DAYS assumption for
    'fomc' — verify it holds for a full year in the fixture, notation votes
    aside."""
    year_2026 = [
        r for r in fomc_rows if r["date_start"].year == 2026 and "notation" not in r["label"]
    ]
    assert len(year_2026) == 8


# --- CPI / Employment Situation parsing ---------------------------------------


def test_fred_release_dates_all_parse_to_real_dates(fred_payload):
    from src.collectors.calendar import FRED_RELEASES

    for event, release_id in FRED_RELEASES.items():
        payload = fred_payload[event]
        assert payload["release_dates"][0]["release_id"] == release_id
        for item in payload["release_dates"]:
            dt.date.fromisoformat(item["date"])  # raises if malformed


def test_employment_situation_fixture_has_a_real_off_friday_exception(fred_payload):
    """Confirms this collector's design reasoning against real data: BLS does
    not always release on the first Friday. 2026-02-11 is a Wednesday and
    2026-07-02 is a Thursday in the committed fixture — real scheduling
    exceptions, which is why check_event_continuity does not assert Friday
    for this event the way it does for options_expiration_monthly."""
    weekdays = {
        dt.date.fromisoformat(item["date"]).weekday()
        for item in fred_payload["employment_situation"]["release_dates"]
    }
    assert weekdays != {4}, "fixture no longer exercises the off-Friday exception"


# --- options expiration --------------------------------------------------------


def test_options_expiration_hits_the_third_friday_for_a_normal_month():
    assert _monthly_options_expiration(2026, 9) == dt.date(2026, 9, 18)
    assert _monthly_options_expiration(2026, 12) == dt.date(2026, 12, 18)


def test_options_expiration_rolls_back_on_a_market_holiday():
    """Verified live: 2026-06-19 (the 3rd Friday) is Juneteenth, NYSE closed.
    Cross-checked directly against this repo's own trading_days('US', ...)."""
    expiry = _monthly_options_expiration(2026, 6)
    assert expiry == dt.date(2026, 6, 18)
    days = trading_days("US", dt.date(2026, 6, 15), dt.date(2026, 6, 22))
    assert dt.date(2026, 6, 18) in days
    assert dt.date(2026, 6, 19) not in days


def test_options_expiration_rows_cover_every_month_in_the_window():
    rows = _options_expiration_rows(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    assert len(rows) == 12
    assert all(r["date"].weekday() in (3, 4) for r in rows)  # Thu on a holiday roll-back, else Fri


# --- known values ----------------------------------------------------------


def test_the_known_values_are_wired_to_events_we_actually_fetch():
    for kv in KNOWN_VALUES:
        assert kv["where"]["event"] in EVENTS


def test_every_declared_fred_release_has_an_id():
    assert all(isinstance(v, int) and v > 0 for v in FRED_RELEASES.values())
    assert len(set(FRED_RELEASES.values())) == len(FRED_RELEASES), "duplicate FRED release id"


def test_max_gap_days_covers_every_declared_event():
    assert set(EVENTS) <= set(MAX_GAP_DAYS)


def test_check_known_date_passes_on_the_real_fomc_row(frame):
    result = check_known_date(
        frame,
        where={"event": "fomc", "date": dt.date(2026, 9, 16)},
        column="date_start",
        expected=dt.date(2026, 9, 15),
    )
    assert result.passed, result.detail


def test_check_known_date_catches_a_wrong_date(frame):
    tampered = frame.copy()
    mask = (tampered["event"] == "fomc") & (tampered["date"] == pd.Timestamp("2026-09-16"))
    tampered.loc[mask, "date_start"] = pd.Timestamp("2026-09-01")
    result = check_known_date(
        tampered,
        where={"event": "fomc", "date": dt.date(2026, 9, 16)},
        column="date_start",
        expected=dt.date(2026, 9, 15),
    )
    assert not result.passed


# --- check three: event continuity ---------------------------------------


def test_a_real_window_passes_event_continuity(frame):
    result = check_event_continuity(frame, list(EVENTS), START, END)
    assert result.passed, result.detail


def test_an_interior_fomc_gap_fails():
    rows = [
        {
            "date": d,
            "date_start": d,
            "event": "fomc",
            "label": "x",
            "source": "x",
            "has_sep": False,
        }
        for d in (dt.date(2026, 1, 15), dt.date(2026, 10, 15))  # 9-month gap
    ]
    df = pd.DataFrame(rows)
    result = check_event_continuity(df, ["fomc"], START, END)
    assert not result.passed
    assert "interior gap" in result.detail


def test_a_series_with_no_rows_is_named():
    df = pd.DataFrame(columns=["date", "date_start", "event", "label", "source", "has_sep"])
    result = check_event_continuity(df, ["cpi"], START, END)
    assert not result.passed
    assert "cpi" in result.detail


def test_a_weekend_row_is_caught(frame):
    saturday = pd.Timestamp("2026-08-15")  # a real Saturday
    assert saturday.weekday() == 5
    stray = pd.concat(
        [frame, frame.iloc[[0]].assign(date=saturday, date_start=saturday)], ignore_index=True
    )
    result = check_event_continuity(stray, list(EVENTS), START, END)
    assert not result.passed
    assert "weekends" in result.detail


def test_employment_situation_on_a_weekend_is_still_caught(frame):
    """The weekend check applies to every event type, employment_situation
    included — only the Friday-specific check was removed (see the test
    below), not weekend detection."""
    tampered = frame.copy()
    mask = tampered["event"] == "employment_situation"
    first_idx = tampered[mask].index[0]
    tampered.loc[first_idx, "date"] = pd.Timestamp("2026-08-08")  # a Saturday, not the real value
    tampered.loc[first_idx, "date_start"] = pd.Timestamp("2026-08-08")
    result = check_event_continuity(tampered, list(EVENTS), START, END)
    assert not result.passed


def test_a_real_off_friday_employment_date_does_not_fail(frame):
    """The fixture already contains two real off-Friday dates (2026-02-11,
    2026-07-02) and test_a_real_window_passes_event_continuity confirms the
    whole frame still passes — this isolates that specific claim so a
    regression here fails with a clear name rather than a generic one."""
    weekdays = {
        row.date.weekday() for row in frame[frame["event"] == "employment_situation"].itertuples()
    }
    assert weekdays != {4}, "fixture no longer exercises the off-Friday exception"
    result = check_event_continuity(frame, list(EVENTS), START, END)
    assert result.passed, result.detail


# --- the full report ------------------------------------------------------


def test_a_clean_frame_passes_every_check(frame):
    report = validate_frame(frame, list(EVENTS), START, END)
    assert report.ok, report.summary()


def test_dtypes_match_the_declared_schema(frame):
    for column, want in SCHEMA.items():
        if column == "known_at_utc":
            continue  # resolution-only difference tolerated by check_schema itself
        assert str(frame[column].dtype) == want, column


# --- known_at_utc: fetch time, not observation time -----------------------


def test_known_at_utc_is_shared_across_every_row_in_one_fetch(frame):
    """The inverse of macro.py's rule, deliberately: every row from one
    fetch() call is knowable at the same instant, because every source here
    announces dates well in advance of the event — there is no per-row
    publication lag to protect against."""
    assert frame["known_at_utc"].nunique() == 1


def test_known_at_utc_is_timezone_aware(frame):
    assert frame["known_at_utc"].dt.tz is not None


# --- live -------------------------------------------------------------------


@pytest.mark.network
def test_live_fetch_matches_the_committed_fixtures():
    """Catches FRED or federalreserve.gov changing under the fixtures.
    Excluded from the default run."""
    df, report = fetch(dt.date(2026, 8, 1), dt.date(2026, 12, 31))
    assert report.ok, report.summary()
    sep_row = df[(df["event"] == "fomc") & (df["date"] == pd.Timestamp("2026-09-16"))]
    assert len(sep_row) == 1
    assert sep_row["date_start"].iloc[0] == pd.Timestamp("2026-09-15")


@pytest.mark.network
def test_live_fomc_page_still_uses_the_expected_css_classes():
    """Catches federalreserve.gov changing its markup under the fixture."""
    import requests

    from src.collectors.calendar import FOMC_URL

    response = requests.get(FOMC_URL, timeout=30)
    assert response.status_code == 200
    assert "fomc-meeting__month" in response.text
    assert "fomc-meeting__date" in response.text
    assert "Meeting associated with a Summary of Economic Projections" in response.text
