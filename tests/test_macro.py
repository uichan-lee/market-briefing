"""Tests for the macro collector (SPEC §2.2⑨, §3.2).

Offline tests run against a committed FRED payload covering all of 2024 — it
contains the twelve ``"."`` rows and the bond-market holidays, so the parse and
coverage paths are exercised on the shapes that actually occur.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from src.collectors.macro import (
    KNOWN_VALUE,
    MIN_COVERAGE,
    SCHEMA,
    SERIES,
    _parse,
    check_coverage,
    validate_frame,
)
from src.util.session import next_tradeable_open, session_close_utc, trading_days

FIXTURE = Path(__file__).parent / "fixtures" / "macro_fred_2024.json"

START = dt.date(2024, 1, 1)
END = dt.date(2024, 12, 31)


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def frame(payload) -> pd.DataFrame:
    parts = [_parse(v["observations"], k, v["series_id"]) for k, v in payload.items()]
    df = pd.concat(parts, ignore_index=True)
    return df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})


# --- the fixture ----------------------------------------------------------


def test_the_fixture_still_contains_missing_markers(payload):
    """If FRED ever stops using '.', the parse below is testing nothing."""
    values = [o["value"] for o in payload["us_10y"]["observations"]]
    assert "." in values, "fixture no longer exercises the missing-value path"


# --- parsing --------------------------------------------------------------


def test_missing_markers_are_dropped_not_coerced(payload, frame):
    """pd.to_numeric(errors='coerce') would turn '.' into NaN, which reads as a
    collector fault rather than as a closed market."""
    raw = payload["us_10y"]["observations"]
    dots = sum(1 for o in raw if o["value"] == ".")
    parsed = frame[frame["series"] == "us_10y"]

    assert dots > 0
    assert len(parsed) == len(raw) - dots
    assert parsed["value"].notna().all()


def test_dtypes_match_the_declared_schema(frame):
    for column, want in SCHEMA.items():
        assert str(frame[column].dtype) == want, column


def test_the_known_value_survives_parsing(frame):
    """10Y closed at 3.95 on 2024-01-02 — cross-checked against
    home.treasury.gov, which publishes H.15 and which FRED redistributes."""
    row = frame[(frame["series"] == "us_10y") & (frame["date"] == pd.Timestamp("2024-01-02"))]
    assert len(row) == 1
    assert row["value"].iloc[0] == pytest.approx(3.95)


def test_provenance_travels_with_each_row(frame):
    assert set(frame[frame["series"] == "us_10y"]["series_id"]) == {"DGS10"}
    assert set(frame[frame["series"] == "usdkrw"]["series_id"]) == {"DEXKOUS"}


def test_parsing_an_all_missing_series_yields_the_schema(frame):
    empty = _parse([{"date": "2024-01-01", "value": "."}], "us_10y", "DGS10")
    assert list(empty.columns) == list(SCHEMA)
    assert empty.empty


# --- the look-ahead boundary ----------------------------------------------


def test_known_at_utc_is_after_the_observation_date(frame):
    """A value dated D is not knowable during D; FRED publishes after the close."""
    row = frame[(frame["series"] == "us_10y") & (frame["date"] == pd.Timestamp("2024-01-02"))]
    known_at = row["known_at_utc"].iloc[0]

    assert known_at > session_close_utc("US", dt.date(2024, 1, 2))
    assert known_at == next_tradeable_open("US", session_close_utc("US", dt.date(2024, 1, 2)))


def test_known_at_utc_is_timezone_aware(frame):
    assert frame["known_at_utc"].dt.tz is not None


def test_every_row_is_knowable_strictly_after_its_own_date(frame):
    assert (frame["known_at_utc"] > frame["date"].dt.tz_localize("UTC")).all()


# --- coverage, i.e. check three -------------------------------------------


def test_a_real_year_passes_coverage(frame):
    """DGS10 is missing 2024-10-14 and 2024-11-11 — Columbus Day and Veterans
    Day, when the bond market closes and NYSE does not. Real data must pass."""
    result = check_coverage(frame, ["us_10y", "usdkrw"], START, END)
    assert result.passed, result.detail


def test_the_bond_holidays_really_are_absent(frame):
    """Documents the reason coverage is a ratio rather than exact equality."""
    dates = set(frame[frame["series"] == "us_10y"]["date"].dt.date)
    assert dt.date(2024, 10, 14) not in dates
    assert dt.date(2024, 11, 11) not in dates


def test_a_half_broken_fetch_fails_coverage(frame):
    half = frame[frame["date"] < pd.Timestamp("2024-07-01")]
    result = check_coverage(half, ["us_10y"], START, END)
    assert not result.passed
    # Caught by the staleness bound rather than the coverage ratio, which is the
    # more precise diagnosis: a fetch truncated at mid-year is a series that
    # stopped, not one with holes in it. Splitting the two made the message
    # match the actual failure.
    assert "stopped rather than lagged" in result.detail


def test_a_series_that_returned_nothing_fails_coverage(frame):
    result = check_coverage(frame, ["us_10y", "wti"], START, END)
    assert not result.passed
    assert "wti" in result.detail


def test_a_weekend_row_is_caught(frame):
    """No calendar puts a market print on a Saturday, so this is unambiguously
    a wrongly-indexed series."""
    saturday = pd.Timestamp("2024-07-06")
    assert saturday.weekday() == 5

    stray = pd.concat([frame, frame.iloc[[0]].assign(date=saturday)], ignore_index=True)
    result = check_coverage(stray, ["us_10y"], START, END)

    assert not result.passed
    assert "weekends" in result.detail


def test_an_fx_print_on_good_friday_is_not_flagged(frame):
    """The reason the stray test uses weekends rather than the exchange
    calendar. DEXKOUS really does print on 2024-03-29 with XNYS shut, and
    calling that an error would fail correct data every year."""
    good_friday = dt.date(2024, 3, 29)
    fx_dates = set(frame[frame["series"] == "usdkrw"]["date"].dt.date)
    assert good_friday in fx_dates, "fixture no longer exercises this case"

    result = check_coverage(frame, ["usdkrw"], START, END)
    assert result.passed, result.detail


def test_duplicated_dates_are_caught(frame):
    doubled = pd.concat([frame, frame[frame["series"] == "us_10y"]], ignore_index=True)
    result = check_coverage(doubled, ["us_10y"], START, END)
    assert not result.passed
    assert "duplicated" in result.detail


def test_coverage_threshold_is_strict_enough_to_matter():
    assert 0.9 < MIN_COVERAGE < 1.0


# --- the full report ------------------------------------------------------


def test_a_clean_frame_passes_every_check(frame):
    report = validate_frame(frame, ["us_10y", "usdkrw"], START, END)
    assert report.ok, report.summary()


def test_a_wrong_yield_is_caught(frame):
    tampered = frame.copy()
    mask = (tampered["series"] == "us_10y") & (tampered["date"] == pd.Timestamp("2024-01-02"))
    tampered.loc[mask, "value"] = 9.99
    report = validate_frame(tampered, ["us_10y", "usdkrw"], START, END)

    assert not report.ok
    assert any(r.name == "known_value" for r in report.failures)


def test_the_known_value_is_wired_to_a_series_we_actually_fetch():
    assert KNOWN_VALUE["where"]["series"] in SERIES


def test_every_declared_series_has_a_fred_id():
    assert all(isinstance(v, str) and v for v in SERIES.values())
    assert len(set(SERIES.values())) == len(SERIES), "duplicate FRED id"


# --- live -----------------------------------------------------------------


@pytest.mark.network
def test_live_fetch_matches_the_committed_fixture():
    """Catches FRED changing under the fixture. Excluded from the default run."""
    from src.collectors.macro import fetch

    df, report = fetch(START, END, series={"us_10y": "DGS10"})
    assert report.ok, report.summary()

    row = df[df["date"] == pd.Timestamp("2024-01-02")].iloc[0]
    assert row["value"] == pytest.approx(KNOWN_VALUE["expected"], abs=KNOWN_VALUE["tolerance"])


# --- lag is not a gap -----------------------------------------------------


def _series_frame(name: str, dates: list[dt.date]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates).astype("datetime64[s]"),
            "series": name,
            "series_id": "TEST",
            "value": 1.0,
        }
    )


def test_a_publication_lag_is_reported_but_does_not_fail():
    """WTI is an EIA series FRED redistributes days behind.

    Counting its stale tail as missing coverage failed this check on every run
    whose window reached the present, for a series with no gaps at all. A check
    that fails daily is one that stops being read.
    """
    days = trading_days("US", dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    lagging = _series_frame("wti", days[:-4])

    result = check_coverage(lagging, ["wti"], dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    assert result.passed
    assert "stale" in result.detail


def test_an_interior_gap_still_fails():
    """The failure that matters, and which the old form diluted.

    Splitting lag from gaps makes this *more* sensitive: a hole no longer hides
    behind a long run of good days.
    """
    days = trading_days("US", dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    holed = _series_frame("wti", days[:2] + days[10:])

    result = check_coverage(holed, ["wti"], dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    assert not result.passed
    assert "interior gap" in result.detail


def test_a_series_that_stopped_publishing_fails():
    """A lag is tolerated; a series that quietly died is not.

    The distinction is the bound: a few sessions cost nothing to a 120-day
    trend, six months of silence would cost everything.
    """
    days = trading_days("US", dt.date(2026, 1, 1), dt.date(2026, 7, 31))
    dead = _series_frame("wti", days[:60])

    result = check_coverage(dead, ["wti"], dt.date(2026, 1, 1), dt.date(2026, 7, 31))
    assert not result.passed
    assert "stopped rather than lagged" in result.detail


def test_a_series_with_no_rows_at_all_is_named():
    result = check_coverage(
        pd.DataFrame(columns=["date", "series", "series_id", "value"]),
        ["wti"],
        dt.date(2026, 7, 1),
        dt.date(2026, 7, 31),
    )
    assert not result.passed
    assert "no rows at all" in result.detail
